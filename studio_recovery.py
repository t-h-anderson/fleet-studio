"""Join durable delivery and work evidence without replaying unknown effects."""
from contextlib import closing
import json

from studio_bindings import runtime_binding
from studio_progress import task_key


def effective_release(app, fid, contract):
    """Recognize a reviewed policy-only proof without rewriting the envelope."""
    from studio_contracts import current, digest
    rid = app.deployments.data['active'].get(fid)
    if rid == contract['releaseId']:
        return rid
    with app.progress.lock, closing(app.progress.connect()) as db:
        active = current(db, fid, contract['agentId'], contract['taskUrl'])
    if not active or active.get('status') == 'dismissed' or digest(active['assignmentContract']) != digest(contract):
        return None
    proof = next((p for p in reversed(active.get('releaseRevalidations') or [])
                  if p.get('deploymentId') == rid and p.get('contractDigest') == digest(contract)), None)
    return rid if proof and active.get('deploymentId') == rid else None


def replay_hold(app, fid, handoff_id):
    """Only exact obligation evidence can suppress a prepared delivery."""
    with app.progress.lock, closing(app.progress.connect()) as db:
        row = db.execute('SELECT data FROM handoffs WHERE fleet=? AND id=?', (fid, handoff_id)).fetchone()
        if not row:
            return 'The original handoff is not durably observed; reconcile before delivery.'
        handoff = json.loads(row['data'])
        if handoff.get('submittedAt'):
            return 'This exact obligation is already submitted; do not repeat delivery.'
        if handoff.get('receivedAt') or handoff.get('status') == 'reconciled':
            return 'This exact handoff already has a recipient receipt; do not repeat delivery.'
        if handoff.get('status') == 'dismissed':
            return 'This handoff was withdrawn; do not repeat delivery.'
        row = db.execute('SELECT data FROM agent_tasks WHERE fleet=? AND agent=? AND url=?',
                         (fid, handoff['targetAgentId'], task_key(handoff.get('ticket') or ''))).fetchone()
        report = json.loads(row['data']) if row else {}
    original = handoff.get('continuationOf') or handoff_id
    if (report.get('obligationId') == original and report.get('releaseId') == handoff.get('deploymentId')
            and report.get('sessionId') == handoff.get('targetSessionId')
            and report.get('directory') == handoff.get('targetDirectory')
            and runtime_binding(report) == handoff.get('targetRuntime')
            and (report.get('phase') == 'submitted' or report.get('kind') == 'work-finished')):
        return 'This exact obligation is already submitted; do not repeat delivery.'
    return ''


def checkpoint(handoff, report, receipt):
    """Derived facts, never a claim of external exactly-once execution."""
    target = next((t for t in (receipt or {}).get('targets', []) if t['agentId'] == handoff['targetAgentId']), {})
    received = bool(handoff.get('receivedAt'))
    if received:
        delivery = 'received'
    elif handoff.get('status') == 'dismissed':
        delivery = 'withdrawn'
    elif target.get('status') in ('queued', 'started'):
        delivery = 'queued'
    elif target.get('status') == 'held':
        delivery = 'unsent'
    elif receipt or handoff.get('controlId') or handoff.get('status') in ('sending', 'needs-attention'):
        delivery = 'uncertain'
    elif handoff.get('status') == 'queued':
        delivery = 'queued'
    else:
        delivery = 'unsent'
    original = handoff.get('continuationOf') or handoff['id']
    matches = (report and report.get('obligationId') == original
               and report.get('sessionId') == handoff.get('targetSessionId')
               and report.get('directory') == handoff.get('targetDirectory')
               and runtime_binding(report) == handoff.get('targetRuntime')
               and report.get('releaseId') == handoff.get('deploymentId'))
    work = 'submitted' if handoff.get('submittedAt') else (report.get('phase') or 'reported' if matches else 'not-reported')
    return {'handoffId': handoff['id'], 'controlId': handoff.get('controlId'),
            'contractId': (handoff.get('assignmentContract') or {}).get('id'),
            'delivery': delivery, 'work': work, 'evidenceUrl': handoff.get('submissionEvidence') or (report.get('evidenceUrl') if matches else handoff.get('receiptEvidence')),
            'retryable': delivery == 'unsent' and work != 'submitted',
            'nextAction': 'Review receipt evidence; never blindly replay an uncertain delivery.' if delivery == 'uncertain' else
                          'Await review and tracker acceptance.' if work == 'submitted' else
                          'Continue only within current scope and stop conditions.' if received else
                          'Await the recipient receipt.' if delivery == 'queued' else
                          'Normal delivery guards must pass before sending.' if delivery == 'unsent' else 'Obtain a new scoped assignment.'}
