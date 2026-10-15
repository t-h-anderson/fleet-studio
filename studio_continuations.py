"""One explicitly authorized dependency notification per immutable assignment."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import time

from studio_contracts import current, current_key, digest, delivery_hold
from studio_progress import configuration


def identity(fid, contract):
    return hashlib.sha256((fid + ':' + digest(contract)).encode()).hexdigest()


def invalidated(app, action):
    contract, fid = action['assignmentContract'], action['fleetId']
    if app.triggers.data['currentContracts'].get(current_key(fid, contract)) != contract['id']:
        return True
    from studio_recovery import effective_release
    if effective_release(app, fid, contract) != app.deployments.data['active'].get(fid):
        return True
    with app.progress.lock, closing(app.progress.connect()) as db:
        original = current(db, fid, contract['agentId'], contract['taskUrl'])
    return bool(original and (original.get('status') == 'dismissed' or original.get('submittedAt')))


def references(app, fid):
    """Include active dependency issues even when the visible tracker scope is AI."""
    result = set()
    if not hasattr(app, 'progress'):
        return result
    with app.progress.lock, closing(app.progress.connect()) as db:
        for row in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fid,)):
            h = json.loads(row['data'])
            c = h.get('assignmentContract')
            if (c and not h.get('continuationOf') and h.get('status') != 'dismissed'
                    and app.triggers.data.get('currentContracts', {}).get(current_key(fid, c)) == c['id']):
                result.update(int(url.rsplit('/', 1)[1]) for url in [c['taskUrl']] + c['dependencies'])
    return result


def hold(app, fid, contract, original_id, *, now=None):
    """No refresh or trigger lock here: called under the final dispatch locks."""
    now = time.time() if now is None else now
    option = contract.get('continuation')
    if not option or option.get('authorized') is not True or not contract['dependencies']:
        return 'This assignment has no explicitly authorized dependency continuation.'
    reason = delivery_hold(app, fid, contract)
    if reason:
        return reason
    fleet = app.store.fleet(fid)
    _, source = configuration(fleet)
    rid = app.deployments.data['active'].get(fid)
    with app.progress.lock, closing(app.progress.connect()) as db:
        original = current(db, fid, contract['agentId'], contract['taskUrl'])
        if not original or original['id'] != original_id or not original.get('receivedAt'):
            return 'The current original assignment must have a durable recipient receipt.'
        if original.get('submittedAt'):
            return 'This assignment is already submitted; no continuation will be sent.'
        row = db.execute('SELECT data FROM agent_tasks WHERE fleet=? AND agent=? AND url=?',
                         (fid, contract['agentId'], contract['taskUrl'])).fetchone()
        report = json.loads(row['data']) if row else {}
        if report and (report.get('contractId') != contract['id'] or report.get('releaseId') != rid):
            return 'Assignment progress needs reconciliation before continuing.'
        if report.get('phase') == 'submitted' or report.get('kind') == 'work-finished':
            return 'This assignment is already submitted; no continuation will be sent.'
        if report.get('blockedSince') and (report.get('blockerCategory') != 'dependency'
                                          or set(report.get('dependsOn') or []) != set(contract['dependencies'])):
            return 'The reported blocker is not exactly the authorized dependency set; review it first.'
        saved = db.execute('SELECT * FROM snapshots WHERE fleet=? AND source=?', (fid, source)).fetchone()
        snapshot = json.loads(saved['data']) if saved else {}
        tasks = {r['url']: json.loads(r['data']) for r in db.execute('SELECT * FROM tasks WHERE fleet=?', (fid,))}
    # A failed refresh retains the last successful DB snapshot, so also require
    # the live read cache to corroborate it without refresh errors.
    with app.backlog.lock:
        live = deepcopy(app.backlog.cache.get(fid) or {})
    observed = snapshot.get('observedAt')
    if (observed is None or not 0 <= now - observed <= 180 or snapshot.get('partial')
            or live.get('observedAt') != observed or live.get('error') or live.get('partial') or live.get('loading')):
        return 'Dependencies need a fresh, complete, successful tracker observation.'
    required = [contract['taskUrl']] + contract['dependencies']
    if any(url not in tasks or tasks[url].get('releaseId') != rid or tasks[url].get('sourceKey') != source
           or tasks[url].get('observedAt') != observed for url in required):
        return 'Assigned task and every dependency need observations in the current project and release.'
    if tasks[contract['taskUrl']]['state'] != 'opened':
        return 'The assigned tracker task is no longer open; reconcile it before continuing.'
    from studio_metrics import blocker_category
    if blocker_category(tasks[contract['taskUrl']]) in ('restricted', 'deferred'):
        return 'The assigned tracker task has a restriction or sign-off hold; preserve it.'
    from workflow_spec import project_ticket
    runtime = app.runtime_fleet(fleet)
    for url in contract['dependencies']:
        task = tasks[url]
        state = project_ticket(task, runtime['workflow']) if runtime.get('workflow', {}).get('states') else {}
        if task['state'] != 'closed' or not state.get('acceptsDelivery'):
            return 'Every dependency must satisfy the deployed tracker acceptance rule.'
    return ''


def reconcile(triggers, state, monitor):
    """Periodic fallback also covers acceptance already present at first receipt."""
    app = triggers.app
    if not hasattr(app, 'progress'):
        return
    for fleet in state['fleets']:
        fid = fleet['id']
        with app.progress.lock, closing(app.progress.connect()) as db:
            originals = [json.loads(r['data']) for r in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fid,))]
        for original in originals:
            contract = original.get('assignmentContract')
            if not contract or not contract.get('continuation') or original.get('continuationOf'):
                continue
            key = identity(fid, contract)
            if key in triggers.data['continuations'] or hold(app, fid, contract, original['id']):
                continue
            if not original.get('ruleSignature') or not original.get('mode'):
                continue  # Older incomplete intent evidence needs reconciliation.
            action_id = 'continuation-' + key
            action = {k: deepcopy(original[k]) for k in (
                'triggerId', 'targetAgentId', 'targetName', 'targetSessionId', 'targetDirectory',
                'targetRuntime', 'sourceAgentId', 'deploymentId', 'ruleSignature', 'mode', 'ticket')}
            action.update(id=action_id, fleetId=fid, name='Dependencies accepted',
                          eventId=action_id, continuationOf=original['id'], assignmentContract=deepcopy(contract),
                          createdAt=time.time(), status='pending', cause='Authorized dependencies satisfy acceptance rules')
            action['message'] = (
                'Fleet Studio dependency continuation for assignment ' + contract['id'] + '.\n'
                'The tracker dependencies now satisfy the deployed acceptance rule. This notification does not resume '
                'a stopped goal, change scope or custody, or accept your work. Preserve all explicit stops and source permissions.\n'
                'Task: ' + contract['taskUrl'] + '\nOriginal handoff: ' + original['id'] + '\n\n'
                + contract['continuation']['message'] + '\n\nFleet handoff action ID: ' + action_id
                + '. Report handoff-received with this ID and contractId ' + contract['id'] + '.')
            before = deepcopy(triggers.data)
            triggers.data['actions'].append(action)
            triggers.data['continuations'][key] = {'actionId': action_id, 'originalId': original['id']}
            try:
                triggers.persist()  # Reserve stable identity before any native effect.
            except Exception:
                triggers.data = before
                raise
            triggers.deliver(action, monitor)
