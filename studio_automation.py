"""Health automations: act on stalled, looping, admin-heavy or burning agents.

Automations run from the fleet draft so a tweak takes effect on the next tick,
without a release or acknowledgement round. Each rule limits itself (cooldown
and a maximum number of fires per agent in a rolling window) and then falls
back to a different action, so Studio cannot become the loop it is meant to
break. Throttles cap concurrent work and token spend for every automatic send.
"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

from studio_health import PROBLEM_STATES

SIGNALS = PROBLEM_STATES + ('waiting', 'any-problem')
ACTIONS = ('reprompt', 'handoff', 'stop-turn', 'pause', 'escalate')
THEN_ACTIONS = ('escalate', 'handoff', 'pause', 'none')
OUTCOME_SECONDS = 1800
MARKER = '[Fleet Studio]'

DEFAULT_MESSAGES = {
    'looping': ("You appear to be repeating yourself: {evidence}\n"
                "Stop repeating that step. Write down the root cause in one or two sentences, then try a different approach "
                "toward your goal. If you cannot make progress without help, run the blocked report and stop."),
    'stalled': ("{evidence}\nYour goal: {goal}\nContinue the work on {ticket}. If something is stopping you, "
                "say what it is with the blocked report instead of waiting."),
    'admin-heavy': ("{evidence}\nReporting is not the goal. Do not post status updates, poll, or re-read instructions. "
                    "Spend the next turn on the work itself: {goal}"),
    'burning': ("{evidence}\nPause and summarise what you have learned in three bullet points, decide the single next edit "
                "or test that moves {ticket} forward, and do it."),
    'waiting': "You reported a blocker: {evidence}\nCheck whether it is resolved. If it is, continue; if not, do nothing further.",
}
PRESETS = [
    {'name': 'Break loops', 'when': {'signal': 'looping', 'forMinutes': 0}, 'action': 'reprompt',
     'cooldownMinutes': 10, 'maxFires': 2, 'thenAction': 'handoff'},
    {'name': 'Restart stalled work', 'when': {'signal': 'stalled', 'forMinutes': 0}, 'action': 'reprompt',
     'cooldownMinutes': 20, 'maxFires': 2, 'thenAction': 'escalate'},
    {'name': 'Refocus admin-heavy agents', 'when': {'signal': 'admin-heavy', 'forMinutes': 5}, 'action': 'reprompt',
     'cooldownMinutes': 30, 'maxFires': 1, 'thenAction': 'escalate'},
    {'name': 'Pause token burn', 'when': {'signal': 'burning', 'forMinutes': 0}, 'action': 'reprompt',
     'cooldownMinutes': 30, 'maxFires': 1, 'thenAction': 'pause'},
]


def number(value, name, low, high, default):
    value = default if value is None else value
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(name + ' must be between ' + str(low) + ' and ' + str(high) + '.')
    return value


def clean_automations(raw, role_ids):
    raw = [] if raw is None else raw
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError('A fleet can have at most 100 automations.')
    output, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('Automations must be objects.')
        rule_id = str(uuid.UUID(str(row.get('id'))))
        if rule_id in seen:
            raise ValueError('Automation IDs must be unique.')
        seen.add(rule_id)
        name = row.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('Give the automation a name.')
        when = row.get('when') if isinstance(row.get('when'), dict) else {}
        signal = when.get('signal')
        if signal not in SIGNALS:
            raise ValueError('Choose a health signal for ' + name + '.')
        role = row.get('roleId', '')
        if role and role not in role_ids:
            raise ValueError('Automation roles must belong to this fleet.')
        action, then = row.get('action'), row.get('thenAction', 'escalate')
        if action not in ACTIONS or then not in THEN_ACTIONS:
            raise ValueError('Choose a supported automation action.')
        handoff_role = row.get('handoffRoleId', '')
        if handoff_role and handoff_role not in role_ids:
            raise ValueError('The handoff role must belong to this fleet.')
        message = row.get('message', '')
        if not isinstance(message, str) or len(message) > 4000 or message.lstrip().startswith(('/', '!')):
            raise ValueError('Use a plain message template of at most 4000 characters.')
        if type(row.get('enabled', True)) is not bool or type(row.get('parkSource', True)) is not bool:
            raise ValueError('Invalid automation switches.')
        output.append({
            'id': rule_id, 'name': name.strip(), 'enabled': row.get('enabled', True), 'roleId': role,
            'when': {'signal': signal, 'forMinutes': number(when.get('forMinutes'), 'forMinutes', 0, 10080, 0)},
            'action': action, 'thenAction': then, 'message': message.strip(), 'handoffRoleId': handoff_role,
            'parkSource': row.get('parkSource', True),
            'cooldownMinutes': number(row.get('cooldownMinutes'), 'cooldownMinutes', 1, 10080, 15),
            'maxFires': int(number(row.get('maxFires'), 'maxFires', 0, 1000, 3)),
            'windowHours': number(row.get('windowHours'), 'windowHours', 1, 720, 24),
            'pauseMinutes': number(row.get('pauseMinutes'), 'pauseMinutes', 1, 10080, 60),
        })
    return output


def clean_throttles(raw, role_ids):
    raw = raw or {}
    if not isinstance(raw, dict) or set(raw) - {'maxWorkingAgents', 'fleetTokensPerHour', 'roleTokensPerHour'}:
        raise ValueError('Unknown throttle setting.')
    result = {}
    if raw.get('maxWorkingAgents') is not None:
        result['maxWorkingAgents'] = int(number(raw['maxWorkingAgents'], 'maxWorkingAgents', 1, 500, 1))
    if raw.get('fleetTokensPerHour') is not None:
        result['fleetTokensPerHour'] = int(number(raw['fleetTokensPerHour'], 'fleetTokensPerHour', 1000, 10**10, 1000))
    roles = raw.get('roleTokensPerHour') or {}
    if not isinstance(roles, dict) or set(roles) - set(role_ids):
        raise ValueError('Role token limits must name roles in this fleet.')
    if roles:
        result['roleTokensPerHour'] = {key: int(number(value, 'role token limit', 1000, 10**10, 1000)) for key, value in roles.items()}
    return result


def clean_alerts(raw):
    raw = raw or {}
    if not isinstance(raw, dict) or set(raw) - {'webhookUrl'}:
        raise ValueError('Unknown alert setting.')
    url = raw.get('webhookUrl', '')
    if url:
        parts = urlsplit(url) if isinstance(url, str) else None
        loopback = parts and parts.hostname in ('127.0.0.1', 'localhost', '::1')
        if not parts or len(url) > 1000 or parts.username or parts.password or not parts.hostname \
                or not (parts.scheme == 'https' or parts.scheme == 'http' and loopback):
            raise ValueError('Alert webhooks need an HTTPS URL without credentials.')
        return {'webhookUrl': url}
    return {}


def matches(rule, health):
    if not health:
        return False
    signal = rule['when']['signal']
    return health['state'] in PROBLEM_STATES if signal == 'any-problem' else health['state'] == signal


def evidence_text(health):
    return ' '.join(signal['message'] for signal in health.get('signals', [])) or 'Health state: ' + health['state'] + '.'


def backtest(rule, history, now, hours=24):
    """Replay recorded health transitions to estimate how often a rule would act.

    History rows are {'at', 'agentId', 'state'} in time order. Each interval in a
    matching state fires after forMinutes, then every cooldown while it lasts,
    limited by maxFires per agent in the rolling window.
    """
    start = now - hours * 3600
    by_agent = {}
    for row in sorted(history, key=lambda r: r['at']):
        by_agent.setdefault(row['agentId'], []).append(row)
    result = {'fires': 0, 'fallbacks': 0, 'agents': {}, 'hours': hours}
    for agent, rows in by_agent.items():
        fires = []
        for index, row in enumerate(rows):
            end = rows[index + 1]['at'] if index + 1 < len(rows) else now
            if not matches(rule, {'state': row['state']}):
                continue
            moment = max(row['at'] + rule['when']['forMinutes'] * 60, start)
            fell_back = False
            while moment <= end:
                recent = [at for at in fires if at > moment - rule['windowHours'] * 3600]
                if rule['maxFires'] and len(recent) >= rule['maxFires']:
                    if not fell_back and rule['thenAction'] != 'none':
                        result['fallbacks'] += 1
                        fell_back = True
                    break
                fires.append(moment)
                moment += rule['cooldownMinutes'] * 60
        counted = [at for at in fires if at >= start]
        if counted:
            result['agents'][agent] = len(counted)
            result['fires'] += len(counted)
    return result


class Automation:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.RLock()
        # Delivery checks run under the control and store locks; they use their own lock
        # so a dispatch never waits on a tick that is itself waiting to dispatch.
        self.hold_lock = threading.Lock()
        self.path = app.root / '.runtime' / 'automation.json'
        self.data = {'fires': [], 'paused': {}, 'escalations': [], 'throttled': {}, 'throttleLog': []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding='utf-8')))

    def persist(self):
        from fleet_studio import atomic_json
        self.path.parent.mkdir(exist_ok=True)
        self.data['fires'] = self.data['fires'][-1000:]
        self.data['escalations'] = self.data['escalations'][-500:]
        self.data['throttleLog'] = self.data['throttleLog'][-1000:]
        with self.hold_lock:
            snapshot = deepcopy(self.data)
        atomic_json(self.path, snapshot)

    # Holds shared by every automatic delivery.

    def paused_reason(self, fleet_id, agent):
        until = self.data['paused'].get(fleet_id + ':' + agent['id'])
        if until and until > self.clock():
            return agent['name'] + ' is paused by automation until ' + time.strftime('%H:%M', time.localtime(until)) + '.'
        return ''

    def throttle_reason(self, fleet, agents, monitor):
        from studio_bindings import session_key
        limits = fleet.get('throttles') or {}
        if not limits:
            return ''
        sessions = monitor.get('sessions', {})
        recipients = {a['id'] for a in agents}
        if limits.get('maxWorkingAgents'):
            working = [a for a in fleet['agents'] if a['id'] not in recipients and sessions.get(session_key(a), {}).get('status') == 'working']
            if len(working) >= limits['maxWorkingAgents']:
                return 'Throttled: ' + str(len(working)) + ' agents are working (limit ' + str(limits['maxWorkingAgents']) + ').'
        health = {a['id']: self.app.health.get(fleet['id'], a['id']) or {} for a in fleet['agents']}
        if limits.get('fleetTokensPerHour'):
            spent = sum(h.get('tokensLastHour', 0) for h in health.values())
            if spent >= limits['fleetTokensPerHour']:
                return 'Throttled: the fleet used ' + format(spent, ',') + ' tokens in the last hour (limit ' + format(limits['fleetTokensPerHour'], ',') + ').'
        for role_id, limit in (limits.get('roleTokensPerHour') or {}).items():
            if not any(role_id in a.get('roleIds', []) for a in agents):
                continue
            spent = sum(health[a['id']].get('tokensLastHour', 0) for a in fleet['agents'] if role_id in a.get('roleIds', []))
            if spent >= limit:
                role = next((r['name'] for r in fleet.get('roles', []) if r['id'] == role_id), role_id)
                return 'Throttled: ' + role + ' used ' + format(spent, ',') + ' tokens in the last hour (limit ' + format(limit, ',') + ').'
        return ''

    def hold(self, fleet_id, agents, monitor):
        """Pause and throttle checks for an automatic delivery; records throttled time."""
        fleet = self.app.store.fleet(fleet_id)
        for agent in agents:
            reason = self.paused_reason(fleet_id, agent)
            if reason:
                return reason
        reason = self.throttle_reason(fleet, agents, monitor)
        now = self.clock()
        with self.hold_lock:
            for agent in agents:
                key = fleet_id + ':' + agent['id']
                started = self.data['throttled'].get(key)
                if reason and not started:
                    self.data['throttled'][key] = now
                elif not reason and started:
                    self.data['throttleLog'].append({'fleetId': fleet_id, 'agentId': agent['id'], 'start': started, 'end': now})
                    self.data['throttled'].pop(key)
        return reason

    # Rule evaluation.

    def tick(self, state, monitor):
        now = self.clock()
        with self.lock:
            changed = self.track_outcomes(now)
            for fleet in state['fleets']:
                if self.app.deployments.data['paused'].get(fleet['id']):
                    continue
                for rule in fleet.get('automations', []):
                    if not rule['enabled']:
                        continue
                    for agent in fleet['agents']:
                        if not agent['enabled'] or not agent.get('sessionId'):
                            continue
                        if rule['roleId'] and rule['roleId'] not in agent.get('roleIds', []):
                            continue
                        health = self.app.health.get(fleet['id'], agent['id'])
                        if not matches(rule, health) or now - health['since'] < rule['when']['forMinutes'] * 60:
                            continue
                        changed = self.consider(fleet, rule, agent, health, monitor, now) or changed
            if changed:
                self.persist()

    def consider(self, fleet, rule, agent, health, monitor, now):
        fires = [f for f in self.data['fires'] if f['ruleId'] == rule['id'] and f['agentId'] == agent['id']]
        last = fires[-1] if fires else None
        if last and last['status'] in ('sending', 'held') and last.get('episode') == health['since']:
            return self.retry(fleet, last, monitor)
        if last and now - last['at'] < rule['cooldownMinutes'] * 60:
            return False
        recent = [f for f in fires if f['at'] > now - rule['windowHours'] * 3600 and not f.get('fallback')]
        action, fallback = rule['action'], False
        if rule['maxFires'] and len(recent) >= rule['maxFires']:
            if any(f.get('fallback') and f['at'] > recent[-1]['at'] for f in fires) or rule['thenAction'] == 'none':
                return False
            action, fallback = rule['thenAction'], True
        fire = {'id': uuid.uuid4().hex, 'fleetId': fleet['id'], 'ruleId': rule['id'], 'ruleName': rule['name'],
                'agentId': agent['id'], 'agentName': agent['name'], 'action': action, 'fallback': fallback,
                'at': now, 'episode': health['since'], 'healthState': health['state'],
                'evidence': evidence_text(health), 'status': 'pending', 'detail': '', 'outcome': None}
        self.data['fires'].append(fire)
        self.execute(fleet, rule, agent, health, fire, monitor)
        self.app.event('Automation · ' + rule['name'], agent['name'] + ': ' + action + ' (' + fire['status'] + ')', fleet['id'])
        return True

    def retry(self, fleet, fire, monitor):
        if fire['status'] == 'sending':
            receipt = next((c for c in self.app.controls.snapshot(full=True) if c['id'] == fire.get('controlId')), None)
            if not receipt or receipt['status'] == 'sending':
                return False
            targets = receipt['targets']
            if all(t['status'] == 'held' for t in targets):
                fire.update(status='held', detail='; '.join(t.get('detail', '') for t in targets))
                fire.pop('controlId', None)
            else:
                fire.update(status='sent' if receipt['status'] == 'completed' else 'failed',
                            detail='; '.join(t.get('detail', t['status']) for t in targets))
            return True
        rule = next((r for r in fleet.get('automations', []) if r['id'] == fire['ruleId']), None)
        agent = next((a for a in fleet['agents'] if a['id'] == fire['agentId']), None)
        health = self.app.health.get(fleet['id'], fire['agentId'])
        if not rule or not agent or not matches(rule, health):
            fire.update(status='skipped', detail='The agent recovered before delivery.')
            return True
        before = (fire['status'], fire['detail'])
        self.execute(fleet, rule, agent, health, fire, monitor)
        return before != (fire['status'], fire['detail'])

    def execute(self, fleet, rule, agent, health, fire, monitor):
        action = fire['action']
        try:
            if action == 'reprompt':
                self.send(fleet, agent, self.compose(fleet, rule, agent, health), fire, monitor)
            elif action == 'handoff':
                self.handoff(fleet, rule, agent, health, fire, monitor)
            elif action == 'stop-turn':
                backend = self.app.runtimes.for_agent(agent)
                stop = getattr(backend, 'interrupt', None)
                if not stop:
                    raise RuntimeError(agent['name'] + "'s backend cannot stop a running turn.")
                fire.update(status='sent', detail=stop(agent['sessionId']))
            elif action == 'pause':
                self.data['paused'][fleet['id'] + ':' + agent['id']] = self.clock() + rule['pauseMinutes'] * 60
                fire.update(status='sent', detail='Automatic deliveries to ' + agent['name'] + ' paused for ' + str(rule['pauseMinutes']) + ' min.')
            elif action == 'escalate':
                self.escalate(fleet, agent, rule, fire)
        except Exception as exc:
            fire.update(status='failed', detail=str(exc)[-600:])

    def goal(self, fleet, agent):
        roles = [r for r in fleet.get('roles', []) if r['id'] in agent.get('roleIds', [])]
        return '; '.join(r.get('goal') or r.get('responsibilities', '') for r in roles if r.get('goal') or r.get('responsibilities')) or 'the outcome of your role'

    def ticket(self, fleet, agent):
        from studio_progress import task_key
        with self.app.progress.lock, closing(self.app.progress.connect()) as db:
            rows = [json.loads(r['data']) for r in db.execute('SELECT data FROM agent_tasks WHERE fleet=? AND agent=?', (fleet['id'], agent['id']))]
        open_rows = [r for r in rows if r.get('phase') != 'submitted' and r.get('kind') != 'work-finished']
        latest = max(open_rows, key=lambda r: r['at'], default=None)
        if latest:
            return latest['taskUrl']
        handed = [f for f in self.data['fires'] if f.get('recipientId') == agent['id'] and f.get('ticket')]
        return task_key(handed[-1]['ticket']) if handed else ''

    def compose(self, fleet, rule, agent, health):
        template = rule['message'] or DEFAULT_MESSAGES.get(health['state'], DEFAULT_MESSAGES['stalled'])
        values = {'agent': agent['name'], 'goal': self.goal(fleet, agent), 'evidence': evidence_text(health),
                  'ticket': self.ticket(fleet, agent) or 'your current task', 'state': health['state'],
                  'minutes': str(int((self.clock() - health['since']) // 60))}
        body = template
        for key, value in values.items():
            body = body.replace('{' + key + '}', value)
        return MARKER + ' ' + rule['name'] + '\n\n' + body

    def send(self, fleet, agent, message, fire, monitor):
        preview = self.app.controls.preview({'fleetId': fleet['id'], 'agentIds': [agent['id']], 'action': 'message',
                                             'message': message, 'automation': True, 'purpose': 'health-automation'})
        receipt = self.app.controls.send(preview['id'])
        fire.update(status='sending', controlId=receipt['id'], detail='Delivery started.')

    def handoff(self, fleet, rule, agent, health, fire, monitor):
        role_id = rule['handoffRoleId'] or (rule['roleId'] or next(iter(agent.get('roleIds', [])), ''))
        candidates = []
        for other in fleet['agents']:
            if other['id'] == agent['id'] or not other['enabled'] or not other.get('sessionId') or role_id not in other.get('roleIds', []):
                continue
            other_health = self.app.health.get(fleet['id'], other['id']) or {}
            if other_health.get('state') in PROBLEM_STATES or self.paused_reason(fleet['id'], other):
                continue
            candidates.append((other_health.get('state') != 'idle', bool(other_health.get('openWork')), other['name'], other))
        if not candidates:
            raise RuntimeError('No healthy agent in the handoff role can take over; escalating is recommended.')
        recipient = sorted(candidates, key=lambda c: c[:3])[0][3]
        ticket = self.ticket(fleet, agent)
        recent = self.recent_steps(fleet['id'], agent['id'])
        message = (MARKER + ' Handoff: ' + rule['name'] + '\n\n'
                   + agent['name'] + ' is ' + health['state'] + ' and this work now passes to you.\n'
                   + 'Evidence: ' + evidence_text(health) + '\n'
                   + ('Ticket: ' + ticket + '\n' if ticket else '')
                   + ('Their last steps: ' + recent + '\n' if recent else '')
                   + 'Your goal: ' + self.goal(fleet, recipient) + '\n'
                   + 'Read the ticket, avoid repeating the steps above, and take the next concrete action.')
        fire.update(recipientId=recipient['id'], recipientName=recipient['name'], ticket=ticket)
        self.send(fleet, recipient, message, fire, monitor)
        if rule['parkSource']:
            self.data['paused'][fleet['id'] + ':' + agent['id']] = self.clock() + rule['pauseMinutes'] * 60
            fire['detail'] += ' ' + agent['name'] + ' parked for ' + str(rule['pauseMinutes']) + ' min.'

    def recent_steps(self, fleet_id, agent_id, count=6):
        turns = self.app.telemetry.turns(fleet_id, agent_id, limit=5)
        calls = sorted((c for t in turns for c in t.get('calls', [])), key=lambda c: c.get('at') or 0)[-count:]
        return ', '.join('`' + (c['label'] or c['tool']) + '`' + (' (failed)' if c.get('error') else '') for c in calls)

    def escalate(self, fleet, agent, rule, fire):
        record = {'id': uuid.uuid4().hex, 'fleetId': fleet['id'], 'agentId': agent['id'], 'agentName': agent['name'],
                  'ruleName': rule['name'], 'at': self.clock(), 'message': fire['evidence'], 'acknowledged': False}
        self.data['escalations'].append(record)
        fire.update(status='sent', detail='Escalated to the operator.')
        url = (fleet.get('alerts') or {}).get('webhookUrl')
        if url:
            threading.Thread(target=self.notify, args=(url, record), daemon=True).start()

    @staticmethod
    def notify(url, record):
        body = json.dumps({'text': 'Fleet Studio: ' + record['agentName'] + ' needs attention (' + record['ruleName'] + '). ' + record['message'],
                           'escalation': record}).encode('utf-8')
        try:
            with urlopen(Request(url, data=body, method='POST', headers={'Content-Type': 'application/json'}), timeout=10):
                pass
        except Exception:
            import logging
            logging.warning('Alert webhook delivery failed for escalation %s', record['id'])

    def track_outcomes(self, now):
        changed = False
        for fire in self.data['fires']:
            if fire['outcome'] or fire['status'] not in ('sent',) or fire['action'] == 'escalate':
                continue
            agent_id = fire.get('recipientId') or fire['agentId']
            health = self.app.health.get(fire['fleetId'], agent_id) or {}
            if health.get('state') in ('productive',) and health.get('since', 0) >= fire['at']:
                fire['outcome'] = 'recovered'
                changed = True
            elif now - fire['at'] >= OUTCOME_SECONDS:
                fire['outcome'] = 'no-change' if health.get('state') == fire['healthState'] else 'changed'
                changed = True
        return changed

    def acknowledge(self, fleet_id, escalation_id):
        with self.lock:
            record = next((e for e in self.data['escalations'] if e['id'] == escalation_id and e['fleetId'] == fleet_id), None)
            if not record:
                raise ValueError('Escalation not found.')
            record['acknowledged'] = True
            self.persist()
            return deepcopy(record)

    def resume(self, fleet_id, agent_id):
        with self.lock:
            self.data['paused'].pop(fleet_id + ':' + agent_id, None)
            self.persist()

    def snapshot(self, fleet_id):
        now = self.clock()
        with self.lock:
            fires = [f for f in self.data['fires'] if f['fleetId'] == fleet_id][-200:]
            throttled = [{'agentId': key.split(':', 1)[1], 'since': since} for key, since in self.data['throttled'].items() if key.startswith(fleet_id + ':')]
            day = [t for t in self.data['throttleLog'] if t['fleetId'] == fleet_id and t['end'] >= now - 86400]
            throttled_seconds = sum(t['end'] - max(t['start'], now - 86400) for t in day) + sum(now - max(t['since'], now - 86400) for t in throttled)
            return deepcopy({
                'fires': fires[::-1],
                'escalations': [e for e in self.data['escalations'] if e['fleetId'] == fleet_id and not e['acknowledged']][::-1],
                'paused': [{'agentId': key.split(':', 1)[1], 'until': until} for key, until in self.data['paused'].items()
                           if key.startswith(fleet_id + ':') and until > now],
                'throttled': throttled,
                'throttledSeconds24h': round(throttled_seconds),
                'outcomes': {name: sum(f['outcome'] == name for f in fires) for name in ('recovered', 'changed', 'no-change')},
                'presets': PRESETS,
            })
