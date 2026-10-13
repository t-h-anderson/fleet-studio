"""Read-only GitLab backlog projection. GitLab remains the ticket authority."""
from workflow_spec import project_ticket, stable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

STAGES = [('backlog', 'Scope / backlog'), ('ready', 'Ready'), ('active', 'In progress'),
          ('integration', 'Awaiting merge'), ('blocked', 'Blocked'), ('closed', 'Closed')]
class GitLabTracker:
    """Tracker adapter: snapshots and label history are read-only."""
    def snapshot(self, config):
        return fetch_issues(config)

    def history(self, config, iid):
        return gitlab_get(config['projectUrl'], 'issues/' + str(iid) + '/resource_label_events?per_page=100&sort=desc')



def clean_config(value, agents, source=None):
    value = value or {}
    default_url = (source or {}).get('url', '').split('/-/')[0]
    url = value.get('projectUrl', default_url)
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError('Invalid backlog project URL.')
    url = url.rstrip('/')
    if url:
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or len(parts.path.strip('/').split('/')) < 2:
            raise ValueError('Use an HTTPS GitLab project URL without credentials or query parameters.')
    scope = value.get('scope', 'all')
    if scope not in ('ai', 'all'):
        raise ValueError('Choose the AI workflow or all project tickets.')
    links = value.get('agentTickets', {})
    if not isinstance(links, dict) or any(a not in agents for a in links):
        raise ValueError('Ticket display links must name fleet agents.')
    for iids in links.values():
        if not isinstance(iids, list) or len(iids) > 30 or any(type(i) is not int or not 0 < i < 10000000 for i in iids):
            raise ValueError('Link at most 30 positive ticket numbers to each agent.')
    legacy = value.get('legacyReferences', {})
    if not isinstance(legacy, dict) or any(a not in agents or not isinstance(path, str) or len(path) > 1000 for a, path in legacy.items()):
        raise ValueError('Legacy references map known agents to local status files.')
    return {'legacyReferences': legacy, 'projectUrl': url, 'scope': scope, 'agentTickets': {a: sorted(set(v)) for a, v in links.items()}}


def stage_of(issue):
    labels = set(issue.get('labels', []))
    if issue['state'] == 'closed':
        return 'closed'
    if 'AI Blocked' in labels or 'AI Banned' in labels or any(x.startswith('Restriction::') for x in labels):
        return 'blocked'
    if 'AI Awaiting Merge' in labels:
        return 'integration'
    if 'AI Claimed' in labels:
        return 'active'
    if 'AI Ready' in labels:
        return 'ready'
    return 'backlog'


def project_api(url, suffix):
    parsed = urlsplit(url)
    return parsed.netloc, 'projects/' + quote(parsed.path.strip('/'), safe='') + '/' + suffix


def gitlab_get(project_url, suffix):
    host, endpoint = project_api(project_url, suffix)
    glab = shutil.which('glab') or str(Path.home() / 'AppData/Local/Programs/glab/glab.exe')
    env = dict(os.environ)
    # This host's inherited token can be stale; use the configured OS-keyring login.
    env.pop('GITLAB_TOKEN', None)
    run = subprocess.run([glab, 'api', endpoint, '--hostname', host, '--method', 'GET'],
                         capture_output=True, text=True, encoding='utf-8', errors='replace',
                         timeout=35, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if run.returncode:
        # Do not echo CLI output that could contain credentials or request headers.
        raise ValueError('GitLab could not be read. Check the glab login for ' + host + ' and refresh.')
    return json.loads(run.stdout)


def fetch_issues(config, get=gitlab_get):
    rows, partial = {}, False
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for state, limit in [('all', 50)]:
        for page in range(1, limit + 1):
            query = {'state': state, 'per_page': 100, 'page': page, 'order_by': 'updated_at', 'sort': 'desc'}
            if state == 'closed':
                query['updated_after'] = since
            batch = get(config['projectUrl'], 'issues?' + urlencode(query))
            if not isinstance(batch, list):
                raise ValueError('GitLab returned an unexpected issue response.')
            for row in batch:
                # Only a projection is retained, never full issue bodies or an editable queue.
                labels = row.get('labels', [])
                if config['scope'] == 'ai' and row['iid'] not in config.get('includeIids', []) and not any(l.startswith('AI ') or l in ('Scope review', 'Stage tracking') for l in labels):
                    continue
                rows[row['iid']] = {'iid': row['iid'], 'title': row['title'], 'state': row['state'],
                    'url': config['projectUrl'] + '/-/issues/' + str(row['iid']), 'labels': labels,
                    'stage': stage_of(row), 'updatedAt': row['updated_at'],
                    'assignees': [a.get('name') or a['username'] for a in row.get('assignees', [])]}
            if len(batch) < 100:
                break
            if page == limit:
                partial = True
    return list(rows.values()), partial


def local_references(fleet, config):
    refs = {}
    for aid, ids in config['agentTickets'].items():
        for iid in ids:
            refs.setdefault(iid, []).append({'agentId': aid, 'basis': 'Display link'})
    from studio_legacy_references import read_references
    return read_references(fleet, config, refs)



class Backlog:
    def __init__(self, app, fixture=None):
        self.app, self.fixture = app, fixture
        self.lock = threading.RLock()
        self.cache = {}

    def snapshot(self, fleet_id, force=False):
        draft = self.app.store.fleet(fleet_id)
        active = self.app.deployments.data["active"].get(fleet_id)
        fleet = self.app.runtime_fleet(draft) if active else draft
        config = clean_config(draft.get('backlog'), {a['id'] for a in fleet['agents']}, fleet.get('roleSource'))
        refs = local_references(fleet, config)
        key = json.dumps([config['projectUrl'], config['scope']] + ([sorted(refs)] if config['scope'] == 'ai' else []))
        with self.lock:
            item = self.cache.get(fleet_id)
            if not item or item['key'] != key:
                item = {'key': key, 'items': [], 'changes': [], 'observedAt': None, 'attemptedAt': 0,
                        'loading': False, 'error': '', 'partial': False}
                cached = self.app.progress.cached_backlog(draft) if hasattr(self.app, 'progress') else None
                if cached:
                    item.update(cached)
                self.cache[fleet_id] = item
            if config['projectUrl'] and not item['loading'] and (force or time.time() - item['attemptedAt'] > 90):
                item.update(loading=True, attemptedAt=time.time())
                threading.Thread(target=self._refresh, args=(fleet_id, config, item), daemon=True).start()
            result = deepcopy(item)
        refs = local_references(fleet, config)
        role_keys = {r['key']: r['id'] for r in fleet['roles']}
        for ticket in result['items']:
            ticket['references'] = refs.get(ticket['iid'], [])
            stage = ticket['stage']
            key = {'backlog': 'coordinator', 'ready': 'writer', 'active': 'writer', 'integration': 'integrator', 'blocked': 'coordinator'}.get(stage)
            ticket['roleIds'] = [role_keys[key]] if key in role_keys else []
            if fleet.get('workflow', {}).get('states'):
                mapped = project_ticket(ticket, fleet['workflow'])
                ticket['stage'] = mapped['id']
                ticket['roleIds'] = [r['id'] for r in fleet['roles'] if stable(r, 'role') in mapped['roles']]
        result.pop('key', None)
        result.update(config=config, workflowProjection="deployed" if active else "draft", stages=([{'id': s['id'], 'name': s['name']} for s in fleet['workflow']['states']] if fleet.get('workflow', {}).get('states') else [{'id': i, 'name': n} for i, n in STAGES]),
                      stale=bool(result['observedAt'] and (result['error'] or time.time() - result['observedAt'] > 180)))
        return result

    def _refresh(self, fleet_id, config, item):
        try:
            config = {**config, 'includeIids': list(local_references(self.app.store.fleet(fleet_id), config))}
            if self.fixture:
                rows = json.loads(Path(self.fixture).read_text(encoding='utf-8'))
                issues, partial = fetch_issues(config, lambda url, suffix: rows if 'page=1&' in suffix else [])
            elif not self.app.launch_enabled:
                raise ValueError('Live GitLab reads are disabled in this test instance.')
            else:
                issues, partial = GitLabTracker().snapshot(config)
            with self.lock:
                if self.cache.get(fleet_id) is not item:
                    return
                old = {i['iid']: i for i in item['items']}
                changes = [{'iid': i['iid'], 'title': i['title'], 'from': old[i['iid']]['stage'], 'to': i['stage'], 'time': time.time()}
                           for i in issues if i['iid'] in old and old[i['iid']]['stage'] != i['stage']]
                item.update(items=issues, partial=partial, observedAt=time.time(), error='',
                            changes=(changes + item['changes'])[:40])
                captured = deepcopy(item)
            if hasattr(self.app, 'progress'):
                current_fleet = self.app.store.fleet(fleet_id)
                current_config = clean_config(current_fleet.get('backlog'), {a['id'] for a in current_fleet['agents']}, current_fleet.get('roleSource'))
                if (current_config['projectUrl'], current_config['scope']) == (config['projectUrl'], config['scope']):
                    self.app.progress.observe_backlog(current_fleet, captured)
        except Exception as exc:
            with self.lock:
                item['error'] = str(exc) if isinstance(exc, ValueError) else 'GitLab refresh failed. The last successful snapshot is retained.'
        finally:
            with self.lock:
                item['loading'] = False

    def history(self, fleet_id, iid):
        draft = self.app.store.fleet(fleet_id)
        active = self.app.deployments.data["active"].get(fleet_id)
        fleet = self.app.runtime_fleet(draft) if active else draft
        config = clean_config(draft.get('backlog'), {a['id'] for a in fleet['agents']}, fleet.get('roleSource'))
        if type(iid) is not int or iid <= 0 or not config['projectUrl']:
            raise ValueError('Choose a ticket in the configured GitLab project.')
        if self.fixture or not self.app.launch_enabled:
            return {'events': [], 'notice': 'Ticket history is disabled in this test instance.'}
        rows = GitLabTracker().history(config, iid)
        events = [{'time': r['created_at'], 'action': r['action'], 'label': r['label']['name']} for r in rows if r.get('label') and r['label']['name'].startswith('AI ')]
        return {'events': sorted(events, key=lambda e: e['time'], reverse=True), 'notice': 'GitLab label history; up to 100 label events.'}
