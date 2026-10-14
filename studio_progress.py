"""Evidence-labelled progress projections. This database never owns tracker tasks."""
from studio_bindings import runtime_binding, session_key, same_binding
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from studio_trackers import issue_prefix

KINDS = ('task-started', 'progress-updated', 'blocked', 'unblocked', 'handoff-received', 'work-finished', 'test-result')
DEFAULTS = {'handoffMinutes': 30, 'quietMinutes': 60, 'retentionDays': 90}


def clean_settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unknown progress setting.')
    result = {**DEFAULTS, **value}
    for key, maximum in [('handoffMinutes', 10080), ('quietMinutes', 10080), ('retentionDays', 3650)]:
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(key + ' must be an integer between 1 and ' + str(maximum) + '.')
    return result


def plain(value, name, limit=1800, required=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError('Invalid ' + name + '.')
    if required and not value.strip():
        raise ValueError(name + ' is required.')
    return value.strip()


def link(value, name='URL'):
    value = plain(value, name, 1000, True)
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError(name + ' must be an HTTPS link without credentials.')
    return value


def task_key(value):
    p = urlsplit(value)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), p.query, ''))


def configuration(fleet):
    from studio_backlog import clean_config
    c = clean_config(fleet.get('backlog'), {a['id'] for a in fleet['agents']}, fleet.get('roleSource'))
    return c, json.dumps([c['projectUrl'], c['scope']] + ([c['provider']] if 'provider' in c else []), separators=(',', ':'))


class Progress:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.path = app.root / '.runtime' / 'progress.sqlite'
        self.path.parent.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.last_prune = 0
        self.observation_error = ""
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, fleet TEXT NOT NULL, category TEXT NOT NULL,
                    identity TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL,
                    UNIQUE(fleet, category, identity));
                CREATE INDEX IF NOT EXISTS observation_time ON observations(fleet, at);
                CREATE TABLE IF NOT EXISTS snapshots (
                    fleet TEXT PRIMARY KEY, source TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    fleet TEXT NOT NULL, url TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(fleet,url));
                CREATE TABLE IF NOT EXISTS handoffs (
                    fleet TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(fleet,id));
                CREATE TABLE IF NOT EXISTS identities (
                    fleet TEXT NOT NULL, agent TEXT NOT NULL, event TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(fleet,agent,event));
                CREATE TABLE IF NOT EXISTS sessions (fleet TEXT NOT NULL, agent TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(fleet,agent));
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    fleet TEXT NOT NULL, agent TEXT NOT NULL, url TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(fleet,agent,url));
                CREATE TABLE IF NOT EXISTS tracking_windows (
                    fleet TEXT, source TEXT, first_at REAL, PRIMARY KEY(fleet,source));
            ''')
        from studio_metrics import Metrics
        self.metrics = Metrics(self)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def record(db, fleet, category, identity, at, data):
        db.execute('INSERT OR IGNORE INTO observations(fleet,category,identity,at,data) VALUES(?,?,?,?,?)',
                   (fleet, category, identity, at, json.dumps(data)))

    def cached_backlog(self, fleet):
        _, source = configuration(fleet)
        with self.lock, closing(self.connect()) as db:
            row = db.execute('SELECT * FROM snapshots WHERE fleet=? AND source=?', (fleet['id'], source)).fetchone()
        return json.loads(row['data']) if row else None

    def observe_backlog(self, fleet, snapshot):
        """Called once per successful refresh, including refreshes without a browser."""
        from workflow_spec import project_ticket
        _, source = configuration(fleet)
        now, fid = snapshot['observedAt'], fleet['id']
        rid = self.app.deployments.data['active'].get(fid)
        runtime = self.app.runtime_fleet(fleet) if rid else fleet
        with self.lock, closing(self.connect()) as db, db:
            previous = db.execute('SELECT source,at FROM snapshots WHERE fleet=?', (fid,)).fetchone()
            if previous and previous['source'] != source:
                db.execute('DELETE FROM tasks WHERE fleet=?', (fid,))
            gap = not previous or now - previous['at'] > 180
            old = {r['url']: json.loads(r['data']) for r in db.execute('SELECT * FROM tasks WHERE fleet=?', (fid,))}
            first = min((t.get('timingStartedAt') or t.get('stageObservedAt') or t['observedAt'] for t in old.values()), default=now)
            db.execute('INSERT OR IGNORE INTO tracking_windows VALUES(?,?,?)', (fid, source, first))
            for issue in snapshot['items']:
                issue = deepcopy(issue)
                if runtime.get('workflow', {}).get('states'):
                    mapped = project_ticket(issue, runtime['workflow'])
                    issue['stage'] = mapped['id']
                url = task_key(issue['url'])
                prior = old.get(url)
                same_projection = prior and prior.get('releaseId') == rid
                changed = prior and same_projection and (prior['stage'], prior['state']) != (issue['stage'], issue['state'])
                entered = prior.get('stageObservedAt') if prior and same_projection and not changed else now
                row = {**issue, 'sourceKey': source, 'releaseId': rid, 'observedAt': now,
                       'stageObservedAt': entered, 'initialStageUnknown': prior.get('initialStageUnknown', True) if prior and same_projection and not changed else not bool(changed),
                       'observationGap': gap or bool(prior and prior.get('observationGap') and not changed)}
                from studio_metrics import state_timings
                state_timings(prior, row, gap or bool(prior and now - prior['observedAt'] > 180))
                if changed:
                    event = {'taskUrl': url, 'from': prior['stage'], 'to': issue['stage'], 'title': issue['title'],
                             'source': 'tracker', 'sourceKey': source, 'observationGap': gap,
                             'closed': prior['state'] != 'closed' and issue['state'] == 'closed'}
                    self.record(db, fid, 'tracker', url + ':' + str(now), now, event)
                db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?)', (fid, url, json.dumps(row)))
            # A complete snapshot establishes which tasks are still in the selected scope.
            if not snapshot.get('partial'):
                visible = {task_key(i['url']) for i in snapshot['items']}
                for url in set(old) - visible:
                    db.execute('DELETE FROM tasks WHERE fleet=? AND url=?', (fid, url))
            payload = {k: snapshot.get(k) for k in ('items', 'observedAt', 'partial')}
            db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)', (fid, source, now, json.dumps(payload)))
            self.metrics.backlog_sample(db, runtime, snapshot, source, rid)

    def observe_handoffs(self, actions):
        with self.lock, closing(self.connect()) as db, db:
            for action in actions:
                if not action.get('eventId'):
                    continue
                fid, aid = action['fleetId'], action['id']
                prior = db.execute('SELECT data FROM handoffs WHERE fleet=? AND id=?', (fid, aid)).fetchone()
                old = json.loads(prior['data']) if prior else {}
                value = {k: action.get(k) for k in ('id', 'name', 'createdAt', 'status', 'detail', 'ticket', 'eventId',
                         'targetAgentId', 'targetName', 'targetSessionId', 'targetDirectory', 'triggerId', 'sourceAgentId', 'deploymentId', 'reconciliation', 'releaseRevalidations', 'targetRuntime')}
                value['receivedAt'] = old.get('receivedAt')
                for key in ('receiptSource', 'receiptEvidence'):
                    value[key] = old.get(key)
                if action.get('reconciliation', {}).get('mode') == 'record-receipt':
                    record = action['reconciliation']
                    value.update(receivedAt=old.get('receivedAt') or record['at'], receiptSource=record['source'], receiptEvidence=record['evidenceUrl'])
                value['observedAt'] = self.clock()
                comparison = {k: v for k, v in value.items() if k != 'observedAt'}
                if comparison != {k: v for k, v in old.items() if k != 'observedAt'}:
                    self.record(db, fid, 'handoff', aid + ':' + str(self.clock()), self.clock(), value)
                db.execute('INSERT OR REPLACE INTO handoffs VALUES(?,?,?)', (fid, aid, json.dumps(value)))

    def observe_sessions(self, fleets, monitor):
        with self.lock, closing(self.connect()) as db, db:
            for fleet in fleets:
                for agent in fleet['agents']:
                    observed = monitor.get('sessions', {}).get(session_key(agent))
                    if not observed:
                        continue
                    row = db.execute('SELECT data FROM sessions WHERE fleet=? AND agent=?', (fleet['id'], agent['id'])).fetchone()
                    old = json.loads(row['data']) if row else None
                    data = {k: observed.get(k) for k in ('sessionId', 'status', 'goalStatus', 'queueCount')}
                    data.update(agentId=agent['id'], agentName=agent['name'], source='session-observation')
                    if old != data:
                        at = observed.get('observedAt') or self.clock()
                        self.record(db, fleet['id'], 'session', agent['id'] + ':' + str(at), at, data)
                        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (fleet['id'], agent['id'], json.dumps(data)))

    def pending_handoffs(self, fleet_id):
        with self.lock, closing(self.connect()) as db:
            rows = [json.loads(r['data']) for r in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fleet_id,))]
        return [r for r in rows if not r.get('receivedAt') and r.get('status') != 'dismissed']

    def report(self, payload):
        kind = payload.get('kind')
        if kind not in KINDS:
            raise ValueError('Choose a supported progress report kind.')
        fid, aid, rid = payload.get('fleetId'), payload.get('agentId'), payload.get('releaseId')
        # Lock release/binding checks with the write so a deployment cannot race it.
        with self.app.store.lock:
            fleet = self.app.store.fleet(fid)
            if not rid or rid != self.app.deployments.data['active'].get(fid):
                raise ValueError('Read the current deployed assignment before reporting progress.')
            deployed = self.app.deployments.release(rid)['fleet']
            agent = next((a for a in fleet['agents'] if a['id'] == aid), None)
            binding = next((a for a in deployed['agents'] if a['id'] == aid), None)
            if not agent or not binding or not agent['enabled'] or not binding['enabled'] or not same_binding(agent, binding, ('sessionId', 'directory', 'roleIds')) or payload.get('sessionId') != binding['sessionId']:
                raise ValueError('Reporting agent no longer matches the deployed binding.')
            event = plain(payload.get('eventId'), 'eventId', 200, True)
            task = link(payload.get('taskUrl'), 'taskUrl')
            config, _ = configuration(fleet)
            if config['projectUrl'] and not task_key(task).startswith(task_key(issue_prefix(config)) + '/'):
                raise ValueError('Task must belong to the configured tracker project.')
            data = {'kind': kind, 'agentId': aid, 'agentName': agent['name'], 'sessionId': agent['sessionId'],
                    'directory': agent['directory'], **runtime_binding(agent), 'releaseId': rid, 'eventId': event,
                    'taskUrl': task_key(task), 'evidenceUrl': link(payload.get('evidenceUrl'), 'evidenceUrl'),
                    'summary': plain(payload.get('summary', ''), 'summary'),
                    'reason': plain(payload.get('reason', ''), 'reason', required=kind == 'blocked'),
                    'nextAction': plain(payload.get('nextAction', ''), 'nextAction', required=kind == 'blocked'),
                    'handoffId': plain(payload.get('handoffId', ''), 'handoffId', 200, kind == 'handoff-received'),
                    'dependsOn': [link(u, 'dependency') for u in payload.get('dependsOn', [])] if isinstance(payload.get('dependsOn', []), list) and len(payload.get('dependsOn', [])) <= 20 else None,
                    'source': 'agent-report'}
            if data['dependsOn'] is None:
                raise ValueError('Supply at most 20 dependency links.')
            if kind == 'test-result':
                from studio_metrics import clean_test
                data.update(clean_test(payload))
            if payload.get('goalId'):
                from studio_goals import BLOCKERS, DETAILS
                if kind in ('test-result', 'handoff-received'):
                    raise ValueError('Receipts and test reports are separate from goal updates; omit goalId.')
                data['goalId'] = plain(payload['goalId'], 'goalId', 200, True)
                for key in DETAILS:
                    data[key] = plain(payload.get(key, ''), key, required=kind == 'blocked' and key != 'completionCriteria')
                if kind == 'blocked' and data['blockerCategory'] not in BLOCKERS:
                    raise ValueError('Choose a supported blockerCategory.')
            digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            with self.lock, closing(self.connect()) as db, db:
                prior = db.execute('SELECT digest FROM identities WHERE fleet=? AND agent=? AND event=?', (fid, aid, event)).fetchone()
                if prior:
                    if prior['digest'] != digest:
                        raise ValueError('This event ID was already used with different report details.')
                    return {'duplicate': True, 'eventId': event}
                now = self.clock()
                data['at'] = now
                if data.get('goalId'):
                    self.app.goals.link_report(db, fleet, agent, data)
                if kind == 'handoff-received':
                    row = db.execute('SELECT data FROM handoffs WHERE fleet=? AND id=?', (fid, data['handoffId'])).fetchone()
                    handoff = json.loads(row['data']) if row else None
                    if not handoff or handoff['targetAgentId'] != aid or handoff.get('targetSessionId') != agent['sessionId'] or handoff.get('targetDirectory') != agent['directory'] or runtime_binding(agent) != (handoff.get('targetRuntime') or {}) or task_key(handoff.get('ticket') or '') != data['taskUrl']:
                        raise ValueError('Receipt must match this agent, task and original handoff binding.')
                    handoff['receivedAt'] = now
                    handoff['receiptSource'] = 'agent-report'
                    handoff['receiptEvidence'] = data['evidenceUrl']
                    db.execute('UPDATE handoffs SET data=? WHERE fleet=? AND id=?', (json.dumps(handoff), fid, data['handoffId']))
                elif kind == 'test-result':
                    db.execute('INSERT INTO test_runs VALUES(?,?,?,?,?)', (fid, aid, event, now, json.dumps(data)))
                else:
                    prior_row = db.execute('SELECT data FROM agent_tasks WHERE fleet=? AND agent=? AND url=?', (fid, aid, data['taskUrl'])).fetchone()
                    previous = json.loads(prior_row['data']) if prior_row else {}
                    data['blockedSince'] = (previous.get('blockedSince') or now) if kind == 'blocked' else previous.get('blockedSince') if kind == 'progress-updated' else None
                    if kind == 'progress-updated' and data['blockedSince']:
                        for k in ('reason', 'nextAction', 'dependsOn', 'blockerCategory', 'blockerOwner', 'resumeCondition'):
                            data[k] = data.get(k) or previous.get(k)
                    db.execute('INSERT OR REPLACE INTO agent_tasks VALUES(?,?,?,?)', (fid, aid, data['taskUrl'], json.dumps(data)))
                db.execute('INSERT INTO identities VALUES(?,?,?,?)', (fid, aid, event, digest))
                self.record(db, fid, 'agent-report', aid + ':' + event, now, data)
        return {'duplicate': False, 'eventId': event, 'observedAt': now}

    def history(self, fleet_id, task=None, agent=None, before=None):
        self.app.store.fleet(fleet_id)
        with self.lock, closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM observations WHERE fleet=? AND id<? ORDER BY id DESC LIMIT 1000',
                              (fleet_id, int(before) if before else 9223372036854775807)).fetchall()
        result = []
        for r in rows:
            d = json.loads(r['data'])
            if task and task_key(d.get('taskUrl') or d.get('ticket') or '') != task_key(task):
                continue
            if agent and agent not in (d.get('agentId'), d.get('targetAgentId'), d.get('sourceAgentId')):
                continue
            result.append({'id': r['id'], 'at': r['at'], 'category': r['category'], **d})
            if len(result) == 100:
                break
        return {'items': result, 'nextBefore': result[-1]['id'] if len(result) == 100 else rows[-1]['id'] if len(rows) == 1000 else None}

    def tick(self, state, monitor):
        self.observe_handoffs(monitor.get('automation', {}).get('actions', []))
        self.observe_sessions(state['fleets'], monitor)
        self.app.goals.observe(state['fleets'], monitor)
        self.metrics.observe_tokens(state['fleets'], monitor)
        for fleet in state['fleets']:
            # Backlog refresh is asynchronous and coalesces concurrent browser requests.
            self.app.backlog.snapshot(fleet['id'])
            self.metrics.ci_snapshot(fleet)
        now = self.clock()
        if now - self.last_prune >= 3600:
            with self.lock, closing(self.connect()) as db, db:
                for f in state['fleets']:
                    cutoff = now - clean_settings(f.get('progress'))['retentionDays'] * 86400
                    db.execute('DELETE FROM observations WHERE fleet=? AND at<?', (f['id'], cutoff))
                    self.metrics.prune(db, f['id'], cutoff)
                    for row in db.execute('SELECT * FROM handoffs WHERE fleet=?', (f['id'],)).fetchall():
                        h = json.loads(row['data'])
                        if (h.get('receivedAt') or h.get('status') == 'dismissed') and h['createdAt'] < cutoff:
                            db.execute('DELETE FROM handoffs WHERE fleet=? AND id=?', (f['id'], row['id']))
                    for row in db.execute('SELECT * FROM agent_tasks WHERE fleet=?', (f['id'],)).fetchall():
                        d = json.loads(row['data'])
                        if d['at'] < cutoff:
                            db.execute('DELETE FROM agent_tasks WHERE fleet=? AND agent=? AND url=?', (f['id'], row['agent'], row['url']))
            self.last_prune = now

    def snapshot(self, fleet_id):
        from workflow_spec import project_ticket, stable
        fleet = self.app.store.fleet(fleet_id)
        runtime = self.app.runtime_fleet(fleet)
        settings = clean_settings(fleet.get('progress'))
        now = self.clock()
        backlog = self.app.backlog.snapshot(fleet_id)
        monitor = self.app.status()
        deployment = self.app.deployments.overview(fleet)
        config, source = configuration(fleet)
        rid = self.app.deployments.data['active'].get(fleet_id)
        with self.lock, closing(self.connect()) as db:
            tasks = [json.loads(r['data']) for r in db.execute('SELECT data FROM tasks WHERE fleet=?', (fleet_id,))]
            reports = [json.loads(r['data']) for r in db.execute('SELECT data FROM agent_tasks WHERE fleet=?', (fleet_id,))]
            handoffs = [json.loads(r['data']) for r in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fleet_id,))]
            changes = [dict(r) for r in db.execute("SELECT at,data FROM observations WHERE fleet=? AND category='tracker' AND at>=?", (fleet_id, now - 604800))]
            window = db.execute('SELECT first_at FROM tracking_windows WHERE fleet=? AND source=?', (fleet_id, source)).fetchone()
        tasks = [t for t in tasks if t.get('sourceKey') == source]
        for t in tasks:
            if runtime.get('workflow', {}).get('states'):
                state = project_ticket(t, runtime['workflow'])
                t['stage'] = state['id']
                t['roleIds'] = [r['id'] for r in runtime['roles'] if stable(r, 'role') in state['roles']]
            else:
                key = {'backlog':'coordinator','ready':'writer','active':'writer','integration':'integrator','blocked':'coordinator'}.get(t['stage'])
                t['roleIds'] = [r['id'] for r in runtime['roles'] if r['key'] == key]
            if t.get('releaseId') != rid:
                t['stageObservedAt'] = None
                t['initialStageUnknown'] = True
                t['stateSeconds'] = {}
            t['source'] = 'tracker'
        task_by_url = {task_key(t['url']): t for t in tasks}
        agents, attention = [], []
        for agent in runtime['agents']:
            rs = sorted([r for r in reports if r['agentId'] == agent['id']], key=lambda r:r['at'], reverse=True)
            for r in rs:
                r['bindingCurrent'] = r['releaseId'] == rid and same_binding(r, agent)
                t = task_by_url.get(r['taskUrl'])
                r['trackerState'] = t['state'] if t else None
                r['conflict'] = bool(t and t['state'] == 'closed' and r['kind'] != 'work-finished')
                if not r['bindingCurrent']:
                    continue
                if r['conflict']:
                    attention.append({'kind':'conflict','agentId':agent['id'],'taskUrl':r['taskUrl'],'at':r['at'],'message':'Agent report differs from tracker closure; inspect the evidence.'})
                elif r.get('blockedSince'):
                    attention.append({'kind':'blocked','agentId':agent['id'],'taskUrl':r['taskUrl'],'at':r['blockedSince'],'message':r['reason'], 'nextAction':r['nextAction']})
                elif r['kind'] != 'work-finished' and now - r['at'] >= settings['quietMinutes'] * 60:
                    attention.append({'kind':'quiet','agentId':agent['id'],'taskUrl':r['taskUrl'],'at':r['at'],'message':'No recent meaningful report; inspect activity before intervening.'})
            observed = deepcopy(monitor.get('sessions', {}).get(session_key(agent), {}))
            observed['stale'] = not observed.get('observedAt') or now - observed['observedAt'] > 20
            stopped = not observed['stale'] and observed.get('goalStatus') in ('blocked', 'paused', 'budget_limited', 'usage_limited')
            blocked_report = next((r for r in rs if r.get('bindingCurrent') and r.get('blockedSince') and r.get('trackerState') != 'closed'), None)
            if stopped:
                reason = 'Goal is ' + observed['goalStatus'] + '. ' + (blocked_report['reason'] if blocked_report else 'No current structured blocker reason; inspect activity and task evidence. Preserve the stop.')
                attention.append({'kind':'goal-stop','agentId':agent['id'],'at':None,'message':agent['name'] + ': ' + reason})
            else:
                reason = ('Workflow automation is paused.' if deployment['paused'] else '') or self.app.triggers.hold_reason([agent], monitor, idle_only=True) or self.app.deployments.adoption_hold(fleet_id,[agent['id']])
            availability = {'state':'held' if stopped else 'busy' if observed.get('status') == 'working' else 'held' if reason else 'eligible',
                            'reason':reason or 'Eligible for an idle workflow check; task scope still applies.'}
            agents.append({'id':agent['id'],'name':agent['name'],'roleIds':agent.get('roleIds',[]),'enabled':agent['enabled'],
                           'session':observed,'reports':rs,'availability':availability,'adoption':next((a for a in deployment['adoption'] if a['agentId']==agent['id']),None)})
        for h in handoffs:
            h['awaitingReceipt'] = not h.get('receivedAt') and h['status'] != 'dismissed'
            h['overdue'] = h['awaitingReceipt'] and now - h['createdAt'] >= settings['handoffMinutes'] * 60
            if h['overdue'] or h['status'] == 'needs-attention':
                attention.append({'kind':'handoff','handoffId':h['id'],'agentId':h['targetAgentId'],'taskUrl':h.get('ticket'), 'at':h['createdAt'],
                                  'message':h.get('detail') or 'Handoff has no agent receipt.'})
        from studio_metrics import blocker_category
        execution = {a['taskUrl'] for a in attention if a['kind'] == 'blocked'}
        for t in tasks:
            t['blockerCategory'] = blocker_category(t, t['url'] in execution)
            if t['blockerCategory'] == 'execution' and t['url'] not in execution:
                attention.append({'kind':'tracker-blocked','taskUrl':t['url'],'at':t.get('stageObservedAt'),'message':'Claimed work is blocked in the tracker; inspect the current blocker reason.'})
        for a in deployment['adoption']:
            if a['required'] and (not a['acknowledged'] or not a['bindingMatches'] or a['entryPoint'] != 'installed'):
                attention.append({'kind':'adoption','agentId':a['agentId'],'at':None,'message':'Workflow adoption needs attention: ' + a['name']})
        completed = [(r['at'], json.loads(r['data'])) for r in changes]
        completed = [(at,d) for at,d in completed if d.get('closed') and d.get('sourceKey') == source]
        roles = []
        for role in runtime['roles']:
            role_tasks = [t for t in tasks if role['id'] in t.get('roleIds',[]) and t['state'] != 'closed']
            ids = {a['id'] for a in agents if role['id'] in a['roleIds']}
            roles.append({**role,'queueCount':len(role_tasks),'waitingCount':sum(h['awaitingReceipt'] and h['targetAgentId'] in ids for h in handoffs)})
        metrics = self.metrics.snapshot(fleet, tasks, agents, handoffs, source, rid)
        goals = self.app.goals.snapshot(fleet, monitor, settings['quietMinutes'])
        for g in goals['records']:
            if g['current'] and g['native']['status'] != 'complete' and (g['staleGoal'] or not g.get('taskUrl')):
                attention.append({'kind': 'stale-goal' if g['staleGoal'] else 'goal-link', 'agentId': g['agentId'],
                                  'at': g.get('lastReportAt') or g['firstObservedAt'],
                                  'message': g['agentName'] + ': ' + ('Goal needs evidence review; no automatic clearing.' if g['staleGoal'] else 'Goal has no linked ticket or completion criterion.'),
                                  'nextAction': 'Inspect the goal, blocker owner and resume condition.'})
        return {'fleetId':fleet_id,'observedAt':now,'observationError':self.observation_error,'settings':settings,'agents':agents,'tasks':tasks,'roles':roles,'metrics':metrics,
                'goals':goals,
                'roleLinks':runtime.get('roleLinks',[]),'handoffs':sorted(handoffs,key=lambda h:h['createdAt'],reverse=True),
                'attention':sorted(attention,key=lambda a:a.get('at') or now),'deployment':{**{k:deployment[k] for k in ('active','draftChanged','paused','enabledTriggers','adoption','migrationComplete')},
                    'reportingEnabled':bool(rid and any(p['id']=='progress-reporting' for p in self.app.deployments.release(rid)['spec']['policies'])),
                    'boundedGoalsEnabled':bool(rid and any(p['id']=='bounded-goals' for p in self.app.deployments.release(rid)['spec']['policies']))},
                'tracker':{'url':config['projectUrl'],'observedAt':backlog.get('observedAt'),'stale':backlog.get('stale',False),
                           'partial':backlog.get('partial',False),'error':backlog.get('error',''),'loading':backlog.get('loading',False)},
                'summary':{'openTasks':sum(t['state']!='closed' for t in tasks),'blockedTasks':sum(t['stage']=='blocked' for t in tasks),
                           'executionBlockedTasks':sum(t['blockerCategory']=='execution' for t in tasks),
                           'restrictedTasks':sum(t['blockerCategory']=='restricted' for t in tasks),
                           'deferredBlockedTasks':sum(t['blockerCategory']=='deferred' for t in tasks),
                           'unclaimedBlockedTasks':sum(t['blockerCategory']=='unclaimed' for t in tasks),
                           'observationStartedAt':window['first_at'] if window else None,
                           'closureWindow24h':max(now-86400,now-settings['retentionDays']*86400,window['first_at']) if window else None,
                           'closureWindow7d':max(now-604800,now-settings['retentionDays']*86400,window['first_at']) if window else None,
                           'pendingHandoffs':sum(h['awaitingReceipt'] for h in handoffs),
                           'completed24h':len({d['taskUrl'] for at,d in completed if at>=now-86400}),
                           'completed7d':len({d['taskUrl'] for at,d in completed})},
                'stages':backlog.get('stages',[]),'historyNotice':'Durations begin with observations. Initial stage entry and changes during observation gaps are unknown.'}
