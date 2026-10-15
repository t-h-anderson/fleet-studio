"""Read-only assignment lifecycle from existing reports, receipts and tracker facts."""
from studio_bindings import runtime_binding

PHASES = ('implementing', 'verifying', 'submitted')
LABELS = {'assigned': 'Assigned; receipt pending', 'received': 'Received',
          'implementing': 'Implementing', 'verifying': 'Verifying', 'waiting': 'Waiting',
          'submitted': 'Submitted; acceptance pending', 'accepted': 'Accepted in tracker',
          'closed': 'Closed; acceptance unconfirmed', 'unknown': 'Unknown',
          'conflicting': 'Conflicting evidence', 'unassigned': 'No assignment reported'}


def project_assignments(agent, reports, handoffs, tasks, tracker, release_id, now, quiet_seconds):
    """Never infer ownership or start work from a queued notification or test result."""
    from studio_progress import task_key
    rows = {}
    for report in reports:
        rows.setdefault(report['taskUrl'], {'reports': [], 'handoffs': []})['reports'].append(report)
    for handoff in handoffs:
        if handoff.get('continuationOf'):
            continue
        if handoff.get('targetAgentId') == agent['id'] and handoff.get('ticket') and (handoff.get('status') != 'dismissed' or handoff.get('assignmentContract')):
            rows.setdefault(task_key(handoff['ticket']), {'reports': [], 'handoffs': []})['handoffs'].append(handoff)
    projected = []
    for url, evidence in rows.items():
        current_reports = [r for r in evidence['reports'] if r.get('bindingCurrent')]
        current_handoffs = [h for h in evidence['handoffs'] if release_id and h.get('deploymentId') == release_id
                            and h.get('targetSessionId') == agent.get('sessionId')
                            and h.get('targetDirectory') == agent.get('directory')
                            and (h.get('targetRuntime') or {}) == runtime_binding(agent)]
        report = max(current_reports, key=lambda r: r['at'], default=None)
        handoff = max(current_handoffs, key=lambda h: h['createdAt'], default=None)
        # A later handoff may request different work on the same ticket. Earlier
        # completion/receipt evidence must not complete that new obligation.
        if report and handoff and handoff['createdAt'] > report['at']:
            report = None
        task = tasks.get(url)
        task_fresh = bool(task and tracker.get('observedAt') is not None and task.get('releaseId') == release_id and not tracker.get('stale')
                          and not tracker.get('error') and not tracker.get('partial')
                          and now - task['observedAt'] <= 180)
        item = {'taskUrl': url, 'title': task.get('title', url) if task else url,
                'state': 'unknown', 'source': 'No current assignment evidence', 'evidenceAt': None,
                'evidenceUrl': '', 'owner': '', 'reason': '', 'nextAction': 'Inspect the current task and assignment.',
                'dependsOn': [], 'stale': False, 'waitingObservedSeconds': None, 'waitingObservationGap': False}
        if report:
            item.update(source='Agent report', evidenceAt=report['at'], evidenceUrl=report.get('evidenceUrl', ''),
                        state='waiting' if report.get('blockedSince') else report.get('phase') or
                        {'task-started': 'implementing', 'work-finished': 'submitted'}.get(report['kind'], 'unknown'),
                        reason=report.get('reason', ''), nextAction=report.get('nextAction') or 'Continue within the current assignment.',
                        owner=report.get('blockerOwner', ''), dependsOn=report.get('dependsOn', []),
                        stale=now - report['at'] > quiet_seconds)
            if report.get('blockedSince'):
                item.update(waitingObservedSeconds=report.get('waitingObservedSeconds'),
                            waitingObservationGap=report.get('waitingObservationGap', True) or now - report['at'] > 180)
        elif handoff:
            item.update(source='Handoff receipt' if handoff.get('receivedAt') else 'Handoff delivery',
                        evidenceAt=handoff.get('receivedAt') or handoff['createdAt'],
                        evidenceUrl=handoff.get('receiptEvidence') or url,
                        state='received' if handoff.get('receivedAt') else 'assigned' if handoff['status'] == 'queued' else 'waiting',
                        reason=handoff.get('detail') or ('Recipient has not reported receipt.' if not handoff.get('receivedAt') else ''),
                        owner=agent['name'] if handoff['status'] == 'queued' or handoff.get('receivedAt') else 'Workflow operator',
                        nextAction='Read the task and report receipt.' if handoff['status'] == 'queued' and not handoff.get('receivedAt') else
                        'Confirm current scope before starting.' if handoff.get('receivedAt') else 'Inspect delivery evidence before retrying.',
                        stale=now - (handoff.get('receivedAt') or handoff['createdAt']) > quiet_seconds)
            if handoff['status'] in ('sending', 'needs-attention') and not handoff.get('receivedAt'):
                item.update(state='unknown', reason='Delivery outcome needs reconciliation. ' + item['reason'])
        else:
            item.update(reason='Only earlier release or session evidence is available.', stale=True,
                        nextAction='Read the current deployed assignment; reconcile earlier evidence.')
        if task and task['state'] == 'closed' and (report or handoff):
            obligation_at = report['at'] if report else handoff['createdAt']
            if not task_fresh or task['observedAt'] < obligation_at:
                item.update(state='unknown', stale=True, reason='Retained tracker closure is not current acceptance evidence.',
                            nextAction='Refresh tracker evidence and reconcile the assignment.')
            elif report and item['state'] != 'submitted':
                item.update(state='conflicting', reason='Tracker closure differs from the active assignment report.',
                            nextAction='Reconcile the task closure and current report.')
            elif report and task.get('acceptsDelivery'):
                item.update(state='accepted', source='Tracker acceptance rule', evidenceAt=task['observedAt'],
                            evidenceUrl=url, stale=False, nextAction='Await the next scoped assignment.')
            else:
                item.update(state='closed', source='Tracker closure', evidenceAt=task['observedAt'], evidenceUrl=url,
                            reason='Closure alone does not establish accepted delivery for this assignment.',
                            nextAction='Inspect the acceptance evidence and remaining obligations.')
        if item['state'] == 'submitted':
            item['nextAction'] = report.get('nextAction') or 'Await review and tracker acceptance.'
        typed = [h for h in evidence['handoffs'] if h.get('assignmentContract')]
        if typed:
            from studio_contracts import projection
            superseded = {h['assignmentContract'].get('supersedes') for h in typed}
            current = max((h for h in typed if h['assignmentContract']['id'] not in superseded), key=lambda h: (h['createdAt'], h['id']), default=None)
            if current:
                item['contract'] = projection(current['assignmentContract'], report)
                if current.get('status') == 'dismissed':
                    item.update(state='waiting', reason='The current assignment contract was withdrawn.', owner='Workflow operator',
                                nextAction='Obtain a new scoped contract; earlier contracts remain superseded.')
                elif not report or report.get('contractId') != current['assignmentContract']['id']:
                    if item['state'] in ('implementing', 'verifying', 'submitted', 'accepted'):
                        item.update(state='unknown', reason='This milestone does not identify the current assignment contract.')
                elif item['state'] in ('submitted', 'accepted') and item['contract']['missingRequirements']:
                    item.update(state='unknown', reason='Completion evidence for the current contract is incomplete.')
        item['label'] = LABELS[item['state']]
        projected.append(item)
    projected.sort(key=lambda a: (a['state'] in ('accepted', 'closed'), -(a['evidenceAt'] or 0), a['taskUrl']))
    return projected


def waiting_observation(previous, data, now):
    """Accumulate only short intervals bounded by matching blocker reports."""
    seconds, gap = 0, False
    if previous.get('blockedSince'):
        seconds = previous.get('waitingObservedSeconds', 0)
        elapsed = now - previous['at']
        matching = all(data.get(k) == previous.get(k) for k in ('reason', 'blockerOwner', 'dependsOn'))
        gap = previous.get('waitingObservationGap', False) or not matching or not 0 <= elapsed <= 180
        if matching and 0 <= elapsed <= 180:
            seconds += elapsed
    return seconds, gap
