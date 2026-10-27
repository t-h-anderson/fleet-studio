"""The three agent reports: done, blocked and hand-off.

Agents say only what Studio cannot observe. Studio fills in identity, release,
ticket and event IDs, and routes a hand-off through a matching workflow
trigger or, failing that, directly to a healthy agent in the named role.
"""
from contextlib import closing
import hashlib
import json
import time
import uuid

from studio_health import PROBLEM_STATES

KINDS = ('done', 'blocked', 'handoff')


class Reports:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        with closing(app.progress.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS agent_notes (fleet TEXT, agent TEXT, data TEXT, PRIMARY KEY(fleet, agent))')

    def latest(self, db, fleet_id, agent_id):
        row = db.execute('SELECT data FROM agent_notes WHERE fleet=? AND agent=?', (fleet_id, agent_id)).fetchone()
        return json.loads(row['data']) if row else None

    def report(self, payload):
        from studio_progress import link, plain
        kind = payload.get('kind')
        if kind not in KINDS:
            raise ValueError('Report done, blocked or handoff.')
        fleet = self.app.store.fleet(payload.get('fleetId'))
        agent = next((a for a in fleet['agents'] if a['id'] == payload.get('agentId')), None)
        if not agent or not agent['enabled']:
            raise ValueError('Identify an enabled agent in this fleet.')
        note = plain(payload.get('note'), 'note', 1800, True)
        ticket = link(payload['ticket'], 'ticket') if payload.get('ticket') else self.app.automation.ticket(fleet, agent)
        now = self.clock()
        record = {'kind': kind, 'note': note, 'ticket': ticket, 'at': now, 'agentId': agent['id'], 'agentName': agent['name']}
        if kind == 'handoff':
            record.update(self.handoff(fleet, agent, payload.get('to'), note, ticket))
        with self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO agent_notes VALUES(?,?,?)', (fleet['id'], agent['id'], json.dumps(record)))
            self.app.progress.record(db, fleet['id'], 'agent-note', agent['id'] + ':' + str(now), now, record)
        record['milestone'] = self.milestone(fleet, agent, kind, note, ticket)
        self.app.event(agent['name'] + ' · ' + kind, note[:140], fleet['id'])
        return {'recorded': True, 'kind': kind, 'ticket': ticket, **{k: record[k] for k in ('recipient', 'route') if k in record}}

    def milestone(self, fleet, agent, kind, note, ticket):
        """Mirror the report into the tracker-linked progress model when a release and ticket exist."""
        release = self.app.deployments.data['active'].get(fleet['id'])
        if not release or not ticket or kind == 'handoff':
            return ''
        event = hashlib.sha256((kind + note + ticket).encode()).hexdigest()[:24]
        payload = {'fleetId': fleet['id'], 'agentId': agent['id'], 'releaseId': release, 'sessionId': agent['sessionId'],
                   'eventId': kind + '-' + event, 'taskUrl': ticket, 'evidenceUrl': ticket, 'summary': note,
                   'kind': 'work-finished' if kind == 'done' else 'blocked'}
        if kind == 'blocked':
            payload.update(reason=note, nextAction='Resolve: ' + note[:1700])
        try:
            self.app.progress.report(payload)
            return 'recorded'
        except ValueError as exc:
            # The note itself is recorded; a tracker mismatch is for the operator, not the agent.
            return 'not linked: ' + str(exc)

    def resolve(self, fleet, agent, target):
        if not isinstance(target, str) or not target.strip():
            outgoing = [link['target'] for link in fleet.get('roleLinks', []) if link['source'] in agent.get('roleIds', [])]
            if len(set(outgoing)) == 1:
                return outgoing[0], None
            raise ValueError('Name the role or agent to hand off to with --to.')
        wanted = target.strip().casefold()
        named = next((a for a in fleet['agents'] if wanted in (a['name'].casefold(), a['id'])), None)
        if named:
            return next(iter(named.get('roleIds', [])), ''), named
        role = next((r for r in fleet.get('roles', []) if wanted in (r['name'].casefold(), r.get('key', ''), r['id'])), None)
        if not role:
            raise ValueError('No role or agent called ' + target.strip() + ' in this fleet.')
        return role['id'], None

    def handoff(self, fleet, agent, target, note, ticket):
        role_id, named = self.resolve(fleet, agent, target)
        runtime = self.app.runtime_fleet(fleet)
        rules = [r for r in runtime.get('triggers', []) if r['enabled'] and r['kind'] == 'event' and r['targetRoleId'] == role_id
                 and r['sourceRoleId'] in agent.get('roleIds', [])]
        if rules and ticket:
            event = hashlib.sha256((agent['id'] + note + ticket).encode()).hexdigest()[:24]
            try:
                result = self.app.triggers.report({'fleetId': fleet['id'], 'agentId': agent['id'], 'eventName': rules[0]['eventName'],
                                                   'eventId': 'handoff-' + event, 'ticket': ticket, 'artifact': note[:700],
                                                   'context': note, 'targetAgentId': named['id'] if named else ''})
                return {'route': 'workflow trigger ' + rules[0]['name'], 'recipient': ', '.join(a['targetName'] for a in result.get('actions', []))}
            except ValueError:
                pass  # Fall back to a direct hand-off below.
        recipient = named or self.pick(fleet, agent, role_id)
        message = ('[Fleet Studio] Hand-off from ' + agent['name'] + '\n\n' + note + '\n'
                   + ('Ticket: ' + ticket + '\n' if ticket else '')
                   + 'Your goal: ' + self.app.automation.goal(fleet, recipient))
        fire = {'id': uuid.uuid4().hex, 'fleetId': fleet['id'], 'ruleId': 'agent-handoff', 'ruleName': 'Agent hand-off',
                'agentId': agent['id'], 'agentName': agent['name'], 'action': 'handoff', 'fallback': False, 'at': self.clock(),
                'episode': None, 'healthState': 'handoff', 'evidence': note, 'status': 'pending', 'detail': '', 'outcome': None,
                'recipientId': recipient['id'], 'recipientName': recipient['name'], 'ticket': ticket, 'message': message}
        with self.app.automation.lock:
            self.app.automation.data['fires'].append(fire)
            try:
                self.app.automation.send(fleet, recipient, message, fire, self.app.status())
            except Exception as exc:
                fire.update(status='held', detail=str(exc)[-600:])
            self.app.automation.persist()
        return {'route': 'direct', 'recipient': recipient['name']}

    def pick(self, fleet, agent, role_id):
        candidates = []
        for other in fleet['agents']:
            if other['id'] == agent['id'] or not other['enabled'] or not other.get('sessionId') or role_id not in other.get('roleIds', []):
                continue
            health = self.app.health.get(fleet['id'], other['id']) or {}
            candidates.append((health.get('state') in PROBLEM_STATES, health.get('state') != 'idle', bool(health.get('openWork')), other['name'], other))
        if not candidates:
            raise ValueError('No other agent holds that role.')
        return sorted(candidates, key=lambda c: c[:4])[0][4]
