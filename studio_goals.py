"""Bounded work evidence and reviewed native goal controls. Never writes native DBs."""
from studio_bindings import runtime_binding, session_key, same_binding
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import queue
import subprocess
import threading
import time
import uuid

from studio_progress import configuration, link, plain, task_key
from studio_trackers import issue_prefix
from studio_runtime import normalize_goal_status, observe

BLOCKERS = ('dependency', 'approval', 'restricted', 'host', 'capacity', 'other')
DETAILS = ('completionCriteria', 'blockerCategory', 'blockerOwner', 'resumeCondition')
POLICY = {'id': 'bounded-goals', 'name': 'Bounded goals and reviewed recovery', 'roles': [], 'sources': [],
          'text': 'Use native goals only for explicitly authorized, bounded work packets with a tracker ticket and a concrete completion criterion. A role is a continuing responsibility, not a permanent goal. Do not create a goal merely to occupy an idle role. Link the exact native goal to its ticket in Fleet Studio. Report meaningful progress and structured blockers with category, reason, dependencies, blocker owner and resume condition. Finish a goal only when its own completion criterion is met; handing off does not establish downstream acceptance or issue closure. Preserve explicit stops, source restrictions, custody and runtime ownership. Stale goals require evidence review: never automatically clear, complete, cancel or resume them to make an agent available. An explicitly enabled transient-goal-error recovery rule may resume the same blocked goal after a confirmed model transport or rate-limit failure, subject to settled-tool checks, preserved holds and bounded attempts. Reviewed complete/cancel/resume actions require evidence and an explicit operator decision. Receipts, policy acknowledgements and blocker reports can be recorded while work is stopped; they do not resume technical work. If a dependency resolves, report that fact and retain the stop until an authorized resume. Native goal budgets and usage limits remain in force.'}


def identity(goal):
    return (goal.get('id'), goal.get('createdAt'), goal.get('objective'))


def record_id(goal):
    return hashlib.sha256(json.dumps(identity(goal)).encode()).hexdigest()


class GoalRPC:
    """A metadata-only app-server connection; no thread/start, load or resume."""
    def __init__(self, codex):
        self.codex = codex

    def __enter__(self):
        self.process = subprocess.Popen([self.codex, 'app-server', '--stdio'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.responses, self.sequence = queue.Queue(), 0
        def reader():
            try:
                for line in self.process.stdout:
                    try:
                        self.responses.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                self.responses.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'fleet_studio', 'version': '2.0.0'},
                                     'capabilities': {'experimentalApi': True}})
            self.write({'method': 'initialized', 'params': {}})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def write(self, data):
        self.process.stdin.write(json.dumps(data) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        self.write({'id': self.sequence, 'method': method, 'params': params})
        deadline = time.monotonic() + 25
        while True:
            try:
                row = self.responses.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('Codex did not acknowledge the goal operation.') from None
            if row is None:
                raise RuntimeError('Codex app-server closed before acknowledgement.')
            if row.get('id') == self.sequence:
                if 'error' in row:
                    raise RuntimeError(row['error'].get('message', 'Codex rejected the goal operation.'))
                return row.get('result')
            if time.monotonic() >= deadline:
                raise TimeoutError('Codex did not acknowledge the goal operation.')

    def __exit__(self, *_):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=3)
        self.process.stdout.close()


class Goals:
    def __init__(self, app):
        self.app, self.previews = app, {}
        self.path = app.root / '.runtime' / 'goal-actions.json'
        self.receipts = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        for row in self.receipts.values():
            if row['status'] == 'applying':
                row.update(status='unknown', detail='Studio restarted during the operation. Inspect the native goal; do not retry blindly.')
        self.persist()
        with closing(app.progress.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS goal_records (fleet TEXT, agent TEXT, session TEXT, goal TEXT, data TEXT, PRIMARY KEY(fleet,agent,session,goal))')

    def persist(self):
        from fleet_studio import atomic_json
        atomic_json(self.path, self.receipts)

    def binding(self, fid, aid):
        fleet = self.app.store.fleet(fid)
        rid = self.app.deployments.data['active'].get(fid)
        if not rid:
            raise ValueError('Deploy a workflow before managing goals.')
        agent = next((a for a in fleet['agents'] if a['id'] == aid), None)
        deployed = next((a for a in self.app.deployments.release(rid)['fleet']['agents'] if a['id'] == aid), None)
        if not agent or not deployed or not agent['enabled'] or not deployed['enabled'] or not agent['sessionId'] or not same_binding(agent, deployed, ('sessionId', 'directory', 'roleIds', 'notes')):
            raise ValueError('Agent binding differs from the active deployment.')
        if runtime_binding(agent):
            raise ValueError('Goal records and controls require a local Codex agent.')
        return fleet, agent, rid

    @staticmethod
    def validate_observation(observed, now):
        goal = observed.get('goal')
        if not observed.get('observedAt') or not 0 <= now - observed['observedAt'] <= 20 or not observed.get('goalKnown') or not goal or not goal.get('id') or not goal.get('createdAt') or observed.get('error'):
            raise ValueError('Current native goal identity is unavailable or stale. Refresh before continuing.')
        return goal

    def observe(self, fleets, monitor):
        p = self.app.progress
        with p.lock, closing(p.connect()) as db, db:
            for fleet in fleets:
                for agent in fleet['agents']:
                    if runtime_binding(agent):
                        continue  # Native goal management is local Codex only.
                    observed = monitor.get('sessions', {}).get(session_key(agent), {})
                    try:
                        goal = self.validate_observation(observed, p.clock())
                    except ValueError:
                        continue
                    key = (fleet['id'], agent['id'], agent['sessionId'], record_id(goal))
                    prior = db.execute('SELECT data FROM goal_records WHERE fleet=? AND agent=? AND session=? AND goal=?', key).fetchone()
                    data = json.loads(prior['data']) if prior else {'firstObservedAt': p.clock()}
                    # Same native ID with a replaced objective must not inherit a ticket or blocker.
                    if data.get('native') and identity(data['native']) != identity(goal):
                        data = {'firstObservedAt': p.clock()}
                    old = data.get('native', {})
                    data.update(native=goal, agentId=agent['id'], agentName=agent['name'], sessionId=agent['sessionId'],
                                directory=agent['directory'], roleIds=agent.get('roleIds', []), observedAt=observed['observedAt'])
                    db.execute('INSERT OR REPLACE INTO goal_records VALUES(?,?,?,?,?)', (*key, json.dumps(data)))
                    if identity(old) != identity(goal) or old.get('status') != goal['status']:
                        p.record(db, fleet['id'], 'goal-observation', uuid.uuid4().hex, p.clock(), data)

    def link_report(self, db, fleet, agent, data):
        """Attach evidence to the explicitly named native goal inside the report transaction."""
        with self.app.lock:
            observed = deepcopy(self.app.monitor.get('sessions', {}).get(session_key(agent), {}))
        goal = self.validate_observation(observed, self.app.progress.clock())
        if goal['id'] != data['goalId']:
            raise ValueError('Goal changed. Read its current identity before linking evidence.')
        key = (fleet['id'], agent['id'], agent['sessionId'], record_id(goal))
        prior = db.execute('SELECT data FROM goal_records WHERE fleet=? AND agent=? AND session=? AND goal=?', key).fetchone()
        record = json.loads(prior['data']) if prior else {'firstObservedAt': data['at']}
        if record.get('native') and identity(record['native']) != identity(goal):
            record = {'firstObservedAt': data['at']}
        if record.get('taskUrl') and record['taskUrl'] != data['taskUrl']:
            raise ValueError('This goal already belongs to another ticket. Use a new bounded goal for new work.')
        criteria = data.get('completionCriteria') or record.get('completionCriteria')
        if not criteria:
            raise ValueError('completionCriteria is required when first linking a goal.')
        record.update(native=goal, agentId=agent['id'], agentName=agent['name'], sessionId=agent['sessionId'],
                      directory=agent['directory'], roleIds=agent.get('roleIds', []), observedAt=observed['observedAt'],
                      taskUrl=data['taskUrl'], completionCriteria=criteria, lastReportAt=data['at'],
                      evidenceUrl=data['evidenceUrl'], releaseId=data['releaseId'], summary=data['summary'])
        if data['kind'] == 'blocked':
            record['blocker'] = {k: data[k] for k in ('reason', 'nextAction', 'dependsOn', 'blockerCategory', 'blockerOwner', 'resumeCondition')}
            record['blocker']['reportedAt'] = data['at']
        elif data['kind'] == 'unblocked' and record.get('blocker'):
            record['blocker']['resolvedAt'] = data['at']
            record['blocker']['resolutionEvidence'] = data['evidenceUrl']
        if data['kind'] == 'work-finished':
            record['workFinishedAt'] = data['at']
        db.execute('INSERT OR REPLACE INTO goal_records VALUES(?,?,?,?,?)', (*key, json.dumps(record)))

    def link_metadata(self, payload):
        with self.app.store.lock:
            fleet, agent, rid = self.binding(payload.get('fleetId'), payload.get('agentId'))
            if payload.get('releaseId') != rid or payload.get('sessionId') != agent['sessionId']:
                raise ValueError('Assignment changed. Refresh before linking a goal.')
            data = {'goalId': plain(payload.get('goalId'), 'goalId', 200, True),
                    'taskUrl': task_key(link(payload.get('taskUrl'), 'taskUrl')),
                    'evidenceUrl': link(payload.get('evidenceUrl'), 'evidenceUrl'),
                    'completionCriteria': plain(payload.get('completionCriteria'), 'completionCriteria', required=True),
                    'summary': plain(payload.get('summary', ''), 'summary'), 'kind': 'goal-linked',
                    'source': 'operator-link', 'agentId': agent['id'], 'releaseId': rid, 'at': self.app.progress.clock()}
            config, _ = configuration(fleet)
            if config['projectUrl'] and not data['taskUrl'].startswith(task_key(issue_prefix(config)) + '/'):
                raise ValueError('Task must belong to the configured tracker project.')
            with self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
                self.link_report(db, fleet, agent, data)
                self.app.progress.record(db, fleet['id'], 'goal-link', uuid.uuid4().hex, data['at'], data)
            return data

    def snapshot(self, fleet, monitor, quiet_minutes):
        p = self.app.progress
        with p.lock, closing(p.connect()) as db:
            records = [json.loads(r['data']) for r in db.execute('SELECT data FROM goal_records WHERE fleet=?', (fleet['id'],))]
        agents = {a['id']: a for a in fleet['agents']}
        for r in records:
            a = agents.get(r['agentId'])
            s = monitor.get('sessions', {}).get(r['sessionId'], {})
            r['bindingCurrent'] = bool(a and not runtime_binding(a) and a['sessionId'] == r['sessionId'] and a['directory'] == r['directory'] and a.get('roleIds', []) == r.get('roleIds', []))
            r['observationFresh'] = bool(s.get('goalKnown') and not s.get('error') and 0 <= p.clock() - (s.get('observedAt') or 0) <= 20)
            r['current'] = bool(r['bindingCurrent'] and r['observationFresh'] and identity(s.get('goal') or {}) == identity(r['native']))
            age = p.clock() - (r.get('lastReportAt') or r['firstObservedAt'])
            r['staleGoal'] = bool(r['current'] and r['native']['status'] != 'complete' and age >= quiet_minutes * 60)
        with self.app.store.lock:
            receipts = deepcopy([r for r in self.receipts.values() if r['fleetId'] == fleet['id']])
        return {'records': sorted(records, key=lambda r:r['observedAt'], reverse=True), 'actions': receipts[-50:][::-1]}

    def preflight(self, fid, aid):
        fleet, agent, rid = self.binding(fid, aid)
        if agent.get('backend', 'codex') != 'codex' or agent.get('hostId', 'local') != 'local':
            raise ValueError('Native goal controls are available only for local Codex agents.')
        sid = agent['sessionId']
        observed = observe(self.app.codex_root, [sid])['sessions'].get(sid, {})
        goal = self.validate_observation(observed, time.time())
        from studio_subagents import hold_reason as child_hold
        reason = child_hold(observed, require_known=True)
        if reason:
            raise ValueError(reason)
        if observed.get('status') != 'closed' or observed.get('pids') or observed.get('turnStatus') == 'inProgress' or observed.get('queueCount') != 0:
            raise ValueError('Close the agent session at a safe boundary and empty its queue before changing a goal.')
        if any(r['status'] == 'sending' and any(t['sessionId'] == sid for t in r.get('targets', [])) for r in self.app.controls.receipts.values()) or any(j['status'] == 'running' for j in self.app.jobs.values()):
            raise ValueError('Wait for active controls and launches to settle.')
        return fleet, agent, rid, goal

    def preview(self, payload):
        with self.app.triggers.lock, self.app.store.lock, self.app.controls.lock:
            fleet, agent, rid, goal = self.preflight(payload.get('fleetId'), payload.get('agentId'))
            action = payload.get('action')
            if action not in ('complete', 'cancel', 'resume'):
                raise ValueError('Choose complete, cancel or resume.')
            if goal['status'] == 'complete' or action == 'resume' and goal['status'] not in ('blocked', 'paused'):
                raise ValueError('This action is not valid for the current goal. Budget and usage limits cannot be overridden here.')
            evidence = link(payload.get('evidenceUrl'), 'Decision evidence')
            note = plain(payload.get('note'), 'Decision and authority', required=True)
            with closing(self.app.progress.connect()) as db:
                row = db.execute('SELECT data FROM goal_records WHERE fleet=? AND agent=? AND session=? AND goal=?',
                                 (fleet['id'], agent['id'], agent['sessionId'], record_id(goal))).fetchone()
            record = json.loads(row['data']) if row else {}
            if not record.get('taskUrl') or identity(record.get('native') or {}) != identity(goal):
                raise ValueError('Link this exact goal to its ticket and completion criterion before reviewing an action.')
            if task_key(evidence) != record['taskUrl']:
                raise ValueError('Use decision evidence on the linked ticket.')
            if payload.get('confirmed') is not True:
                raise ValueError('Confirm the completion evidence, cancellation authority or authorized resume condition.')
            p = {'id': uuid.uuid4().hex, 'createdAt': time.time(), 'fleetId': fleet['id'], 'agentId': agent['id'],
                 'agentName': agent['name'], 'sessionId': agent['sessionId'], 'goal': goal, 'action': action,
                 'revision': self.app.store.snapshot()['revision'], 'releaseId': rid, 'record': record,
                 'evidenceUrl': evidence, 'note': note,
                 'effect': {'complete': 'Mark only this goal complete; preserve native usage history.',
                            'cancel': 'Clear this native goal and retain its objective, usage and evidence in Studio history.',
                            'resume': 'Set this goal active without changing its objective or budget. The next session run can continue authorized work.'}[action],
                 'caution': 'Keep this session closed during apply. Codex has no atomic expected-goal check; another client could change it between the final check and write. No tracker state, acceptance or session launch is changed.'}
            self.previews = {k:v for k,v in self.previews.items() if time.time() - v['createdAt'] < 300}
            self.previews[p['id']] = p
            return deepcopy(p)

    def apply(self, preview_id):
        if not self.app.launch_enabled:
            raise ValueError('Native goal controls are disabled in this test instance.')
        with self.app.triggers.lock, self.app.store.lock, self.app.controls.lock:
            if preview_id in self.receipts:
                return deepcopy(self.receipts[preview_id])
            p = self.previews.get(preview_id)
            if not p or time.time() - p['createdAt'] > 300:
                raise ValueError('Goal preview expired. Review it again.')
            _, agent, rid, goal = self.preflight(p['fleetId'], p['agentId'])
            if p['revision'] != self.app.store.snapshot()['revision'] or rid != p['releaseId'] or agent['sessionId'] != p['sessionId'] or goal != p['goal']:
                raise ValueError('Goal, binding or release changed. Review it again.')
            with closing(self.app.progress.connect()) as db:
                row = db.execute('SELECT data FROM goal_records WHERE fleet=? AND agent=? AND session=? AND goal=?',
                                 (p['fleetId'], p['agentId'], p['sessionId'], record_id(goal))).fetchone()
            current = json.loads(row['data']) if row else {}
            # Polling refreshes observedAt; substantive report changes invalidate the decision.
            if {k:v for k,v in current.items() if k != 'observedAt'} != {k:v for k,v in p['record'].items() if k != 'observedAt'}:
                raise ValueError('Goal evidence changed. Review it again.')
            receipt = {**deepcopy(p), 'status': 'applying', 'startedAt': time.time()}
            self.receipts[preview_id] = receipt
            self.persist()  # Durable intent before native effects; never silently retry.
            attempted = False
            try:
                with GoalRPC(self.app.store.legacy['codexPath']) as rpc:
                    before = rpc.call('thread/goal/get', {'threadId': p['sessionId']})['goal']
                    if not before or before['objective'] != goal['objective'] or normalize_goal_status(before['status']) != goal['status'] or before.get('tokensUsed') != goal.get('tokensUsed') or before.get('tokenBudget') != goal.get('tokenBudget'):
                        raise ValueError('Native goal changed during review.')
                    # Recheck exact DB identity and session state after starting the metadata server.
                    if self.preflight(p['fleetId'], p['agentId'])[3] != goal:
                        raise ValueError('Native goal changed before apply.')
                    params = {'threadId': p['sessionId']}
                    method = 'thread/goal/clear' if p['action'] == 'cancel' else 'thread/goal/set'
                    if p['action'] != 'cancel':
                        params['status'] = 'active' if p['action'] == 'resume' else 'complete'
                    attempted = True
                    rpc.call(method, params)
                    after = rpc.call('thread/goal/get', {'threadId': p['sessionId']})['goal']
                    matches = after is None if p['action'] == 'cancel' else bool(after and after['objective'] == before['objective'] and after['createdAt'] == before['createdAt'] and after['status'] == params['status'] and after.get('tokenBudget') == before.get('tokenBudget') and after['tokensUsed'] == before['tokensUsed'])
                    if not matches:
                        raise RuntimeError('Native state after apply did not match the reviewed result.')
                    receipt.update(status='applied', detail='Codex acknowledged and the resulting goal was verified.', nativeBefore=before, nativeAfter=after)
            except Exception as exc:
                receipt.update(status='unknown' if attempted else 'failed', detail=str(exc) + (' Inspect native state before any new action; no automatic retry.' if attempted else ' No goal change was sent.'))
            receipt['finishedAt'] = time.time()
            self.persist()
            return deepcopy(receipt)
