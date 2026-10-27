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

KINDS = ('done', 'blocked', 'unblocked', 'handoff')


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
            raise ValueError('Report done, blocked, unblocked or handoff.')
        fleet = self.app.store.fleet(payload.get('fleetId'))
        agent = next((a for a in fleet['agents'] if a['id'] == payload.get('agentId')), None)
        if not agent or not agent['enabled']:
            raise ValueError('Identify an enabled agent in this fleet.')
        note = plain(payload.get('note'), 'note', 1800, True)
        ticket = link(payload['ticket'], 'ticket') if payload.get('ticket') else self.app.automation.ticket(fleet, agent)
        now = self.clock()
        record = {'kind': kind, 'note': note, 'ticket': ticket, 'at': now, 'agentId': agent['id'], 'agentName': agent['name']}
        if kind == 'handoff':
            record.update(self.handoff(fleet, agent, payload.get('to'), note, ticket, payload.get('retryId')))
        with self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO agent_notes VALUES(?,?,?)', (fleet['id'], agent['id'], json.dumps(record)))
            self.app.progress.record(db, fleet['id'], 'agent-note', agent['id'] + ':' + str(now), now, record)
        record['milestone'] = self.milestone(fleet, agent, kind, note, ticket)
        self.app.event(agent['name'] + ' · ' + kind, note[:140], fleet['id'])
        return {'recorded': True, 'kind': kind, 'ticket': ticket, **{k: record[k] for k in ('recipient', 'route', 'delivery') if k in record}}

    def milestone(self, fleet, agent, kind, note, ticket):
        """Mirror the report into the tracker-linked progress model when a release and ticket exist."""
        release = self.app.deployments.data['active'].get(fleet['id'])
        if not release or not ticket or kind == 'handoff':
            return ''
        kind_map = {'done': 'work-finished', 'blocked': 'blocked', 'unblocked': 'unblocked'}
        event = hashlib.sha256((kind + note + ticket).encode()).hexdigest()[:24]
        payload = {'fleetId': fleet['id'], 'agentId': agent['id'], 'releaseId': release, 'sessionId': agent['sessionId'],
                   'eventId': kind + '-' + event, 'taskUrl': ticket, 'evidenceUrl': ticket, 'summary': note,
                   'kind': kind_map[kind]}
        if kind == 'blocked':
            payload.update(reason=note, nextAction='Resolve: ' + note[:1700])
        try:
            self.app.progress.report(payload)
            return 'recorded'
        except ValueError as exc:
            # The note itself is recorded; a tracker mismatch is for the operator, not the agent.
            return 'not linked: ' + str(exc)

    def targets_hint(self, fleet, agent):
        names = [r['name'] for r in fleet.get('roles', []) if r.get('kind', 'subagent' if r.get('key') == 'subagent-reviewers' else 'agent') == 'agent']
        names += [a['name'] for a in fleet['agents'] if a['id'] != agent['id'] and a['enabled']]
        return 'Use --to with one of: ' + ', '.join(names) + '.'

    def resolve(self, fleet, agent, target):
        """Role IDs a hand-off may go to, and the named agent if one was given."""
        if not isinstance(target, str) or not target.strip():
            outgoing = {link['target'] for link in fleet.get('roleLinks', []) if link['source'] in agent.get('roleIds', [])}
            if len(outgoing) == 1:
                return list(outgoing), None
            raise ValueError('Say who should pick this up. ' + self.targets_hint(fleet, agent))
        wanted = target.strip().casefold()
        named = next((a for a in fleet['agents'] if wanted in (a['name'].casefold(), a['id']) and a['id'] != agent['id']), None)
        if named:
            return list(named.get('roleIds', [])), named
        role = next((r for r in fleet.get('roles', []) if wanted in (r['name'].casefold(), r.get('key', ''), r['id'])), None)
        if not role:
            raise ValueError('No role or agent called ' + target.strip() + '. ' + self.targets_hint(fleet, agent))
        return [role['id']], None

    def handoff(self, fleet, agent, target, note, ticket, retry_id=None):
        role_ids, named = self.resolve(fleet, agent, target)
        # The client sends one retry ID per invocation, so its own retries match and a later,
        # identical hand-off is new; without one, a ten-minute window stands in.
        basis = [agent['id'], retry_id] if retry_id else [agent['id'], role_ids, named['id'] if named else '', note, ticket, int(self.clock() // 600)]
        key = hashlib.sha256(json.dumps(basis).encode()).hexdigest()[:32]
        existing = next((f for f in self.app.automation.data['fires'] if f['id'] == key and f['status'] in ('pending', 'sending', 'sent')), None)
        if existing:
            return {'route': 'direct', 'recipient': existing['recipientName'], 'delivery': existing['status']}
        if any(f['id'] == key for f in self.app.automation.data['fires']):
            key += '-' + uuid.uuid4().hex[:6]
        runtime = self.app.runtime_fleet(fleet)
        rules = [r for r in runtime.get('triggers', []) if r['enabled'] and r['kind'] == 'event' and r['targetRoleId'] in role_ids
                 and r['sourceRoleId'] in agent.get('roleIds', [])]
        if rules and ticket:
            # The workflow defines this hand-off, so its guards and contracts apply. When they refuse
            # for reasons the agent cannot fix (holds, routing, contracts), deliver it directly instead.
            try:
                result = self.app.triggers.report({'fleetId': fleet['id'], 'agentId': agent['id'], 'eventName': rules[0]['eventName'],
                                                   'eventId': 'handoff-' + key[:24], 'ticket': ticket, 'artifact': note[:700],
                                                   'context': note, 'targetAgentId': named['id'] if named else ''})
            except ValueError as exc:
                fallback = str(exc)
            else:
                actions = result.get('actions', [])
                if actions:
                    return {'route': 'workflow trigger ' + rules[0]['name'], 'recipient': ', '.join(a['targetName'] for a in actions),
                            'delivery': ', '.join(sorted({a['status'] for a in actions})) or 'recorded'}
                fallback = 'the workflow recorded no delivery'
            import logging
            logging.info('Workflow hand-off for %s delivered directly: %s', agent['name'], fallback)
        recipient = named or self.pick(fleet, agent, role_ids)
        message = ('[Fleet Studio] Hand-off from ' + agent['name'] + '\n\n' + note + '\n'
                   + ('Ticket: ' + ticket + '\n' if ticket else '')
                   + 'Your goal: ' + self.app.automation.goal(fleet, recipient))
        fire = {'id': key, 'fleetId': fleet['id'], 'ruleId': 'agent-handoff', 'ruleName': 'Agent hand-off',
                'agentId': agent['id'], 'agentName': agent['name'], 'action': 'handoff', 'fallback': False, 'at': self.clock(),
                'episode': None, 'healthState': 'handoff', 'evidence': note, 'status': 'pending', 'detail': '', 'outcome': None,
                'recipientId': recipient['id'], 'recipientName': recipient['name'], 'ticket': ticket, 'message': message}
        with self.app.automation.lock:
            self.app.automation.data['fires'].append(fire)
            try:
                self.app.automation.send(fleet, recipient, message, fire)
            except Exception as exc:
                fire.update(status='failed', detail=str(exc)[-600:])
                self.app.automation.escalate_failure(fleet, fire)
            self.app.automation.persist()
        return {'route': 'direct', 'recipient': recipient['name'], 'delivery': 'queued' if fire['status'] == 'sending' else fire['status']}

    def pick(self, fleet, agent, role_ids):
        """The best agent to receive work now, or the healthiest member if none can receive yet."""
        ready, _ = self.app.automation.candidates(fleet, role_ids, agent['id'], self.app.status())
        if ready:
            return ready[0]
        members = [a for a in fleet['agents'] if a['id'] != agent['id'] and a['enabled'] and a.get('sessionId')
                   and set(role_ids) & set(a.get('roleIds', []))]
        if not members:
            raise ValueError('Nobody else holds that role. ' + self.targets_hint(fleet, agent))
        rank = lambda a: ((self.app.health.get(fleet['id'], a['id']) or {}).get('state') in PROBLEM_STATES, a['name'])
        return sorted(members, key=rank)[0]
