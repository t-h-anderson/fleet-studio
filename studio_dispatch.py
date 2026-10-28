"""Give agents their next piece of work.

Two sources: finished work moves along the workflow to the next role, and an
idle agent with nothing open is offered the oldest unclaimed tracker ticket
that its role picks up. Nothing is sent when nothing is eligible, so idle
agents are never woken just to look for work. Claims stop two agents from
receiving the same ticket.
"""
from contextlib import closing
import hashlib
import json
import threading
import time
import uuid

from studio_health import PROBLEM_STATES

DEFAULTS = {'enabled': True, 'onDeploy': True, 'states': []}
# Without workflow states, these tracker stages are ready to be picked up, and by whom.
STAGE_ROLES = {'ready': 'writer', 'backlog': 'coordinator', 'integration': 'integrator'}
READY_WORDS = ('ready', 'todo', 'to-do', 'backlog', 'open', 'queued', 'awaiting')


def clean_dispatch(value, state_ids=()):
    value = value or {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unknown dispatch setting.')
    result = {**DEFAULTS, **value}
    if type(result['enabled']) is not bool or type(result['onDeploy']) is not bool:
        raise ValueError('Dispatch switches must be true or false.')
    states = result['states']
    if not isinstance(states, list) or len(states) > 50 or any(not isinstance(s, str) for s in states):
        raise ValueError('Dispatch states must be a list of workflow state IDs.')
    if state_ids and set(states) - set(state_ids):
        raise ValueError('Dispatch states must name workflow states.')
    return result


def eligible_states(workflow, chosen):
    """States whose tickets are waiting to be picked up: the operator's choice, or ready-looking ones."""
    states = workflow.get('states', []) if workflow else []
    if chosen:
        return [s for s in states if s['id'] in chosen]
    return [s for s in states if not s['fallback'] and s['issueState'] != 'closed'
            and any(word in (s['id'] + ' ' + s['name']).lower() for word in READY_WORDS)]


class Dispatch:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.RLock()
        with closing(app.progress.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS dispatch_claims (
                fleet TEXT NOT NULL, ticket TEXT NOT NULL, agent TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(fleet, ticket))''')

    # Claims.

    def claims(self, fleet_id):
        with self.app.progress.lock, closing(self.app.progress.connect()) as db:
            rows = db.execute('SELECT ticket, agent, at, status FROM dispatch_claims WHERE fleet=?', (fleet_id,)).fetchall()
        return {row['ticket']: dict(row) for row in rows}

    def claim(self, fleet_id, ticket, agent_id, status='offered'):
        from studio_progress import task_key
        with self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO dispatch_claims VALUES(?,?,?,?,?)', (fleet_id, task_key(ticket), agent_id, self.clock(), status))

    def release(self, fleet_id, ticket=None, agent_id=None):
        from studio_progress import task_key
        with self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
            if ticket:
                db.execute("UPDATE dispatch_claims SET status='done' WHERE fleet=? AND ticket=?", (fleet_id, task_key(ticket)))
            elif agent_id:
                db.execute("UPDATE dispatch_claims SET status='done' WHERE fleet=? AND agent=? AND status!='done'", (fleet_id, agent_id))

    def current_ticket(self, fleet_id, agent_id):
        for ticket, row in self.claims(fleet_id).items():
            if row['agent'] == agent_id and row['status'] != 'done':
                return ticket
        return ''

    # Idle agents pull work.

    def tick(self, state, monitor):
        with self.lock:
            for fleet in state['fleets']:
                settings = clean_dispatch(fleet.get('dispatch'))
                if not settings['enabled'] or self.app.deployments.data['paused'].get(fleet['id']):
                    continue
                self.offer(fleet, settings, monitor)

    def tickets_by_role(self, fleet, settings):
        """Open, unassigned tickets in a pick-up state, oldest first, with the roles that take them."""
        from workflow_spec import project_ticket, stable
        from studio_progress import task_key
        backlog = self.app.backlog.snapshot(fleet['id'])
        if backlog.get('error') or backlog.get('observedAt') is None:
            return []
        runtime = self.app.runtime_fleet(fleet)
        workflow = runtime.get('workflow') or {}
        states = {s['id'] for s in eligible_states(workflow, settings['states'])}
        by_stable = {stable(r, 'role'): r['id'] for r in runtime.get('roles', [])}
        keys = {r['key']: r['id'] for r in runtime.get('roles', []) if r.get('key')}
        claims = self.claims(fleet['id'])
        found = []
        for ticket in backlog.get('items', []):
            if ticket['state'] == 'closed' or ticket.get('assignees'):
                continue
            url = task_key(ticket['url'])
            claim = claims.get(url)
            if claim and claim['status'] != 'done':
                continue
            if workflow.get('states'):
                projected = project_ticket(ticket, workflow)
                if not projected or projected['id'] not in states:
                    continue
                roles = [by_stable[r] for r in projected['roles'] if r in by_stable]
            else:
                role = keys.get(STAGE_ROLES.get(ticket.get('stage'), ''))
                roles = [role] if role and ticket.get('stage') == 'ready' else []
            if roles:
                found.append((ticket['iid'], ticket, roles))
        return [(ticket, roles) for _, ticket, roles in sorted(found, key=lambda item: item[0])]

    def ready(self, fleet, agent, monitor, claims):
        if not agent['enabled'] or not agent.get('sessionId'):
            return False
        health = self.app.health.get(fleet['id'], agent['id']) or {}
        if health.get('openWork') or health.get('state') in PROBLEM_STATES or health.get('state') in ('waiting', 'productive'):
            return False
        if any(row['agent'] == agent['id'] and row['status'] != 'done' for row in claims.values()):
            return False
        if self.app.automation.paused_reason(fleet['id'], agent):
            return False
        return not self.app.triggers.hold_reason([agent], monitor)

    def offer(self, fleet, settings, monitor):
        tickets = self.tickets_by_role(fleet, settings)
        if not tickets:
            return
        claims = self.claims(fleet['id'])
        taken = set()
        for agent in fleet['agents']:
            if not self.ready(fleet, agent, monitor, claims):
                continue
            match = next(((ticket, roles) for ticket, roles in tickets if ticket['url'] not in taken
                          and set(roles) & set(agent.get('roleIds', []))), None)
            if not match:
                continue
            ticket, _ = match
            taken.add(ticket['url'])
            message = ('[Fleet Studio] Your next ticket\n\n' + ticket['title'] + '\n' + ticket['url'] + '\n\n'
                       'Your goal: ' + self.app.automation.goal(fleet, agent) + '\n'
                       'Work it toward done; report done, blocked or a hand-off when you get there.')
            self.deliver(fleet, agent, message, ticket['url'], 'dispatch', 'Next ticket', ticket['title'])

    def deliver(self, fleet, recipient, message, ticket, rule_id, rule_name, evidence, source=None, key=None):
        """Send a direct message as an automation fire, claiming the ticket for the recipient.

        With a `key` (from the client's retry ID) a retried report reuses the first delivery.
        """
        from studio_automation import LIVE_STATUSES
        automation = self.app.automation
        if key:
            existing = next((f for f in automation.data['fires'] if f['id'] == key and f['status'] in LIVE_STATUSES), None)
            if existing:
                return existing
            if any(f['id'] == key for f in automation.data['fires']):
                key += '-' + uuid.uuid4().hex[:6]
        fire = {'id': key or uuid.uuid4().hex, 'fleetId': fleet['id'], 'ruleId': rule_id, 'ruleName': rule_name,
                'agentId': (source or recipient)['id'], 'agentName': (source or recipient)['name'], 'action': rule_id,
                'fallback': False, 'at': self.clock(), 'episode': None, 'healthState': rule_id, 'evidence': evidence,
                'status': 'pending', 'detail': '', 'outcome': None, 'recipientId': recipient['id'],
                'recipientName': recipient['name'], 'ticket': ticket, 'message': message}
        if ticket:
            self.claim(fleet['id'], ticket, recipient['id'])
        with automation.lock:
            automation.data['fires'].append(fire)
            try:
                automation.send(fleet, recipient, message, fire)
            except Exception as exc:
                fire.update(status='failed', detail=str(exc)[-600:])
                if ticket:
                    self.release(fleet['id'], ticket)
            automation.persist()
        self.app.event('Dispatch · ' + rule_name, recipient['name'] + ': ' + (ticket or evidence)[:120], fleet['id'])
        return fire

    # Finished work moves on.

    def route_done(self, fleet, agent, note, ticket, target=None, retry_id=None):
        """Pass finished work to the next role along the workflow. Returns what to tell the agent."""
        names = {r['id']: r['name'] for r in fleet.get('roles', [])}
        outgoing = [l for l in fleet.get('roleLinks', []) if l['source'] in agent.get('roleIds', [])
                    and names.get(l['target']) and l['target'] not in agent.get('roleIds', [])]
        if ticket:
            self.release(fleet['id'], ticket)
        if target is None and len({l['target'] for l in outgoing}) > 1:
            options = ', '.join(sorted({names[l['target']] for l in outgoing}))
            return {'next': 'Recorded. Who picks this up next? Report a hand-off with --to one of: ' + options + '.'}
        if target is None and not outgoing:
            return {'next': 'Recorded. Stop here; Studio will send your next ticket when one is ready.'}
        link = next((l for l in outgoing if target is None or names.get(l['target'], '').casefold() == str(target).casefold()), None)
        detail = ('\nHand-off: ' + link['label'] + (('; pass ' + link['artifact']) if link.get('artifact') else '')
                  + (('; only if ' + link['condition']) if link.get('condition') else '')) if link else ''
        try:
            result = self.app.reports.handoff(fleet, agent, target or names[link['target']], note + detail, ticket, retry_id)
        except ValueError as exc:
            # The work is finished either way; tell the operator who should have received it.
            record = {'id': uuid.uuid4().hex, 'evidence': 'Finished work has nobody to go to: ' + str(exc), 'detail': ''}
            self.app.automation.escalate(fleet, agent, {'name': 'Done, but not routed'}, record)
            return {'next': 'Recorded. Nobody could take it next, so the operator was told. Stop here.'}
        return {**result, 'next': 'Recorded and passed to ' + (result.get('recipient') or 'the next role')
                + '. Stop here; Studio will send your next ticket when one is ready.'}

    # Blockers.

    def blocked(self, fleet, agent, note, ticket, target=None, retry_id=None):
        """Tell whoever can unblock the agent, or the operator when nobody is named."""
        if target:
            role_ids, named = self.app.reports.resolve(fleet, agent, target)
            recipient = named or self.app.reports.pick(fleet, agent, role_ids)
            message = ('[Fleet Studio] ' + agent['name'] + ' is blocked and needs you\n\n' + note + '\n'
                       + ('Ticket: ' + ticket + '\n' if ticket else '')
                       + 'When it is sorted, report unblocked or done so ' + agent['name'] + ' is woken.')
            self.deliver(fleet, recipient, message, '', 'blocked-notify', 'Blocked: ' + agent['name'], note, source=agent,
                         key='blocked-' + hashlib.sha256((agent['id'] + retry_id).encode()).hexdigest()[:24] if retry_id else None)
            return {'waitingOn': recipient['id'], 'recipient': recipient['name'],
                    'next': 'Recorded; ' + recipient['name'] + ' has been told. Stop here; Studio will wake you when they report back.'}
        record = {'id': uuid.uuid4().hex, 'evidence': note, 'detail': ''}
        self.app.automation.escalate(fleet, agent, {'name': 'Blocked report'}, record)
        return {'next': 'Recorded and passed to the operator. Stop here; Studio will wake you when it is resolved.'}

    def wake_waiting(self, fleet, resolver):
        """When someone reports done or unblocked, wake each agent that was waiting on them, once."""
        with self.app.progress.lock, closing(self.app.progress.connect()) as db:
            notes = [(row['agent'], json.loads(row['data'])) for row in
                     db.execute('SELECT agent, data FROM agent_notes WHERE fleet=?', (fleet['id'],))]
        for agent_id, note in notes:
            if note.get('kind') != 'blocked' or note.get('waitingOn') != resolver['id'] or note.get('woken'):
                continue
            agent = next((a for a in fleet['agents'] if a['id'] == agent_id), None)
            if not agent:
                continue
            message = ('[Fleet Studio] ' + resolver['name'] + ' has reported back on what blocked you\n\n'
                       'You were waiting on: ' + note['note'] + '\nCheck it, then continue or report blocked again.')
            self.deliver(fleet, agent, message, '', 'unblock-wake', 'Wake after blocker', note['note'], source=resolver)
            note['woken'] = self.clock()
            with self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
                db.execute('UPDATE agent_notes SET data=? WHERE fleet=? AND agent=?', (json.dumps(note), fleet['id'], agent_id))

    def on_deploy(self, fleet_id):
        """Send every agent its brief straight after a deploy, when the fleet asks for it."""
        fleet = self.app.store.fleet(fleet_id)
        if not clean_dispatch(fleet.get('dispatch'))['onDeploy'] or not self.app.launch_enabled:
            return None
        try:
            preview = self.app.deployments.notification({'fleetId': fleet_id})
            return self.app.controls.send(preview['id'])
        except ValueError:
            return None
