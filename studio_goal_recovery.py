"""Opt-in transient recovery. Native effects use supported RPCs, never SQLite writes."""
from contextlib import closing
from copy import deepcopy
import json
import time
import uuid

from studio_goals import GoalRPC, identity, record_id
from studio_runtime import observe
from studio_telemetry import epoch


def failure(path, turn_id):
    """Require a complete small transcript and a tool-free failed turn.

    Reject unmatched calls anywhere, and all calls in the failed turn, including
    calls whose output might represent an asynchronous job rather than completion.
    No transcript content is persisted.
    """
    if not path or path.stat().st_size > 8_000_000:
        raise ValueError('Complete recovery evidence is unavailable.')
    pending, current, used_tools, result = set(), None, False, None
    for line in path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        p = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        kind = p.get('type')
        if row.get('type') == 'event_msg':
            if kind == 'task_started':
                current, used_tools, result = p.get('turn_id'), False, None
            elif kind == 'error' and current == turn_id:
                info = p.get('codex_error_info')
                code = (info.get('http_connection_failed') or {}).get('http_status_code') if isinstance(info, dict) else None
                if info == 'rate_limit_exceeded' or code == 502:
                    result = {'kind': 'rate-limit' if info == 'rate_limit_exceeded' else 'gateway',
                              'at': epoch(row.get('timestamp'))}
                else:
                    result = None
        elif row.get('type') == 'response_item':
            if kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
                if not p.get('call_id'):
                    raise ValueError('Unknown tool identity.')
                pending.add(p['call_id'])
                if current == turn_id:
                    used_tools = True
            elif kind in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
                output = p.get('output')
                if isinstance(output, str) and any(marker in output.lower() for marker in ('session_id', 'session id', 'script running with cell id')):
                    raise ValueError('An asynchronous tool outcome requires review.')
                pending.discard(p.get('call_id'))
    if current != turn_id or pending or used_tools or not result or not result['at']:
        raise ValueError('No confirmed tool-free transient failure.')
    return result


def preflight(app, fid, aid, now, expected=None, active=False):
    fleet, agent, rid = app.goals.binding(fid, aid)
    if app.deployments.data['paused'].get(fid):
        raise ValueError('Workflow is paused.')
    s = observe(app.codex_root, [agent['sessionId']])['sessions'][agent['sessionId']]
    goal = app.goals.validate_observation(s, time.time())
    from studio_subagents import hold_reason as child_hold
    reason = child_hold(s, require_known=True)
    if reason:
        raise ValueError(reason)
    if (not s.get('pids') or s.get('status') != 'attention' or
            s.get('turnStatus') not in ('failed', 'error') or not s.get('turnCompletedAt') or
            s.get('queueCount') != 0 or goal['status'] != ('active' if active else 'blocked')):
        raise ValueError('Session is not safely recoverable.')
    if expected and (identity(goal), agent, rid, s.get('turnId')) != expected:
        raise ValueError('Goal or binding changed.')
    error = failure(app.telemetry.source(agent), s.get('turnId'))
    if not active and abs(goal.get('updatedAt', 0) - error['at']) > 30:
        raise ValueError('Blocked goal does not coincide with the transient failure.')
    if goal.get('tokenBudget') is not None and goal['tokensUsed'] >= goal['tokenBudget']:
        raise ValueError('Goal budget is exhausted.')
    with closing(app.progress.connect()) as db:
        row = db.execute('SELECT data FROM goal_records WHERE fleet=? AND agent=? AND session=? AND goal=?',
                         (fid, aid, agent['sessionId'], record_id(goal))).fetchone()
    record = json.loads(row['data']) if row else {}
    if identity(record.get('native') or {}) != identity(goal) or not record.get('taskUrl') or record.get('blocker'):
        raise ValueError('Goal is unlinked or has a reported blocker requiring review.')
    # Preserve ordinary dispatch reservations and agent/fleet holds. Only the two
    # states proved above are substituted, locally; global guards stay unchanged.
    checked = deepcopy(s)
    checked.update(status='idle', goalStatus='active')
    monitor = {'observedAt': time.time(), 'sessions': {agent['sessionId']: checked}}
    reason = (app.triggers.hold_reason([agent], monitor, True) or
              app.automation.hold(fid, [agent], monitor, throttle=False))
    if reason or any(j['status'] == 'running' for j in app.jobs.values()):
        raise ValueError(reason or 'A launch is unsettled.')
    return agent, rid, goal, s, error


def recover(app, fleet, rule, agent, now):
    if not app.launch_enabled or not rule.get('enabled'):
        return
    # Lock order matches reviewed goal controls. Intents are persisted before
    # effects; unknown activation/delivery outcomes never trigger another send.
    with app.triggers.lock, app.store.lock, app.controls.lock:
        try:
            a, rid, goal, s, error = preflight(app, fleet['id'], agent['id'], now)
        except (ValueError, OSError, KeyError, TypeError):
            return
        key = 'recovery:' + fleet['id'] + ':' + a['sessionId'] + ':' + record_id(goal) + ':' + s['turnId']
        if key in app.goals.receipts:
            return
        prior = [r for r in app.goals.receipts.values() if r.get('recoveryGoal') == record_id(goal)
                 and r.get('sessionId') == a['sessionId'] and r.get('fleetId') == fleet['id']]
        if any(r['status'] in ('applying', 'unknown', 'failed', 'needs-attention') for r in prior):
            return
        limit = min(3, rule['maxFires'])
        delay = max(rule['cooldownMinutes'] * 60, 900 if error['kind'] == 'rate-limit' else 60) * 2 ** len(prior)
        if now - error['at'] < max(delay, rule['when']['forMinutes'] * 60):
            return
        receipt = {'fleetId': fleet['id'], 'agentId': a['id'], 'sessionId': a['sessionId'],
                   'action': 'resume', 'agentName': a['name'], 'goal': deepcopy(goal),
                   'ruleId': rule.get('id'), 'note': 'Opt-in recovery of a confirmed transient model error.',
                   'recoveryGoal': record_id(goal), 'turnId': s['turnId'], 'status': 'applying', 'startedAt': now}
        app.goals.receipts[key] = receipt
        if len(prior) >= limit:
            receipt.update(status='needs-attention', detail='Automatic goal recovery limit reached; operator review required.')
            app.goals.persist()
            app.automation.data['escalations'].append({
                'id': uuid.uuid4().hex, 'fleetId': fleet['id'], 'agentId': a['id'],
                'agentName': a['name'], 'ruleName': rule.get('name', 'Goal recovery'),
                'at': now, 'message': receipt['detail'], 'acknowledged': False})
            app.automation.persist()
            app.event('Goal recovery needs attention', receipt['detail'], fleet['id'])
            return
        app.goals.persist()
        attempted = False
        try:
            expected = (identity(goal), a, rid, s['turnId'])
            with GoalRPC(app.store.legacy['codexPath']) as rpc:
                before = rpc.call('thread/goal/get', {'threadId': a['sessionId']})['goal']
                if not before or any(before.get(k) != goal.get(k) for k in ('objective', 'createdAt', 'status', 'tokenBudget', 'tokensUsed')):
                    raise ValueError('Native goal changed before recovery.')
                preflight(app, fleet['id'], a['id'], now, expected)
                attempted = True
                rpc.call('thread/goal/set', {'threadId': a['sessionId'], 'status': 'active'})
                after = rpc.call('thread/goal/get', {'threadId': a['sessionId']})['goal']
                if not after or after.get('status') != 'active' or any(after.get(k) != before.get(k) for k in ('objective', 'createdAt', 'tokenBudget', 'tokensUsed')):
                    raise RuntimeError('Goal activation could not be verified.')
                preflight(app, fleet['id'], a['id'], now, expected, active=True)
                status, detail = app.runtimes.for_agent(a).send(a['sessionId'],
                    '[Fleet Studio] Your same native goal was resumed after a confirmed transient model error. '
                    'Continue authorized work. Preserve explicit stops and inspect prior tool outcomes before proceeding.', a['directory'])
                receipt.update(status='queued' if status == 'queued' else 'unknown', detail=detail)
        except Exception as exc:
            receipt.update(status='unknown' if attempted else 'failed', detail=str(exc))
        receipt['finishedAt'] = time.time()
        app.goals.persist()
        app.event('Goal recovery ' + receipt['status'], receipt.get('detail', ''), fleet['id'])
