"""Read-only backlog projection. The configured tracker remains the ticket authority."""
from workflow_spec import project_ticket, stable
# Tracker names are re-exported for callers that predate the adapter module.
from studio_trackers import PROVIDERS, fetch_issues, gitlab_get, stage_of, tracker_for, GitLabTracker, GitHubTracker  # noqa: F401
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

STAGES = [('backlog', 'Scope / backlog'), ('ready', 'Ready'), ('active', 'In progress'),
          ('integration', 'Awaiting merge'), ('blocked', 'Blocked'), ('closed', 'Closed')]
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
            raise ValueError('Use an HTTPS GitLab or GitHub project URL without credentials or query parameters.')
    provider = value.get('provider', 'auto')
    if provider not in PROVIDERS:
        raise ValueError('Choose GitLab, GitHub or automatic tracker detection.')
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
    config = {'legacyReferences': legacy, 'projectUrl': url, 'scope': scope, 'agentTickets': {a: sorted(set(v)) for a, v in links.items()}}
    # Only record a provider when chosen, so existing GitLab configurations are unchanged.
    if provider != 'auto':
        config['provider'] = provider
    return config


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
        key = json.dumps([config['projectUrl'], config['scope']] + ([config['provider']] if 'provider' in config else []) + ([sorted(refs)] if config['scope'] == 'ai' else []))
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
                raise ValueError('Live tracker reads are disabled in this test instance.')
            else:
                issues, partial = tracker_for(config).snapshot(config)
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
                item['error'] = str(exc) if isinstance(exc, ValueError) else 'Tracker refresh failed. The last successful snapshot is retained.'
        finally:
            with self.lock:
                item['loading'] = False

    def history(self, fleet_id, iid):
        draft = self.app.store.fleet(fleet_id)
        active = self.app.deployments.data["active"].get(fleet_id)
        fleet = self.app.runtime_fleet(draft) if active else draft
        config = clean_config(draft.get('backlog'), {a['id'] for a in fleet['agents']}, fleet.get('roleSource'))
        if type(iid) is not int or iid <= 0 or not config['projectUrl']:
            raise ValueError('Choose a ticket in the configured tracker project.')
        if self.fixture or not self.app.launch_enabled:
            return {'events': [], 'notice': 'Ticket history is disabled in this test instance.'}
        tracker = tracker_for(config)
        rows = tracker.history(config, iid)
        events = [{'time': r['created_at'], 'action': r['action'], 'label': r['label']['name']} for r in rows if r.get('label') and r['label']['name'].startswith('AI ')]
        return {'events': sorted(events, key=lambda e: e['time'], reverse=True), 'notice': tracker.label + ' label history; up to 100 label events.'}
