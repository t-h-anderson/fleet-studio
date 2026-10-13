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


def populate_triggers(fleet):
    roles = {r['id']: r for r in fleet['roles']}
    existing = {t.get('workflowLinkId') for t in fleet.get('triggers', [])}
    created, skipped = [], []
    fleet.setdefault('triggers', [])
    for link in fleet['roleLinks']:
        if link['id'] in existing:
            continue
        if internal(roles[link['source']]) or internal(roles[link['target']]):
            skipped.append(link['label'])
            continue
        trigger = {
            'id': str(uuid.uuid4()), 'name': link['label'], 'kind': 'event',
            'sourceRoleId': link['source'], 'targetRoleId': link['target'],
            'eventName': 'handoff-' + link['id'][:8], 'condition': 'turn-completed',
            'minutes': 30, 'enabled': False, 'mode': 'automatic', 'recipientMode': 'single',
            'workflowLinkId': link['id'], 'workflowSignature': signature(link),
            'message': ('Workflow action: ' + link['label'] + '\nFrom: ' + roles[link['source']]['name'] +
                        '\nTo: ' + roles[link['target']]['name'] +
                        '\nWhen: ' + (link['trigger'] or 'Upstream agent explicitly reports the handoff.') +
                        '\nRequired artifact: ' + (link['artifact'] or 'Read the current ticket handoff.') +
                        '\nRequired condition: ' + (link['condition'] or 'Confirm the current ticket scope and acceptance.') +
                        '\n\nRead the reported ticket and current handoff before acting. Confirm named custody, scope, evidence and explicit stops. A reported event is a notification, not proof of acceptance. Record work in the existing ticket/MR; do not invent missing assignments or transfer another agent\'s runtime.')
        }
        fleet['triggers'].append(trigger)
        created.append(trigger['name'])
    return {'created': created, 'skippedInternal': skipped}


def contract_problem(fleet, rule):
    if not rule.get('workflowLinkId'):
        return ''
    link = next((l for l in fleet['roleLinks'] if l['id'] == rule['workflowLinkId']), None)
    if not link or signature(link) != rule.get('workflowSignature') or link['source'] != rule['sourceRoleId'] or link['target'] != rule['targetRoleId']:
        return 'The source workflow relationship changed or was removed. Rebuild this trigger from its current contract before sending.'
    return ''
