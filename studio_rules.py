"""One view of every rule that can message an agent, and one timeline of what they did.

Routing rules (workflow hand-off triggers), recovery rules (health automations)
and dispatch share this listing and timeline. Each keeps the delivery path it
was proven on: routing through the receipt and contract pipeline in
studio_triggers, recovery and dispatch through studio_automation.
"""
from studio_dispatch import clean_dispatch

KIND_LABELS = {'event': 'reports a hand-off', 'condition': 'changes state', 'timer': 'on a timer', 'idle': 'when idle'}


def routing_rule(fleet, runtime, trigger):
    names = {r['id']: r['name'] for r in fleet.get('roles', [])}
    deployed = next((t for t in runtime.get('triggers', []) if t['id'] == trigger['id']), None)
    if trigger['kind'] == 'event':
        when = (names.get(trigger['sourceRoleId'], 'A role')) + ' reports "' + trigger['eventName'] + '"'
    elif trigger['kind'] == 'condition':
        when = names.get(trigger['sourceRoleId'], 'A role') + ' ' + {'turn-completed': 'finishes a turn'}.get(trigger['condition'], 'is ' + trigger['condition'])
    else:
        when = 'Every ' + str(trigger['minutes']) + ' min, only for agents with open work'
    return {'id': trigger['id'], 'section': 'routing', 'name': trigger['name'], 'when': when,
            'to': names.get(trigger['targetRoleId'], 'Missing role'), 'kind': trigger['kind'], 'mode': trigger['mode'],
            'enabled': trigger['enabled'], 'running': bool(deployed and deployed['enabled']),
            'periodic': trigger['kind'] in ('idle', 'timer'),
            'note': '' if deployed else 'Draft only: deploy the workflow to start it.'}


def list_rules(app, fleet):
    runtime = app.runtime_fleet(fleet)
    return {
        'routing': [routing_rule(fleet, runtime, t) for t in fleet.get('triggers', [])],
        'recovery': [{**r, 'section': 'recovery', 'running': r['enabled'] and not app.deployments.data['paused'].get(fleet['id'])}
                     for r in fleet.get('automations', [])],
        'dispatch': clean_dispatch(fleet.get('dispatch')),
        'paused': app.deployments.data['paused'].get(fleet['id'], False),
    }


def timeline(app, fleet_id, limit=100):
    """Every Studio message to an agent, newest first, whichever rule sent it."""
    rows = []
    for action in app.triggers.snapshot()['actions']:
        if action.get('fleetId') != fleet_id:
            continue
        rows.append({'at': action['createdAt'], 'source': 'routing', 'rule': action['name'], 'agent': action.get('targetName', ''),
                     'action': 'hand-off' if action.get('eventId') else 'message', 'status': action['status'],
                     'detail': action.get('detail', ''), 'cause': action.get('cause', ''), 'outcome': None})
    for fire in app.automation.snapshot(fleet_id)['fires']:
        source = 'dispatch' if fire['ruleId'] in ('dispatch', 'blocked-notify', 'unblock-wake', 'agent-handoff') else 'recovery'
        rows.append({'at': fire['at'], 'source': source, 'rule': fire['ruleName'],
                     'agent': fire['agentName'] + (' → ' + fire['recipientName'] if fire.get('recipientName') else ''),
                     'action': fire['action'], 'status': fire['status'], 'detail': fire.get('detail', ''),
                     'cause': fire.get('evidence', ''), 'outcome': fire.get('outcome')})
    return sorted(rows, key=lambda row: row['at'], reverse=True)[:limit]
