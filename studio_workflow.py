"""Populate explicit workflow designs; never dispatch instructions on import."""
import hashlib
import json
import uuid
from studio_roles import make_role


def signature(link):
    return hashlib.sha256(json.dumps({k: link.get(k, '') for k in
        ('source', 'target', 'label', 'trigger', 'artifact', 'condition')}, sort_keys=True).encode()).hexdigest()


def internal(role):
    return role.get('kind') == 'subagent' or role.get('key') == 'subagent-reviewers' or (role.get('baseline') or {}).get('name', '').lower().startswith('subagent reviewers')


def populate_roles(fleet):
    definitions = {
        'Review': ('Audit / review', 'Investigate explicitly assigned audit and review tickets, reconcile evidence, and return concrete findings to the assigning owner.'),
        'Data': ('Material data', 'Handle explicitly assigned material-data work and report the evidence, unresolved data questions, and handoff to the assigning owner.'),
        'Operations': ('Repository maintenance', 'Handle explicitly assigned repository maintenance and cleanup work, preserving active work and required evidence.'),
    }
    created = []
    for agent in fleet['agents']:
        if agent.get('roleIds') or agent.get('role') not in definitions:
            continue
        name, responsibilities = definitions[agent['role']]
        role = next((r for r in fleet['roles'] if r['name'] == name), None)
        if not role:
            role = make_role({'name': name, 'responsibilities': responsibilities,
                              'authority': 'Local fleet assignment. Current ticket scope and project policy govern authority; this role grants no additional acceptance, merge, source-access or cleanup permission.'}, len(fleet['roles']), False)
            role['position'] = {'x': 80 + len(created) * 360, 'y': min(4000, max((r['position']['y'] for r in fleet['roles']), default=0) + 240)}
            fleet['roles'].append(role)
            created.append(name)
        agent['roleIds'] = [role['id']]
    return created


# How a hand-off is delivered once deployed: sent straight to the next role, held for the operator, or not at all.
DELIVERIES = ('automatic', 'review', 'off')
GENERATED = 'Workflow action: '
TRIGGER_LIMIT = 100


def handoff_message(link, roles):
    return (GENERATED + link['label'] + '\nFrom: ' + roles[link['source']]['name'] +
            '\nTo: ' + roles[link['target']]['name'] +
            '\nWhen: ' + (link['trigger'] or 'Upstream agent explicitly reports the handoff.') +
            '\nRequired artifact: ' + (link['artifact'] or 'Read the current ticket handoff.') +
            '\nRequired condition: ' + (link['condition'] or 'Confirm the current ticket scope and acceptance.') +
            '\n\nRead the reported ticket and current handoff before acting. Confirm named custody, scope, evidence and explicit stops. A reported event is a notification, not proof of acceptance. Record work in the existing ticket/MR; do not invent missing assignments or transfer another agent\'s runtime.')


def handoff_trigger(link, roles):
    """The routing trigger that delivers a hand-off. Its ID derives from the link, so it is the same on every save and import."""
    return {
        'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'fleet-studio:handoff:' + link['id'])), 'name': link['label'][:100], 'kind': 'event',
        'sourceRoleId': link['source'], 'targetRoleId': link['target'],
        'eventName': 'handoff-' + link['id'][:8], 'condition': 'turn-completed',
        'minutes': 30, 'enabled': False, 'mode': 'automatic', 'recipientMode': 'single',
        'workflowLinkId': link['id'], 'workflowSignature': signature(link), 'message': handoff_message(link, roles),
    }


def link_delivery(link, trigger):
    """A link's delivery, taking it from its existing trigger when the link predates the setting."""
    if link.get('delivery') in DELIVERIES:
        return link['delivery']
    if trigger and trigger.get('enabled'):
        return trigger.get('mode', 'automatic')
    return 'off'


def sync_handoff_triggers(roles, links, triggers):
    """Make each hand-off's routing trigger follow the hand-off. Returns the new trigger list; idempotent.

    The link is the single source of truth: its delivery decides whether the trigger is enabled and how,
    a changed link refreshes its trigger instead of leaving it stale, and a removed link takes its trigger
    with it. Unchanged links leave their triggers byte-identical, so deployed digests do not move.
    """
    by_role = {r['id']: r for r in roles}
    by_link = {l['id']: l for l in links}
    output, claimed = [], set()
    for trigger in triggers:
        link_id = trigger.get('workflowLinkId')
        if not link_id:
            output.append(trigger)
            continue
        if link_id not in by_link:
            continue  # Its hand-off was removed.
        if link_id in claimed:
            # A second trigger for the same hand-off is an ordinary trigger from now on.
            output.append({k: v for k, v in trigger.items() if k not in ('workflowLinkId', 'workflowSignature')})
            continue
        claimed.add(link_id)
        output.append(trigger)
    current = {t['workflowLinkId']: t for t in output if t.get('workflowLinkId')}
    for link in links:
        trigger = current.get(link['id'])
        endpoints = (by_role.get(link['source']), by_role.get(link['target']))
        delivery = 'off' if any(internal(role) for role in endpoints if role) else link_delivery(link, trigger)
        link['delivery'] = delivery
        if trigger is None:
            if delivery == 'off':
                continue
            trigger = handoff_trigger(link, by_role)
            output.append(trigger)
        if (trigger.get('workflowSignature') != signature(link) or trigger['sourceRoleId'] != link['source']
                or trigger['targetRoleId'] != link['target']):
            # The hand-off changed: carry the change into its trigger rather than leaving it stale.
            trigger.update(sourceRoleId=link['source'], targetRoleId=link['target'], name=link['label'][:100],
                           workflowSignature=signature(link))
            trigger.pop('specContract', None)
        # A still-generated message also follows role renames, which the signature does not cover.
        if trigger.get('message', '').startswith(GENERATED):
            trigger['message'] = handoff_message(link, by_role)
        trigger['enabled'] = delivery != 'off'
        if delivery != 'off':
            trigger['mode'] = delivery
    if len(output) > TRIGGER_LIMIT:
        raise ValueError('At most ' + str(TRIGGER_LIMIT) + ' triggers, including one per hand-off that has been delivered; remove hand-offs or triggers under Advanced.')
    return output


def populate_triggers(fleet):
    """Give every hand-off without a trigger automatic delivery. Kept for older clients."""
    roles = {r['id']: r for r in fleet['roles']}
    existing = {t.get('workflowLinkId') for t in fleet.get('triggers', [])}
    created, skipped = [], []
    for link in fleet['roleLinks']:
        if link['id'] in existing:
            continue
        if internal(roles[link['source']]) or internal(roles[link['target']]):
            skipped.append(link['label'])
            continue
        link['delivery'] = 'automatic'
        created.append(link['label'])
    fleet['triggers'] = sync_handoff_triggers(fleet['roles'], fleet['roleLinks'], fleet.get('triggers', []))
    return {'created': created, 'skippedInternal': skipped}


def contract_problem(fleet, rule):
    if not rule.get('workflowLinkId'):
        return ''
    link = next((l for l in fleet['roleLinks'] if l['id'] == rule['workflowLinkId']), None)
    if not link or signature(link) != rule.get('workflowSignature') or link['source'] != rule['sourceRoleId'] or link['target'] != rule['targetRoleId']:
        return 'The source workflow relationship changed or was removed. Rebuild this trigger from its current contract before sending.'
    return ''
