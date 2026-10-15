"""Read-only tracker adapters. The external tracker remains the ticket authority.

Each adapter returns the same projection so stages, workflow states, progress and
CI monitoring do not depend on the hosting service:
- issues: iid, title, state ('opened' or 'closed'), url, labels, stage, updatedAt, assignees
- label history: created_at, action ('add' or 'remove'), label.name
- pipelines: id, status, ref, sha, web_url, updated_at, duration, queued_duration, finished_at
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
from urllib.parse import quote, urlencode, urlsplit
from studio_tracker_io import read_cli, read_error

PROVIDERS = ("auto", "gitlab", "github")


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


def in_scope(config, iid, labels):
    return config['scope'] != 'ai' or iid in config.get('includeIids', []) or any(
        l.startswith('AI ') or l in ('Scope review', 'Stage tracking') for l in labels)


def provider_of(config):
    """Explicit provider, otherwise inferred from the host. Self-hosted GitHub must be explicit."""
    provider = config.get('provider', 'auto')
    if provider != 'auto':
        return provider
    host = urlsplit(config.get('projectUrl', '')).hostname or ''
    return 'github' if host == 'github.com' or host.endswith('.github.com') else 'gitlab'


def issue_prefix(config):
    """URL prefix that every ticket in the configured project starts with."""
    project = config['projectUrl'].rstrip('/')
    return project + ('/issues/' if provider_of(config) == 'github' else '/-/issues/')


def project_api(url, suffix):
    parsed = urlsplit(url)
    return parsed.netloc, 'projects/' + quote(parsed.path.strip('/'), safe='') + '/' + suffix


def gitlab_get(project_url, suffix, recover_local_proxy=False):
    host, endpoint = project_api(project_url, suffix)
    glab = shutil.which('glab') or str(Path.home() / 'AppData/Local/Programs/glab/glab.exe')
    env = dict(os.environ)
    # This host's inherited token can be stale; use the configured OS-keyring login.
    env.pop('GITLAB_TOKEN', None)
    return read_cli([glab, 'api', endpoint, '--hostname', host, '--method', 'GET'],
                    'GitLab', host, env, recover_local_proxy)


def github_get(project_url, suffix, recover_local_proxy=False):
    parsed = urlsplit(project_url)
    host = parsed.netloc
    endpoint = 'repos/' + parsed.path.strip('/') + '/' + suffix
    gh = shutil.which('gh') or 'gh'
    return read_cli([gh, 'api', endpoint, '--hostname', host, '--method', 'GET'],
                    'GitHub', host, recover_local_proxy=recover_local_proxy)


def fetch_issues(config, get=None):
    get = get or gitlab_get
    rows, partial = {}, False
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for state, limit in [('all', 50)]:
        for page in range(1, limit + 1):
            query = {'state': state, 'per_page': 100, 'page': page, 'order_by': 'updated_at', 'sort': 'desc'}
            if state == 'closed':
                query['updated_after'] = since
            batch = get(config['projectUrl'], 'issues?' + urlencode(query))
            if not isinstance(batch, list):
                raise read_error('response', 'GitLab', urlsplit(config['projectUrl']).hostname)
            for row in batch:
                # Only a projection is retained, never full issue bodies or an editable queue.
                labels = row.get('labels', [])
                if not in_scope(config, row['iid'], labels):
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


def fetch_github_issues(config, get=None):
    get = get or github_get
    rows, partial, limit = {}, False, 50
    for page in range(1, limit + 1):
        query = {'state': 'all', 'per_page': 100, 'page': page, 'sort': 'updated', 'direction': 'desc'}
        batch = get(config['projectUrl'], 'issues?' + urlencode(query))
        if not isinstance(batch, list):
            raise read_error('response', 'GitHub', urlsplit(config['projectUrl']).hostname)
        for row in batch:
            # The GitHub issues endpoint also lists pull requests; they are not tickets.
            if 'pull_request' in row:
                continue
            labels = [l['name'] if isinstance(l, dict) else l for l in row.get('labels', [])]
            if not in_scope(config, row['number'], labels):
                continue
            state = 'closed' if row['state'] == 'closed' else 'opened'
            rows[row['number']] = {'iid': row['number'], 'title': row['title'], 'state': state,
                'url': config['projectUrl'] + '/issues/' + str(row['number']), 'labels': labels,
                'stage': stage_of({'state': state, 'labels': labels}), 'updatedAt': row['updated_at'],
                'assignees': [a.get('login', '') for a in row.get('assignees') or []]}
        if len(batch) < 100:
            break
        if page == limit:
            partial = True
    return list(rows.values()), partial


def seconds_between(start, end):
    try:
        parse = lambda v: datetime.fromisoformat(v.replace('Z', '+00:00'))
        return max(0, round((parse(end) - parse(start)).total_seconds()))
    except (AttributeError, TypeError, ValueError):
        return None


class GitLabTracker:
    name, label = 'gitlab', 'GitLab'

    def __init__(self, config=None):
        self.recover_local_proxy = bool((config or {}).get('recoverLocalProxy', False))

    def get(self, project_url, suffix):
        if self.recover_local_proxy:
            return gitlab_get(project_url, suffix, recover_local_proxy=True)
        return gitlab_get(project_url, suffix)

    def snapshot(self, config):
        return fetch_issues(config, self.get)

    def history(self, config, iid):
        return self.get(config['projectUrl'], 'issues/' + str(iid) + '/resource_label_events?per_page=100&sort=desc')

    def pipelines(self, project):
        rows = self.get(project, 'pipelines?per_page=10&order_by=id&sort=desc')
        if not isinstance(rows, list):
            raise read_error('response', 'GitLab', urlsplit(project).hostname)
        pipelines = [{k: r.get(k) for k in ('id', 'status', 'ref', 'sha', 'web_url', 'updated_at')} for r in rows]
        summary, notice = None, ''
        if pipelines:
            pid = pipelines[0]['id']
            try:
                detail = self.get(project, 'pipelines/' + str(pid))
                pipelines[0].update({k: detail.get(k) for k in ('duration', 'queued_duration', 'finished_at')})
                tests = self.get(project, 'pipelines/' + str(pid) + '/test_report_summary')
                total = tests.get('total')
                if isinstance(total, dict) and total.get('total_count', 0) > 0:
                    summary = {k: total.get(k) for k in ('total_count', 'success_count', 'failed_count', 'skipped_count', 'error_count', 'time')}
                else:
                    notice = 'No published test report for the latest pipeline.'
            except (ValueError, OSError, KeyError):
                notice = 'Latest pipeline test report unavailable; inspect its evidence in GitLab.'
        return pipelines, summary, notice


# GitHub run states mapped onto the GitLab pipeline vocabulary the monitoring view uses.
RUN_STATUS = {'success': 'success', 'failure': 'failed', 'timed_out': 'failed', 'startup_failure': 'failed',
              'cancelled': 'canceled', 'skipped': 'skipped', 'neutral': 'neutral', 'action_required': 'manual',
              'in_progress': 'running', 'queued': 'pending', 'waiting': 'pending', 'pending': 'pending', 'requested': 'pending'}


class GitHubTracker:
    name, label = 'github', 'GitHub'

    def __init__(self, config=None):
        self.recover_local_proxy = bool((config or {}).get('recoverLocalProxy', False))

    def get(self, project_url, suffix):
        if self.recover_local_proxy:
            return github_get(project_url, suffix, recover_local_proxy=True)
        return github_get(project_url, suffix)

    def snapshot(self, config):
        return fetch_github_issues(config, self.get)

    def history(self, config, iid):
        rows = []
        # GitHub returns issue events oldest-first. Read all pages up to a bounded
        # limit; never label the oldest page as the current recent history.
        for page in range(1, 51):
            batch = self.get(config['projectUrl'], 'issues/' + str(iid) + '/events?per_page=100&page=' + str(page))
            if not isinstance(batch, list):
                raise read_error('response', 'GitHub', urlsplit(config['projectUrl']).hostname)
            rows.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError('GitHub event history exceeds 5000 events; recent history is unavailable.')
        return sorted([{'created_at': r['created_at'], 'action': 'add' if r['event'] == 'labeled' else 'remove',
                        'label': {'name': r['label']['name']}} for r in rows
                       if r.get('event') in ('labeled', 'unlabeled') and isinstance(r.get('label'), dict)],
                      key=lambda r: r['created_at'], reverse=True)[:100]

    def pipelines(self, project):
        data = self.get(project, 'actions/runs?per_page=10')
        runs = data.get('workflow_runs') if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise read_error('response', 'GitHub', urlsplit(project).hostname)
        pipelines = []
        for r in runs:
            status = r.get('conclusion') if r.get('status') == 'completed' else r.get('status')
            finished = r.get('updated_at') if r.get('status') == 'completed' else None
            pipelines.append({'id': r.get('id'), 'status': RUN_STATUS.get(status, status), 'ref': r.get('head_branch'),
                              'sha': r.get('head_sha'), 'web_url': r.get('html_url'), 'updated_at': r.get('updated_at'),
                              'name': r.get('name'),
                              'duration': seconds_between(r.get('run_started_at'), finished) if finished else None,
                              'queued_duration': seconds_between(r.get('created_at'), r.get('run_started_at')),
                              'finished_at': finished})
        # GitHub Actions has no native test report summary; tests stay visible as agent-reported evidence.
        notice = 'GitHub Actions does not publish test totals; inspect the run or agent test evidence.' if pipelines else ''
        return pipelines, None, notice


TRACKERS = {'gitlab': GitLabTracker, 'github': GitHubTracker}


def tracker_for(config):
    return TRACKERS[provider_of(config)](config)


def tracker_label(fleet):
    """Display name of the fleet's ticket authority, used in agent-facing instructions."""
    return TRACKERS[provider_of(fleet.get('backlog') or {})].label
