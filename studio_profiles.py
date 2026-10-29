"""Fleet profiles: a whole setup saved as a file, so a fleet can be retooled from a list.

A profile holds the portable workflow plus everything that makes it run (who holds which role,
hand-off delivery, recovery rules, limits, dispatch and health thresholds), keyed by the workflow's
stable spec IDs. It never holds sessions, directories or hosts, so it describes a setup, not a machine.
"""
from copy import deepcopy
import json
import re
import time

from workflow_spec import apply_spec, digest, export_spec, stable, validate

SCHEMA = 'fleet-studio-profile/1'
SLUG = re.compile(r'[a-z0-9-]{1,60}')
# The setup saved automatically before a switch; people cannot save over it by name.
PREVIOUS = 'before-switch-'


def slugify(name):
    slug = re.sub(r'[^a-z0-9]+', '-', str(name).lower()).strip('-')[:60].strip('-')
    if not slug:
        raise ValueError('Give the profile a name with letters or numbers.')
    return slug


def previous_slug(fleet_id):
    return PREVIOUS + fleet_id.replace('-', '')[:8]


def capture(fleet, name, description=''):
    """The fleet's whole setup as a profile document, with local IDs replaced by spec IDs."""
    spec = export_spec(fleet)
    roles = {r['id']: stable(r, 'role') for r in fleet['roles']}
    agents = {a['id']: a['name'] for a in fleet['agents']}
    automations = []
    for rule in fleet.get('automations', []):
        rule = deepcopy(rule)
        for key in ('roleId', 'handoffRoleId'):
            if rule.get(key):
                rule[key] = roles.get(rule[key], '')
        if rule.get('agentId'):
            rule['agentName'] = agents.get(rule.pop('agentId'), '')
        automations.append(rule)
    throttles = deepcopy(fleet.get('throttles') or {})
    if throttles.get('roleTokensPerHour'):
        throttles['roleTokensPerHour'] = {roles[k]: v for k, v in throttles['roleTokensPerHour'].items() if k in roles}
    return {
        'schema': SCHEMA, 'name': str(name).strip()[:80], 'description': str(description).strip()[:600], 'savedAt': time.time(),
        'source': fleet['name'], 'spec': spec,
        'assignments': {a['name']: [roles[r] for r in a.get('roleIds', []) if r in roles] for a in fleet['agents']},
        'delivery': {stable(l, 'handoff'): l.get('delivery', 'off') for l in fleet['roleLinks']},
        'triggers': {stable(t, 'trigger'): t['enabled'] for t in fleet.get('triggers', []) if not t.get('workflowLinkId')},
        'positions': {roles[r['id']]: deepcopy(r['position']) for r in fleet['roles'] if r.get('position')},
        'automations': automations, 'throttles': throttles,
        'dispatch': deepcopy(fleet.get('dispatch') or {}), 'health': deepcopy(fleet.get('health') or {}),
    }


def retool(fleet, profile):
    """The fleet set up as the profile describes. Returns (fleet, remap): what could not be matched is listed, not guessed."""
    from studio_workflow import sync_handoff_triggers
    spec = validate(profile['spec'])
    remap = []
    base = deepcopy(fleet)
    # Assignments come from the profile, so the old ones must not block roles the profile drops.
    for agent in base['agents']:
        agent['roleIds'] = []
    updated = apply_spec(base, spec)
    by_spec = {r['specId']: r['id'] for r in updated['roles']}
    agents = {a['name']: a for a in updated['agents']}
    assignments = profile.get('assignments') or {}
    for agent in updated['agents']:
        wanted = assignments.get(agent['name'])
        if wanted is None:
            remap.append(agent['name'] + ' has no role in this profile.')
            continue
        agent['roleIds'] = [by_spec[r] for r in wanted if r in by_spec]
    remap += [name + ' is in the profile but not in this fleet; its roles are left unassigned.' for name in assignments if name not in agents]
    delivery = profile.get('delivery') or {}
    for link in updated['roleLinks']:
        link['delivery'] = delivery.get(link['specId'], 'off')
    enabled = profile.get('triggers') or {}
    for trigger in updated['triggers']:
        if not trigger.get('workflowLinkId') and trigger.get('specId') in enabled:
            trigger['enabled'] = bool(enabled[trigger['specId']])
    updated['triggers'] = sync_handoff_triggers(updated['roles'], updated['roleLinks'], updated['triggers'])
    positions = profile.get('positions') or {}
    for role in updated['roles']:
        if role['specId'] in positions:
            role['position'] = deepcopy(positions[role['specId']])
    automations = []
    for rule in deepcopy(profile.get('automations') or []):
        missing = [key for key in ('roleId', 'handoffRoleId') if rule.get(key) and rule[key] not in by_spec]
        name = rule.pop('agentName', None)
        if missing:
            remap.append('Recovery rule "' + rule.get('name', '') + '" dropped: its role is not in this profile.')
            continue
        if name is not None:
            if name not in agents:
                remap.append('Recovery rule "' + rule.get('name', '') + '" dropped: it watches ' + name + ', who is not in this fleet.')
                continue
            rule['agentId'] = agents[name]['id']
        for key in ('roleId', 'handoffRoleId'):
            if rule.get(key):
                rule[key] = by_spec[rule[key]]
        automations.append(rule)
    updated['automations'] = automations
    throttles = deepcopy(profile.get('throttles') or {})
    if throttles.get('roleTokensPerHour'):
        limits = throttles['roleTokensPerHour']
        remap += ['A token limit was dropped: its role is not in this profile.' for k in limits if k not in by_spec]
        throttles['roleTokensPerHour'] = {by_spec[k]: v for k, v in limits.items() if k in by_spec}
    updated['throttles'] = throttles
    updated['dispatch'] = deepcopy(profile.get('dispatch') or {})
    updated['health'] = deepcopy(profile.get('health') or {})
    return updated, remap


class Profiles:
    """A folder of profile files beside the app's state."""

    def __init__(self, app):
        self.app = app
        self.root = app.root / 'profiles'

    def path(self, slug):
        # Paths are only ever built from a checked slug, never from a name or a client string.
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise ValueError('Unknown profile.')
        return self.root / (slug + '.profile.json')

    def read(self, slug):
        path = self.path(slug)
        if not path.is_file():
            raise ValueError('That profile no longer exists. Reload the list.')
        profile = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(profile, dict) or profile.get('schema') != SCHEMA:
            raise ValueError('This file is not a Fleet Studio profile.')
        return profile

    def list(self, fleet=None):
        active = None
        if fleet and fleet['id'] in self.app.deployments.data['active']:
            active = self.app.deployments.release(self.app.deployments.data['active'][fleet['id']])['digest']
        rows, problems = [], []
        for path in sorted(self.root.glob('*.profile.json')) if self.root.is_dir() else []:
            slug = path.name[:-len('.profile.json')]
            try:
                profile = self.read(slug)
                spec = validate(profile['spec'])
            except (ValueError, KeyError, TypeError, OSError) as exc:
                problems.append(path.name + ': ' + str(exc))
                continue
            rows.append({'slug': slug, 'name': profile.get('name') or slug, 'description': profile.get('description', ''),
                         'savedAt': profile.get('savedAt'), 'source': profile.get('source', ''),
                         'roles': len(spec['roles']), 'agents': len(profile.get('assignments') or {}),
                         'automatic': slug.startswith(PREVIOUS), 'active': active == digest(spec)})
        # The setup saved before the last switch comes first, so switching back is one pick.
        rows.sort(key=lambda r: (not r['automatic'], (r['name'] or '').lower()))
        return {'profiles': rows, 'problems': problems}

    def write(self, slug, profile):
        from fleet_studio import atomic_json
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path(slug), profile)

    def save(self, payload):
        with self.app.store.lock:
            state = self.app.store.snapshot()
            if payload.get('revision') != state['revision']:
                raise ValueError('Fleet changed. Reload before saving a profile.')
            fleet = self.app.store.fleet(payload.get('fleetId'))
            slug = slugify(payload.get('name', ''))
            if slug.startswith(PREVIOUS):
                raise ValueError('That name is kept for the setup saved before a switch. Choose another.')
            if self.path(slug).exists() and not payload.get('overwrite'):
                raise ValueError('A profile with that name exists. Choose another name or replace it.')
            self.write(slug, capture(fleet, payload['name'], payload.get('description', '')))
            return {'slug': slug, **self.list(fleet)}

    def delete(self, payload):
        path = self.path(payload.get('slug'))
        if path.exists():
            path.unlink()
        return self.list()
