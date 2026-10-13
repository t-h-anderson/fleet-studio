"""Observed flow, native counters and evidence-linked tests; never dispatch work."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import json
import math
import re
import threading


def state_timings(prior, row, gap):
    compatible = prior and prior.get('sourceKey') == row['sourceKey'] and prior.get('releaseId') == row['releaseId']
    totals = dict(prior.get('stateSeconds', {})) if compatible else {}
    # Only intervals bounded by matching observations contribute. Transition times,
    # time before monitoring and gaps are not inferred from issue update times.
    if compatible and not gap and prior['stage'] == row['stage'] and prior['state'] == row['state']:
        delta = max(0, row['observedAt'] - prior['observedAt'])
        totals[row['stage']] = totals.get(row['stage'], 0) + delta
    row['stateSeconds'] = totals
    row['timingStartedAt'] = prior.get('timingStartedAt', prior['observedAt']) if compatible else row['observedAt']


def clean_test(payload):
    from studio_progress import plain
    status = payload.get('testStatus')
    if status not in ('passed', 'failed', 'blocked', 'running', 'skipped'):
        raise ValueError('Choose a test status: passed, failed, blocked, running or skipped.')
    revision = plain(payload.get('revision'), 'revision', 64, True)
    if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', revision):
        raise ValueError('Test revision must be the full Git commit SHA.')
    counts = {}
    for field in ('passed', 'failed', 'skipped'):
        value = payload.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= 10000000):
            raise ValueError('Test counts must be nonnegative integers or omitted.')
        counts[field] = value
    if status == 'passed' and counts['failed']:
        raise ValueError('A passed test run cannot contain failed tests.')
    duration = payload.get('durationSeconds')
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 31536000):
        raise ValueError('Test duration must be a finite nonnegative number of seconds.')
    return {'testStatus': status, 'suite': plain(payload.get('suite'), 'suite', 200, True),
            'revision': revision.lower(), 'durationSeconds': duration, **counts}


class Metrics:
    def __init__(self, progress):
        self.progress, self.app = progress, progress.app
        self.ci_lock = threading.RLock()
        self.ci = {}
        with closing(progress.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metric_samples (
                    fleet TEXT, kind TEXT, identity TEXT, bucket INTEGER, at REAL, data TEXT,
                    PRIMARY KEY(fleet,kind,identity,bucket));
                CREATE TABLE IF NOT EXISTS token_counters (
                    fleet TEXT, session TEXT, data TEXT, PRIMARY KEY(fleet,session));
                CREATE TABLE IF NOT EXISTS test_runs (
                    fleet TEXT, agent TEXT, event TEXT, at REAL, data TEXT,
                    PRIMARY KEY(fleet,agent,event));
                CREATE TABLE IF NOT EXISTS ci_snapshots (
                    fleet TEXT PRIMARY KEY, project TEXT, data TEXT);
            ''')

    def backlog_sample(self, db, fleet, snapshot, source, release):
        from workflow_spec import project_ticket
        stages = Counter()
        for ticket in snapshot['items']:
            if ticket['state'] != 'closed':
                stage = project_ticket(ticket, fleet['workflow'])['id'] if fleet.get('workflow', {}).get('states') else ticket['stage']
                stages[stage] += 1
        at = snapshot['observedAt']
        data = {'open': sum(stages.values()), 'stages': dict(stages), 'partial': bool(snapshot.get('partial'))}
        db.execute('INSERT OR REPLACE INTO metric_samples VALUES(?,?,?,?,?,?)',
                   (fleet['id'], 'backlog', json.dumps([source, release]), int(at // 3600), at, json.dumps(data)))

    def observe_tokens(self, fleets, monitor):
        with self.progress.lock, closing(self.progress.connect()) as db, db:
            for fleet in fleets:
                for sid in {a['sessionId'] for a in fleet['agents'] if a['sessionId']}:
                    session = monitor.get('sessions', {}).get(sid, {})
                    usage = session.get('usage') or {}
                    total, at = usage.get('tokensUsed'), session.get('observedAt')
                    if type(total) is not int or total < 0 or not at:
                        continue
                    row = db.execute('SELECT data FROM token_counters WHERE fleet=? AND session=?', (fleet['id'], sid)).fetchone()
                    old = json.loads(row['data']) if row else None
                    if old and at <= old['at']:
                        continue
                    delta = max(0, total - old['total']) if old else 0
                    reset = bool(old and total < old['total'])
                    data = {'total': total, 'at': at, 'firstAt': old['firstAt'] if old else at,
                            'observedTokens': (old['observedTokens'] if old else 0) + delta,
                            'resets': (old.get('resets', 0) if old else 0) + int(reset)}
                    db.execute('INSERT OR REPLACE INTO token_counters VALUES(?,?,?)', (fleet['id'], sid, json.dumps(data)))

    def ci_snapshot(self, fleet):
        from studio_progress import configuration
        config, _ = configuration(fleet)
        project, now = config['projectUrl'], self.progress.clock()
        with self.ci_lock:
            item = self.ci.get(fleet['id'])
            if not item or item['project'] != project:
                item = {'project': project, 'pipelines': [], 'observedAt': None, 'attemptedAt': None,
                        'loading': False, 'error': '', 'testSummary': None, 'testNotice': ''}
                with self.progress.lock, closing(self.progress.connect()) as db:
                    row = db.execute('SELECT data FROM ci_snapshots WHERE fleet=? AND project=?', (fleet['id'], project)).fetchone()
                if row:
                    item.update(json.loads(row['data']))
                self.ci[fleet['id']] = item
            if project and self.app.launch_enabled and not item['loading'] and (item['attemptedAt'] is None or now - item['attemptedAt'] > 90):
                item.update(loading=True, attemptedAt=now)
                threading.Thread(target=self._refresh_ci, args=(fleet['id'], item), daemon=True).start()
            result = deepcopy(item)
        result['stale'] = bool(result['observedAt'] and (result['error'] or now - result['observedAt'] > 180))
        return result

    def _refresh_ci(self, fid, item):
        from studio_backlog import gitlab_get
        try:
            project = item['project']
            rows = gitlab_get(project, 'pipelines?per_page=10&order_by=id&sort=desc')
            if not isinstance(rows, list):
                raise ValueError('GitLab returned an unexpected pipeline response.')
            pipelines = [{k: r.get(k) for k in ('id', 'status', 'ref', 'sha', 'web_url', 'updated_at')} for r in rows]
            summary, notice = None, ''
            if pipelines:
                pid = pipelines[0]['id']
                try:
                    detail = gitlab_get(project, 'pipelines/' + str(pid))
                    pipelines[0].update({k: detail.get(k) for k in ('duration', 'queued_duration', 'finished_at')})
                    tests = gitlab_get(project, 'pipelines/' + str(pid) + '/test_report_summary')
                    total = tests.get('total')
                    if isinstance(total, dict) and total.get('total_count', 0) > 0:
                        summary = {k: total.get(k) for k in ('total_count', 'success_count', 'failed_count', 'skipped_count', 'error_count', 'time')}
                    else:
                        notice = 'No published test report for the latest pipeline.'
                except (ValueError, OSError, KeyError):
                    notice = 'Latest pipeline test report unavailable; inspect its evidence in GitLab.'
            data = {'pipelines': pipelines, 'testSummary': summary, 'testNotice': notice,
                    'observedAt': self.progress.clock(), 'error': ''}
            with self.ci_lock:
                if self.ci.get(fid) is not item:
                    return
                item.update(data)
                with self.progress.lock, closing(self.progress.connect()) as db, db:
                    db.execute('INSERT OR REPLACE INTO ci_snapshots VALUES(?,?,?)', (fid, project, json.dumps(data)))
        except Exception:
            with self.ci_lock:
                item['error'] = 'GitLab CI could not be read. Check project access and the glab login.'
        finally:
            with self.ci_lock:
                item['loading'] = False

    def snapshot(self, fleet, tasks, agents, handoffs, source, release):
        now, fid = self.progress.clock(), fleet['id']
        with self.progress.lock, closing(self.progress.connect()) as db:
            trend = [{'at': r['at'], **json.loads(r['data'])} for r in db.execute(
                "SELECT at,data FROM metric_samples WHERE fleet=? AND kind='backlog' AND identity=? AND at>=? ORDER BY at",
                (fid, json.dumps([source, release]), now - 30 * 86400))]
            counters = {r['session']: json.loads(r['data']) for r in db.execute('SELECT session,data FROM token_counters WHERE fleet=?', (fid,))}
            tests = [json.loads(r['data']) for r in db.execute('SELECT data FROM test_runs WHERE fleet=? ORDER BY at DESC LIMIT 1000', (fid,))]
        # Keep one result per suite, reporter, task and revision. Older revisions
        # remain explicitly identified; a passed result is not current acceptance.
        latest = {}
        project = json.loads(source)[0].rstrip('/')
        for test in tests:
            if project and not test['taskUrl'].startswith(project + '/-/issues/'):
                continue
            latest.setdefault((test['agentId'], test['taskUrl'], test['suite'], test['revision']), test)
        token_rows, seen = [], set()
        for a in agents:
            sid = a['session'].get('sessionId')
            if not sid or sid in seen:
                continue
            seen.add(sid)
            usage = a['session'].get('usage') or {}
            row = counters.get(sid)
            token_rows.append({'agentId': a['id'], 'agentName': a['name'], 'sessionId': sid,
                               'tokensUsed': usage.get('tokensUsed'), 'goalTokens': usage.get('goalTokens'),
                               'goalBudget': usage.get('goalBudget'), 'model': usage.get('model'),
                               'observedTokens': row['observedTokens'] if row else None,
                               'firstAt': row['firstAt'] if row else None, 'resets': row.get('resets', 0) if row else 0,
                               'stale': a['session']['stale'] or usage.get('tokensUsed') is None})
        available = [r for r in token_rows if not r['stale'] and r['tokensUsed'] is not None]
        stage_counts = Counter(t['stage'] for t in tasks if t['state'] != 'closed')
        unassigned = sum(t['state'] != 'closed' and not t.get('assignees') for t in tasks)
        return {'backlogTrend': trend, 'openByStage': dict(stage_counts), 'unassigned': unassigned,
                'sessions': dict(Counter('unknown' if a['session']['stale'] else a['session'].get('status', 'unknown') for a in agents)),
                'tokens': {'agents': token_rows, 'total': sum(r['tokensUsed'] for r in available) if available else None,
                           'observed': sum(r['observedTokens'] for r in available if r['observedTokens'] is not None) if any(r['observedTokens'] is not None for r in available) else None,
                           'availableSessions': len(available), 'boundSessions': len({a['sessionId'] for a in fleet['agents'] if a['sessionId']})},
                'tests': list(latest.values())[:100], 'ci': self.ci_snapshot(fleet),
                'oldestHandoffSeconds': max((now - h['createdAt'] for h in handoffs if h.get('awaitingReceipt')), default=None)}

    def prune(self, db, fid, cutoff):
        db.execute('DELETE FROM metric_samples WHERE fleet=? AND at<?', (fid, cutoff))
        db.execute('DELETE FROM test_runs WHERE fleet=? AND at<?', (fid, cutoff))
