"""One view of every rule that can message an agent, and one timeline of what they did.

Routing rules (workflow hand-off triggers), recovery rules (health automations)
and dispatch share this listing and timeline. Each keeps the delivery path it
was proven on: routing through the receipt and contract pipeline in
studio_triggers, recovery and dispatch through studio_automation.
"""
import re

from studio_dispatch import clean_dispatch

KIND_LABELS = {'event': 'reports a hand-off', 'condition': 'changes state', 'timer': 'on a timer', 'idle': 'when idle'}


def routing_rule(fleet, runtime, trigger):
    names = {r['id']: r['name'] for r in fleet.get('roles', [])}
    deployed = next((t for t in runtime.get('triggers', []) if t['id'] == trigger['id']), None)
    if trigger['kind'] == 'event':
        # Generated event names ("handoff-9e32e493") mean nothing to a person; say what happens instead.
        generated = re.fullmatch(r'handoff-[0-9a-f]{6,}', trigger['eventName'])
        when = names.get(trigger['sourceRoleId'], 'A role') + (' hands off' if generated else ' reports "' + trigger['eventName'] + '"')
    elif trigger['kind'] == 'condition':
        when = names.get(trigger['sourceRoleId'], 'A role') + ' ' + {'turn-completed': 'finishes a turn'}.get(trigger['condition'], 'is ' + trigger['condition'])
    else:
        when = 'Every ' + str(trigger['minutes']) + ' min, only for agents with open work'
    return {'id': trigger['id'], 'section': 'routing', 'name': trigger['name'], 'when': when,
            'to': names.get(trigger['targetRoleId'], 'Missing role'), 'kind': trigger['kind'], 'mode': trigger['mode'],
            'enabled': trigger['enabled'], 'running': bool(deployed and deployed['enabled']),
            'linkId': trigger.get('workflowLinkId', ''),
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
                     'detail': action.get('detail', ''), 'cause': action.get('cause', ''), 'outcome': None, 'actionId': action['id']})
    for fire in app.automation.snapshot(fleet_id)['fires']:
        source = 'dispatch' if fire['ruleId'] in ('dispatch', 'blocked-notify', 'unblock-wake', 'agent-handoff') else 'recovery'
        rows.append({'at': fire['at'], 'source': source, 'rule': fire['ruleName'],
                     'agent': fire['agentName'] + (' → ' + fire['recipientName'] if fire.get('recipientName') else ''),
                     'action': fire['action'], 'status': fire['status'], 'detail': fire.get('detail', ''),
                     'cause': fire.get('evidence', ''), 'outcome': fire.get('outcome')})
    return sorted(rows, key=lambda row: row['at'], reverse=True)[:limit]


SLOW_MINUTES = 15
WAITING = ('pending', 'held', 'sending', 'needs-attention')


def flow(app, fleet):
    """Live numbers for the design board: per role, per hand-off and the hotspots worth a look."""
    from studio_dispatch import clean_dispatch
    from studio_health import PROBLEM_STATES
    from studio_bindings import session_key
    now = app.dispatch.clock()
    agents = {a['id']: a for a in fleet['agents']}
    roles = {r['id']: {'waiting': 0, 'stuck': 0, 'working': 0, 'claimed': 0, 'throttled': 0,
                       'tokens24h': 0, 'wasteTokens24h': 0, 'agents': []} for r in fleet.get('roles', [])}
    sessions = app.status().get('sessions', {})
    with app.automation.lock:
        throttled = {key.split(':', 1)[1] for key in app.automation.data['throttled'] if key.startswith(fleet['id'] + ':')}
    for agent in fleet['agents']:
        health = app.health.get(fleet['id'], agent['id']) or {}
        for role_id in agent.get('roleIds', []):
            if role_id in roles:
                row = roles[role_id]
                row['stuck'] += health.get('state') in PROBLEM_STATES
                # Same source as the header's count: a session processing a turn right now.
                row['working'] += sessions.get(session_key(agent), {}).get('status') == 'working'
                row['throttled'] += agent['id'] in throttled
                row['tokens24h'] += health.get('tokens24h') or 0
                row['wasteTokens24h'] += health.get('wasteTokens24h') or 0
                row['agents'].append({'id': agent['id'], 'name': agent['name'], 'state': health.get('state') or 'unknown'})
    try:
        waiting = app.dispatch.tickets_by_role(fleet, clean_dispatch(fleet.get('dispatch')))
    except Exception:
        waiting = []
    for _, role_ids in waiting:
        for role_id in role_ids:
            if role_id in roles:
                roles[role_id]['waiting'] += 1
    for row in app.dispatch.claims(fleet['id']).values():
        if row['status'] != 'done' and row['agent'] in agents:
            for role_id in agents[row['agent']].get('roleIds', []):
                if role_id in roles:
                    roles[role_id]['claimed'] += 1

    # Role-pair edges keep every waiting delivery, including ones no single hand-off explains.
    edges = {}
    def add(source_agent, target_agent, at):
        source, target = agents.get(source_agent), agents.get(target_agent)
        if not source or not target:
            return
        for s in source.get('roleIds', []):
            for t in target.get('roleIds', []):
                edge = edges.setdefault(s + '>' + t, {'pending': 0, 'oldestAt': None})
                edge['pending'] += 1
                edge['oldestAt'] = min(edge['oldestAt'] or at, at)

    # Per hand-off: deliveries found through their trigger, or the one link joining two roles.
    links = {l['id']: {'pending': 0, 'held': 0, 'attention': 0, 'oldestAt': None, 'recent': 0, 'items': []}
             for l in fleet.get('roleLinks', [])}
    by_trigger = {t['id']: t['workflowLinkId'] for t in fleet.get('triggers', []) if t.get('workflowLinkId') in links}
    joining = {}
    for link in fleet.get('roleLinks', []):
        joining.setdefault((link['source'], link['target']), []).append(link['id'])
    def pair_link(source_agent, target_agent):
        source, target = agents.get(source_agent), agents.get(target_agent)
        if not source or not target:
            return None
        found = {l for s in source.get('roleIds', []) for t in target.get('roleIds', []) for l in joining.get((s, t), [])}
        return found.pop() if len(found) == 1 else None
    def count(link_id, item_id, status, source_agent, target_agent, target_name, at, delivered):
        if not link_id:
            return
        row = links[link_id]
        if status in WAITING:
            row[{'held': 'held', 'needs-attention': 'attention'}.get(status, 'pending')] += 1
            row['oldestAt'] = min(row['oldestAt'] or at, at)
            row['items'].append({'actionId': item_id, 'status': status, 'at': at,
                                 'from': agents.get(source_agent, {}).get('name', ''),
                                 'to': target_name or agents.get(target_agent, {}).get('name', '')})
        elif delivered and at >= now - 86400:
            row['recent'] += 1

    for fire in app.automation.snapshot(fleet['id'])['fires']:
        if not fire.get('recipientId'):
            continue
        if fire['status'] in ('pending', 'sending', 'held'):
            add(fire['agentId'], fire['recipientId'], fire['at'])
        # Recovery hand-offs are not routing actions, so they are never reviewed from the board.
        count(pair_link(fire['agentId'], fire['recipientId']), '', fire['status'], fire['agentId'], fire['recipientId'],
              fire.get('recipientName', ''), fire['at'], fire['status'] == 'sent')
    with app.triggers.lock:
        actions = [dict(a) for a in app.triggers.data['actions'] if a.get('fleetId') == fleet['id']]
    for action in actions:
        if action['status'] in WAITING and action.get('sourceAgentId'):
            add(action['sourceAgentId'], action.get('targetAgentId'), action['createdAt'])
        count(by_trigger.get(action.get('triggerId')), action['id'], action['status'], action.get('sourceAgentId'),
              action.get('targetAgentId'), action.get('targetName', ''), action['createdAt'], action['status'] == 'queued')
    for row in links.values():
        row['items'] = sorted(row['items'], key=lambda item: item['at'])[:10]

    queued = max(roles, key=lambda r: roles[r]['waiting'], default=None)
    slow = [(row['oldestAt'], link_id) for link_id, row in links.items() if row['oldestAt'] and row['oldestAt'] <= now - SLOW_MINUTES * 60]
    hot = {'role': queued if queued and roles[queued]['waiting'] else None, 'link': min(slow)[1] if slow else None,
           'slowMinutes': SLOW_MINUTES}
    return {'roles': roles, 'edges': edges, 'links': links, 'hot': hot, 'observedAt': now}
