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
ACTIONS = ('reprompt', 'handoff', 'stop-turn', 'stop', 'pause', 'escalate')
THEN_ACTIONS = ('escalate', 'handoff', 'stop', 'pause', 'none')
OUTCOME_SECONDS = 1800
MARKER = '[Fleet Studio]'

DEFAULT_MESSAGES = {
    'looping': ("You appear to be repeating yourself: {evidence}\n"
                "Stop repeating that step. State the root cause in one or two sentences in your reply, then try a different approach "
                "toward your goal. If you cannot make progress without help, run the blocked report and stop."),
    'stalled': ("{evidence}\nYour goal: {goal}\nContinue the work{on_ticket}. If something is stopping you, "
                "say what it is with the blocked report instead of waiting."),
    'admin-heavy': ("{evidence}\nReporting is not the goal. Do not post status updates, poll, or re-read instructions. "
                    "Spend the next turn on the work itself: {goal}"),
    'burning': ("{evidence}\nPause and summarise what you have learned in three bullet points, decide the single next edit "
                "or test that moves the work{on_ticket} forward, and do it."),
    'waiting': "You reported a blocker: {evidence}\nCheck whether it is resolved. If it is, continue; if not, do nothing further.",
}
PRESETS = [
    {'name': 'Break loops', 'when': {'signal': 'looping', 'forMinutes': 0}, 'action': 'reprompt',
     'cooldownMinutes': 10, 'maxFires': 2, 'thenAction': 'handoff'},
    {'name': 'Restart stalled work', 'when': {'signal': 'stalled', 'forMinutes': 0}, 'action': 'reprompt',
     'cooldownMinutes': 20, 'maxFires': 2, 'thenAction': 'escalate'},
    {'name': 'Refocus admin-heavy agents', 'when': {'signal': 'admin-heavy', 'forMinutes': 5}, 'action': 'reprompt',
     'cooldownMinutes': 30, 'maxFires': 1, 'thenAction': 'escalate'},
    {'name': 'Refocus token burn', 'when': {'signal': 'burning', 'forMinutes': 0}, 'action': 'reprompt',
     'cooldownMinutes': 30, 'maxFires': 1, 'thenAction': 'escalate'},
]


def default_automations(fleet_id):
    """The recommended rules, switched on: a fleet that never configured rules recovers stuck agents by itself.

    IDs derive from the fleet, so reading the same unconfigured fleet twice gives the same rules.
    """
    return [{**preset, 'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'fleet-studio:' + fleet_id + ':' + preset['name'])),
             'enabled': True, 'roleId': '', 'message': '', 'handoffRoleId': '', 'parkSource': True,
             'windowHours': 24, 'pauseMinutes': 60} for preset in PRESETS]


def number(value, name, low, high, default):
    value = default if value is None else value
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(name + ' must be between ' + str(low) + ' and ' + str(high) + '.')
    return value


def clean_automations(raw, role_ids, agent_ids=None):
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
        agent_id = row.get('agentId', '')
        if agent_id and agent_ids is not None and agent_id not in agent_ids:
            raise ValueError('An automation can only watch an agent in this fleet.')
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
            **({'agentId': agent_id} if agent_id else {}),
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


def watches(rule, agent):
    """Whether a rule applies to an agent: a named agent decides alone, otherwise the role does."""
    if rule.get('agentId'):
        return rule['agentId'] == agent['id']
    return not rule['roleId'] or rule['roleId'] in agent.get('roleIds', [])


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
        fires, fallback_at = [], None
        for index, row in enumerate(rows):
            end = rows[index + 1]['at'] if index + 1 < len(rows) else now
            if not matches(rule, {'state': row['state']}):
                continue
            moment = max(row['at'] + rule['when']['forMinutes'] * 60, start)
            while moment <= end:
                recent = [at for at in fires if at > moment - rule['windowHours'] * 3600]
                if rule['maxFires'] and len(recent) >= rule['maxFires']:
                    # As live: one fallback after the latest regular fire, not one per matching interval.
                    if rule['thenAction'] != 'none' and (fallback_at is None or fallback_at < recent[-1]) and moment >= start:
                        result['fallbacks'] += 1
                        fallback_at = moment
                    break
                fires.append(moment)
                moment += rule['cooldownMinutes'] * 60
        counted = [at for at in fires if at >= start]
        if counted:
            result['agents'][agent] = len(counted)
            result['fires'] += len(counted)
    return result


# How long a just-started agent counts as working before the session monitor sees it.
STARTING_SECONDS = 120
# Studio's own messages, which are not recovery and do not count toward the one-message-at-a-time limit.
NOT_RECOVERY = ('dispatch', 'agent-handoff', 'blocked-notify', 'unblock-wake')
# The quiet time an agent gets after a recovery message before any other rule may send it one.
RECOVERY_GAP_SECONDS = 900
# A blocked agent waiting on the operator is mentioned again once after this long.
REMIND_SECONDS = 1800
# Messages someone is waiting on; when one cannot be delivered the operator must hear of it.
DIRECT_NOTICES = ('agent-handoff', 'blocked-notify', 'unblock-wake')
# Fires that may have reached their recipient, or still will: never send these again.
LIVE_STATUSES = ('pending', 'sending', 'held', 'sent', 'unconfirmed')


class Automation:
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.RLock()
        # Delivery checks run under the control and store locks; they use their own lock
        # so a dispatch never waits on a tick that is itself waiting to dispatch.
        self.hold_lock = threading.Lock()
        # New work let through recently, by agent: the session monitor reports it as working
        # only on its next poll, so without this one tick could start more agents than the limit.
        self.started = {}
        self.path = app.root / '.runtime' / 'automation.json'
        self.data = {'fires': [], 'paused': {}, 'escalations': [], 'throttled': {}, 'throttleLog': []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding='utf-8')))

    def persist(self):
        from fleet_studio import atomic_json
        self.path.parent.mkdir(exist_ok=True)
        with self.hold_lock:
            # Each fleet keeps its own history so a busy fleet cannot erase another's rule limits.
            by_fleet = {}
            for fire in self.data['fires']:
                by_fleet.setdefault(fire['fleetId'], []).append(fire)
            self.data['fires'] = sorted((f for fires in by_fleet.values() for f in fires[-1000:]), key=lambda f: f['at'])
            # Per fleet, oldest acknowledged first, so a busy fleet cannot evict another's open escalations.
            kept = []
            for fleet_id in {e['fleetId'] for e in self.data['escalations']}:
                rows = [e for e in self.data['escalations'] if e['fleetId'] == fleet_id]
                extra = len(rows) - 500
                if extra > 0:
                    drop = {id(e) for e in [e for e in rows if e['acknowledged']][:extra]}
                    rows = [e for e in rows if id(e) not in drop][-500:]
                kept += rows
            self.data['escalations'] = sorted(kept, key=lambda e: e['at'])
            self.data['throttleLog'] = self.data['throttleLog'][-1000:]
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
            recent = self.clock() - STARTING_SECONDS
            working = [a for a in fleet['agents'] if a['id'] not in recipients and (sessions.get(session_key(a), {}).get('status') == 'working'
                       or self.started.get(fleet['id'] + ':' + a['id'], 0) > recent)]
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

    def hold(self, fleet_id, agents, monitor, throttle=True):
        """Pause and throttle checks for an automatic delivery; records throttled time.

        Throttles apply only to sends that start new work. A reprompt or stop that would
        fix a burning agent must never wait behind the limit it is trying to bring down.
        """
        fleet = self.app.store.fleet(fleet_id)
        for agent in agents:
            reason = self.paused_reason(fleet_id, agent)
            if reason:
                return reason
        reason = self.throttle_reason(fleet, agents, monitor) if throttle else ''
        now = self.clock()
        with self.hold_lock:
            if throttle and not reason:
                for agent in agents:
                    self.started[fleet_id + ':' + agent['id']] = now
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
            changed = self.expire(state, monitor, now)
            for fleet in state['fleets']:
                changed = self.resolve(fleet, monitor, now) or changed
                changed = self.alert_caps(fleet, now) or changed
                if self.app.deployments.data['paused'].get(fleet['id']):
                    continue
                for rule in fleet.get('automations', []):
                    if not rule['enabled']:
                        continue
                    for agent in fleet['agents']:
                        if not agent['enabled'] or not agent.get('sessionId'):
                            continue
                        if not watches(rule, agent):
                            continue
                        health = self.app.health.get(fleet['id'], agent['id'])
                        if not matches(rule, health) or now - health['since'] < rule['when']['forMinutes'] * 60:
                            continue
                        changed = self.consider(fleet, rule, agent, health, monitor, now) or changed
            changed = self.track_outcomes(now) or changed
            changed = self.remind(state, now) or changed
            if changed:
                self.persist()

    def expire(self, state, monitor, now):
        """Drop finished pauses and close throttle records for agents no longer throttled."""
        changed = False
        for key, until in list(self.data['paused'].items()):
            if until <= now:
                self.data['paused'].pop(key)
                changed = True
        fleets = {fleet['id']: fleet for fleet in state['fleets']}
        for key in list(self.data['throttled']):
            fleet_id, agent_id = key.split(':', 1)
            fleet = fleets.get(fleet_id)
            agent = next((a for a in fleet['agents'] if a['id'] == agent_id), None) if fleet else None
            if not agent or not self.throttle_reason(fleet, [agent], monitor):
                with self.hold_lock:
                    started = self.data['throttled'].pop(key, None)
                    if started is not None:
                        self.data['throttleLog'].append({'fleetId': fleet_id, 'agentId': agent_id, 'start': started, 'end': now})
                changed = True
        return changed

    def alert_caps(self, fleet, now):
        """Escalate once an hour when a fleet or role is over its token budget; throttles alone only slow new work."""
        limits = fleet.get('throttles') or {}
        alerts = self.data.setdefault('capAlerts', {})
        health = {a['id']: self.app.health.get(fleet['id'], a['id']) or {} for a in fleet['agents']}
        checks = []
        if limits.get('fleetTokensPerHour'):
            checks.append(('fleet', 'The fleet', sum(h.get('tokensLastHour', 0) for h in health.values()), limits['fleetTokensPerHour']))
        for role_id, limit in (limits.get('roleTokensPerHour') or {}).items():
            role = next((r['name'] for r in fleet.get('roles', []) if r['id'] == role_id), role_id)
            spent = sum(health[a['id']].get('tokensLastHour', 0) for a in fleet['agents'] if role_id in a.get('roleIds', []))
            checks.append((role_id, role, spent, limit))
        changed = False
        for key, name, spent, limit in checks:
            mark = fleet['id'] + ':' + key
            if spent < limit or now - alerts.get(mark, 0) < 3600:
                continue
            alerts[mark] = now
            record = {'id': uuid.uuid4().hex, 'evidence': name + ' used ' + format(spent, ',') + ' tokens in the last hour (budget '
                      + format(limit, ',') + '). New work is held; check Agent health for who is burning.', 'detail': ''}
            self.escalate(fleet, {'id': '', 'name': name}, {'name': 'Token budget'}, record)
            changed = True
        return changed

    def resolve(self, fleet, monitor, now):
        """Settle every unfinished fire, whether or not its rule still matches."""
        changed = False
        for fire in self.data['fires']:
            if fire['fleetId'] != fleet['id'] or fire['status'] not in ('sending', 'held'):
                continue
            if fire['status'] == 'sending':
                changed = self.settle(fleet, fire) or changed
                continue
            if fire['ruleId'] == 'dispatch' and now - fire['at'] > 900:
                # An offer the agent could not take within 15 minutes goes back to the pool.
                fire.update(status='skipped', detail='The agent was not ready; the ticket was offered back.')
                self.app.dispatch.release(fleet['id'], fire['ticket'])
                changed = True
                continue
            if now - fire['at'] > 86400:
                fire.update(status='skipped', detail='Not delivered within a day; dropped.')
                changed = True
                continue
            if fire.get('message') and fire.get('recipientId'):
                # Direct messages (agent hand-offs, dispatch, blocker notices) resend as written.
                recipient = next((a for a in fleet['agents'] if a['id'] == fire['recipientId']), None)
                if not recipient:
                    fire.update(status='failed', detail='The hand-off recipient left the fleet.')
                    changed = True
                    continue
                before = fire['detail']
                try:
                    self.send(fleet, recipient, fire['message'], fire)
                except Exception as exc:
                    # One undeliverable message must never stop the other rules from running.
                    fire.update(status='failed', detail=str(exc)[-600:])
                    if fire['ruleId'] in ('dispatch', 'agent-handoff') and fire.get('ticket'):
                        self.app.dispatch.release(fleet['id'], fire['ticket'])
                    if fire['ruleId'] != 'dispatch':
                        self.escalate_failure(fleet, fire)
                changed = changed or fire['status'] != 'held' or fire['detail'] != before
                if fire['ruleId'] != 'dispatch':
                    changed = self.escalate_long_hold(fleet, fire, now, 15) or changed
                continue
            rule = next((r for r in fleet.get('automations', []) if r['id'] == fire['ruleId']), None)
            agent = next((a for a in fleet['agents'] if a['id'] == fire['agentId']), None)
            health = self.app.health.get(fleet['id'], fire['agentId'])
            if not rule or not rule['enabled'] or not agent or not matches(rule, health) or health['since'] != fire['episode']:
                fire.update(status='skipped', detail='The agent recovered or the rule changed before delivery.')
                changed = True
                continue
            before = (fire['status'], fire['detail'])
            self.execute(fleet, rule, agent, health, fire, monitor)
            changed = changed or before != (fire['status'], fire['detail'])
            changed = self.escalate_long_hold(fleet, fire, now, rule['cooldownMinutes']) or changed
        return changed

    def remind(self, state, now):
        """Remind the operator once about a blocked agent still waiting on them after 30 minutes."""
        fleets = {fleet['id']: fleet for fleet in state['fleets']}
        changed = False
        for record in self.data['escalations']:
            # Only a fresh blocker: older ones were seen before (or predate reminders) and are not repeated.
            if (not record.get('wakeAgentId') or record['acknowledged'] or record.get('remindedAt')
                    or not REMIND_SECONDS <= now - record['at'] <= 4 * REMIND_SECONDS or record['fleetId'] not in fleets):
                continue
            if not self.still_blocked(fleets[record['fleetId']], record):
                continue
            record['remindedAt'] = now
            record['reported'] = record['message']
            record['message'] = 'Still blocked after ' + str(int((now - record['at']) // 60)) + ' min: ' + record['message']
            url = (fleets[record['fleetId']].get('alerts') or {}).get('webhookUrl')
            if url:
                threading.Thread(target=self.notify, args=(url, record), daemon=True).start()
            changed = True
        return changed

    def still_blocked(self, fleet, record):
        """Whether the agent behind a blocked escalation is still in the fleet and has not reported since."""
        agent = next((a for a in fleet['agents'] if a['id'] == record['wakeAgentId'] and a['enabled']), None)
        if not agent:
            return False
        with self.app.progress.lock, closing(self.app.progress.connect()) as db:
            if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_notes'").fetchone():
                return True
            row = db.execute('SELECT data FROM agent_notes WHERE fleet=? AND agent=?', (fleet['id'], agent['id'])).fetchone()
        note = json.loads(row['data']) if row else None
        return not note or (note.get('kind') == 'blocked' and note.get('at', 0) <= record['at'] + 60)

    def escalate_long_hold(self, fleet, fire, now, minutes):
        """Tell the operator once when a delivery has waited longer than its cooldown; it keeps retrying."""
        from studio_triggers import CLIENT_OPEN
        # Nothing reaches a session open in a client, so waiting out the cooldown only wastes the agent's time.
        client = CLIENT_OPEN in fire.get('detail', '')
        if fire['status'] != 'held' or fire.get('heldEscalated') or (not client and now - fire['at'] < minutes * 60):
            return False
        fire['heldEscalated'] = True
        hint = ' Reply in the session yourself, or close it so Studio can.' if client else ''
        record = dict(fire, evidence=fire['evidence'] + ' (waiting ' + str(int((now - fire['at']) // 60)) + ' min to deliver: ' + fire['detail'] + ')' + hint)
        # The escalation gets a copy, so this fire stays held and keeps retrying.
        self.escalate(fleet, {'id': fire['agentId'], 'name': fire['agentName']}, {'name': fire['ruleName']}, record)
        return True

    def settle(self, fleet, fire):
        receipt = next((c for c in self.app.controls.snapshot(full=True) if c['id'] == fire.get('controlId')), None)
        if not receipt and self.clock() - fire.get('sentAt', fire['at']) > 600:
            # The receipt was lost (for example a damaged controls file). The message may have landed,
            # so it is not retried; the operator checks the session.
            fire.update(status='unconfirmed', detail='No delivery receipt was recorded; check the session.')
            self.escalate_failure(fleet, fire)
            return True
        if not receipt or receipt['status'] == 'sending':
            return False
        targets = receipt['targets']
        detail = '; '.join(t.get('detail', t['status']) for t in targets)
        if all(t['status'] == 'held' for t in targets):
            # A late check found the recipient not ready; nothing was sent, so it waits again.
            fire.update(status='held', detail=detail)
            fire.pop('controlId', None)
        elif receipt['status'] == 'completed':
            fire.update(status='sent', detail=detail)
            self.park(fleet, fire)
        elif receipt['status'] == 'unknown' or any(t['status'] in ('unknown', 'sending', 'waiting') for t in targets):
            # A timeout or a restart mid-send: the message may have been accepted, so it is never resent.
            fire.update(status='unconfirmed', detail=detail)
            self.escalate_failure(fleet, fire)
        else:
            fire.update(status='failed', detail=detail)
            if fire['ruleId'] in ('dispatch', 'agent-handoff') and fire.get('ticket'):
                # The work did not arrive, so the recipient's claim must not hold it.
                self.app.dispatch.release(fleet['id'], fire['ticket'])
            # An offer that did not arrive is simply made again later; anything else someone waits on is escalated.
            if fire['ruleId'] != 'dispatch' and (fire.get('fallback') or fire['action'] == 'handoff' or fire['ruleId'] in DIRECT_NOTICES):
                self.escalate_failure(fleet, fire)
        return True

    def park(self, fleet, fire):
        """Pause the stuck agent only once its work has actually reached someone else."""
        minutes = fire.pop('parkMinutes', None)
        if minutes:
            self.data['paused'][fleet['id'] + ':' + fire['agentId']] = self.clock() + minutes * 60
            # Stop the source's running turn too, so two agents do not work the same ticket.
            source = next((a for a in fleet['agents'] if a['id'] == fire['agentId']), None)
            if source:
                try:
                    self.stop_turn(source)
                except Exception:
                    pass  # Nothing running, or the backend cannot stop it; muting still applies.
            fire['detail'] = (fire['detail'] + ' ' if fire['detail'] else '') + fire['agentName'] + ' parked for ' + str(minutes) + ' min.'

    def consider(self, fleet, rule, agent, health, monitor, now):
        fires = [f for f in self.data['fires'] if f['ruleId'] == rule['id'] and f['agentId'] == agent['id']]
        if any(f['status'] in ('pending', 'sending', 'held') for f in fires):
            return False  # resolve() owns unfinished fires.
        last = fires[-1] if fires else None
        # The cooldown runs from delivery: a fire held for a while must not let the next one follow straight after it.
        if last and now - max(last['at'], last.get('sentAt', 0)) < rule['cooldownMinutes'] * 60:
            return False
        # Only attempts that reached the agent, or failed outright, use up the rule's budget.
        regular = [f for f in fires if not f.get('fallback') and f['status'] in ('sent', 'failed', 'unconfirmed')]
        recent = [f for f in regular if f['at'] > now - rule['windowHours'] * 3600]
        action, fallback = rule['action'], False
        kind = health['signals'][0].get('kind') if health['signals'] else ''
        if kind == 'closed-session' and action == 'reprompt':
            action = 'escalate'  # A closed session cannot read a reprompt; a person has to reopen it.
        if kind == 'nudge-loop' and action == 'reprompt':
            action = 'escalate'  # Studio's prompts are what is not working; another one would only add to the loop.
        if action in ('reprompt', 'handoff') and self.recently_messaged(fleet['id'], agent['id'], rule['id'], now):
            return False
        if rule['maxFires'] and len(recent) >= rule['maxFires']:
            if rule['thenAction'] == 'none' or any(f.get('fallback') and f['at'] > recent[-1]['at'] for f in fires):
                return False
            action, fallback = rule['thenAction'], True
        fire = {'id': uuid.uuid4().hex, 'fleetId': fleet['id'], 'ruleId': rule['id'], 'ruleName': rule['name'],
                'agentId': agent['id'], 'agentName': agent['name'], 'action': action, 'fallback': fallback,
                'at': now, 'episode': health['since'], 'healthState': health['state'],
                'evidence': evidence_text(health), 'status': 'pending', 'detail': '', 'outcome': None}
        self.data['fires'].append(fire)
        self.execute(fleet, rule, agent, health, fire, monitor)
        self.app.event('Automation · ' + rule['name'], agent['name'] + ': ' + fire['action'] + ' (' + fire['status'] + ')', fleet['id'])
        return True

    def recently_messaged(self, fleet_id, agent_id, rule_id, now):
        """Whether another recovery rule already messaged this agent, or is about to; one problem gets one message at a time."""
        return any(f['fleetId'] == fleet_id and f['agentId'] == agent_id and f['ruleId'] != rule_id
                   and f['ruleId'] not in NOT_RECOVERY and f['action'] in ('reprompt', 'handoff')
                   and (f['status'] in ('pending', 'sending', 'held')
                        or (f['status'] in ('sent', 'unconfirmed') and now - max(f['at'], f.get('sentAt', 0)) < RECOVERY_GAP_SECONDS))
                   for f in self.data['fires'])

    def execute(self, fleet, rule, agent, health, fire, monitor):
        action = fire['action']
        try:
            if action == 'reprompt':
                if health['state'] == 'looping':
                    # A looping turn would run on until it ends; stop it so the reprompt lands now.
                    from studio_agents import NothingRunning
                    try:
                        self.stop_turn(agent)
                    except (NothingRunning, RuntimeError):
                        pass  # Nothing running, or this backend cannot stop it; the reprompt waits for the turn.
                self.send(fleet, agent, self.compose(fleet, rule, agent, health), fire)
            elif action == 'handoff':
                self.handoff(fleet, rule, agent, health, fire, monitor)
            elif action in ('stop-turn', 'stop'):
                from studio_agents import NothingRunning
                try:
                    detail = self.stop_turn(agent)
                except NothingRunning:
                    # Nothing to stop is the outcome a stop wants; muting below still applies.
                    detail = 'No turn was running.'
                if action == 'stop':
                    # Stop the turn and mute Studio's messages, so the agent stays still until someone looks.
                    self.data['paused'][fleet['id'] + ':' + agent['id']] = self.clock() + rule['pauseMinutes'] * 60
                    detail += ' Studio messages muted for ' + str(rule['pauseMinutes']) + ' min.'
                fire.update(status='sent', detail=detail)
            elif action == 'pause':
                self.data['paused'][fleet['id'] + ':' + agent['id']] = self.clock() + rule['pauseMinutes'] * 60
                fire.update(status='sent', detail='Automatic messages to ' + agent['name'] + ' stopped for ' + str(rule['pauseMinutes']) + ' min.')
            elif action == 'escalate':
                self.escalate(fleet, agent, rule, fire)
        except Exception as exc:
            fire.update(status='failed', detail=str(exc)[-600:])
        if fire['status'] == 'failed' and (fire.get('fallback') or action in ('handoff', 'stop-turn', 'stop')):
            # A fallback or hand-off that cannot run must not leave the agent stuck silently.
            self.escalate_failure(fleet, fire)

    def stop_turn(self, agent):
        backend = self.app.runtimes.for_agent(agent)
        stop = getattr(backend, 'interrupt', None)
        if not stop:
            raise RuntimeError(agent['name'] + "'s backend cannot stop a running turn.")
        return stop(agent['sessionId'])

    def escalate_failure(self, fleet, fire):
        agent = {'id': fire['agentId'], 'name': fire['agentName']}
        rule = {'name': fire['ruleName']}
        record = dict(fire, evidence=fire['evidence'] + ' (' + fire['action'] + ' failed: ' + fire['detail'] + ')')
        self.escalate(fleet, agent, rule, record)
        fire['detail'] += ' Escalated instead.'

    def goal(self, fleet, agent):
        """The goal as the agent's brief states it: from the active release when there is one."""
        release = self.app.deployments.data['active'].get(fleet['id'])
        if release:
            try:
                fleet = self.app.deployments.release(release)['fleet']
            except (OSError, ValueError):
                pass
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
        claimed = self.app.dispatch.current_ticket(fleet['id'], agent['id']) if hasattr(self.app, 'dispatch') else ''
        if claimed:
            return claimed
        handed = [f for f in self.data['fires'] if f.get('recipientId') == agent['id'] and f.get('ticket')]
        if handed:
            return task_key(handed[-1]['ticket'])
        # Workflow hand-offs delivered through triggers name their ticket too.
        with self.app.progress.lock, closing(self.app.progress.connect()) as db:
            handoffs = [json.loads(r['data']) for r in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fleet['id'],))]
        mine = [h for h in handoffs if h.get('targetAgentId') == agent['id'] and h.get('ticket') and not h.get('submittedAt')]
        return task_key(max(mine, key=lambda h: h['createdAt'])['ticket']) if mine else ''

    def compose(self, fleet, rule, agent, health):
        template = rule['message'] or DEFAULT_MESSAGES.get(health['state'], DEFAULT_MESSAGES['stalled'])
        ticket = self.ticket(fleet, agent)
        values = {'agent': agent['name'], 'goal': self.goal(fleet, agent), 'evidence': evidence_text(health),
                  'ticket': ticket or 'your current task', 'on_ticket': ' on ' + ticket if ticket else '', 'state': health['state'],
                  'minutes': str(int((self.clock() - health['since']) // 60))}
        body = template
        for key, value in values.items():
            body = body.replace('{' + key + '}', value)
        return MARKER + ' ' + rule['name'] + '\n\n' + body

    def send(self, fleet, agent, message, fire):
        """Start delivery. A recipient that is not ready yet leaves the fire held, not failed."""
        from studio_control import AutomationHeld
        new_work = fire['action'] in ('handoff', 'dispatch') or fire['ruleId'] in ('agent-handoff', 'dispatch')
        try:
            preview = self.app.controls.preview({'fleetId': fleet['id'], 'agentIds': [agent['id']], 'action': 'message',
                                                 'message': message, 'automation': True,
                                                 'purpose': 'automation-new-work' if new_work else 'automation-recovery'})
            receipt = self.app.controls.send(preview['id'])
        except AutomationHeld as exc:
            fire.update(status='held', detail=str(exc))
            return
        fire.update(status='sending', controlId=receipt['id'], detail='Delivery started.', sentAt=self.clock())

    def candidates(self, fleet, role_ids, exclude, monitor):
        """Agents in the roles who can take work now: healthy, not paused and deliverable."""
        found, reasons = [], []
        for other in fleet['agents']:
            if other['id'] == exclude or not other['enabled'] or not other.get('sessionId') or not set(role_ids) & set(other.get('roleIds', [])):
                continue
            health = self.app.health.get(fleet['id'], other['id']) or {}
            reason = ('is ' + health['state'] if health.get('state') in PROBLEM_STATES else '') or self.paused_reason(fleet['id'], other) \
                or self.app.triggers.hold_reason([other], monitor)
            if reason:
                reasons.append(other['name'] + ': ' + reason)
                continue
            found.append((health.get('state') != 'idle', bool(health.get('openWork')), other['name'], other))
        return [c[3] for c in sorted(found, key=lambda c: c[:3])], reasons

    def handoff(self, fleet, rule, agent, health, fire, monitor):
        role_id = rule['handoffRoleId'] or (rule['roleId'] or next(iter(agent.get('roleIds', [])), ''))
        found, reasons = self.candidates(fleet, [role_id], agent['id'], monitor)
        # A retry keeps the recipient chosen first while it is merely not ready, so the work is
        # not sprayed across agents and a short wait does not become a failure.
        chosen = next((a for a in fleet['agents'] if a['id'] == fire.get('recipientId') and a['enabled']), None)
        if chosen and ((self.app.health.get(fleet['id'], chosen['id']) or {}).get('state') in PROBLEM_STATES or self.paused_reason(fleet['id'], chosen)):
            chosen = None
        recipient = chosen or (found[0] if found else None)
        if not recipient:
            raise RuntimeError('No agent in the hand-off role can take over now' + (' (' + '; '.join(reasons) + ')' if reasons else '') + '.')
        ticket = self.ticket(fleet, agent)
        recent = self.recent_steps(fleet['id'], agent['id'])
        message = (MARKER + ' Handoff: ' + rule['name'] + '\n\n'
                   + agent['name'] + ' is ' + health['state'] + ' and this work now passes to you.\n'
                   + 'Evidence: ' + evidence_text(health) + '\n'
                   + ('Ticket: ' + ticket + '\n' if ticket else '')
                   + ('Their last steps: ' + recent + '\n' if recent else '')
                   + 'Your goal: ' + self.goal(fleet, recipient) + '\n'
                   + 'Avoid repeating the steps above and take the next concrete action.')
        fire.update(recipientId=recipient['id'], recipientName=recipient['name'], ticket=ticket)
        if rule['parkSource']:
            fire['parkMinutes'] = rule['pauseMinutes']
        self.send(fleet, recipient, message, fire)

    def recent_steps(self, fleet_id, agent_id, count=6):
        turns = self.app.telemetry.turns(fleet_id, agent_id, limit=5, calls=False)
        calls = sorted((c for t in turns for c in t.get('calls', [])), key=lambda c: c.get('at') or 0)[-count:]
        return ', '.join('`' + (c['label'] or c['tool']) + '`' + (' (failed)' if c.get('error') else '') for c in calls)

    def escalate(self, fleet, agent, rule, fire):
        record = {'id': uuid.uuid4().hex, 'fleetId': fleet['id'], 'agentId': agent['id'], 'agentName': agent['name'],
                  'ruleName': rule['name'], 'at': self.clock(), 'message': fire['evidence'], 'acknowledged': False,
                  **({'wakeAgentId': fire['wakeAgentId']} if fire.get('wakeAgentId') else {})}
        # Request threads escalate too, so the record is saved now rather than on the next changed tick.
        with self.lock:
            self.data['escalations'].append(record)
            self.persist()
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
            if fire['outcome'] or fire['status'] != 'sent' or fire['action'] not in ('reprompt', 'handoff'):
                continue
            agent_id = fire.get('recipientId') or fire['agentId']
            health = self.app.health.get(fire['fleetId'], agent_id) or {}
            # Recovery means real work after the prompt, not just a reply to it.
            if health.get('state') not in PROBLEM_STATES and (health.get('lastWorkAt') or 0) > fire['at']:
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
            first = not record['acknowledged']
            record['acknowledged'] = True
            self.persist()
        # Only the first acknowledgement wakes the agent.
        wake = record.get('wakeAgentId') if first else None
        fleet = self.app.store.fleet(fleet_id)
        agent = next((a for a in fleet['agents'] if a['id'] == wake), None) if wake else None
        if agent:
            # An agent that reported blocked to the operator was told it would be woken once someone looked.
            self.app.dispatch.deliver(fleet, agent, MARKER + ' The operator has looked at what blocked you\n\n'
                                      'You reported: ' + record.get('reported', record['message']) + '\nCheck whether you can continue now, or report blocked again.',
                                      '', 'unblock-wake', 'Wake after blocker', record.get('reported', record['message']))
        return deepcopy(record)

    def resume(self, fleet_id, agent_id):
        with self.lock:
            self.data['paused'].pop(fleet_id + ':' + agent_id, None)
            self.persist()

    def snapshot(self, fleet_id):
        now = self.clock()
        with self.lock, self.hold_lock:
            fires = [f for f in self.data['fires'] if f['fleetId'] == fleet_id][-200:]
            # Outcome figures cover the last day of delivered prompts and reassignments only.
            judged = [f for f in fires if f['at'] >= now - 86400 and f['status'] == 'sent' and f['action'] in ('reprompt', 'handoff') and f['outcome']]
            throttled = [{'agentId': key.split(':', 1)[1], 'since': since} for key, since in self.data['throttled'].items() if key.startswith(fleet_id + ':')]
            day = [t for t in self.data['throttleLog'] if t['fleetId'] == fleet_id and t['end'] >= now - 86400]
            last = {}
            for fire in fires:
                last[fire['ruleId']] = {k: fire[k] for k in ('at', 'status', 'detail', 'action', 'agentName')}
            throttled_seconds = sum(t['end'] - max(t['start'], now - 86400) for t in day) + sum(now - max(t['since'], now - 86400) for t in throttled)
            return deepcopy({
                'fires': fires[::-1],
                'escalations': [e for e in self.data['escalations'] if e['fleetId'] == fleet_id and not e['acknowledged']][::-1],
                'paused': [{'agentId': key.split(':', 1)[1], 'until': until} for key, until in self.data['paused'].items()
                           if key.startswith(fleet_id + ':') and until > now],
                'throttled': throttled,
                'throttledSeconds24h': round(throttled_seconds),
                'outcomes': {name: sum(f['outcome'] == name for f in judged) for name in ('recovered', 'changed', 'no-change')},
                'delivered': len(judged),
                'lastFire': last,
                'workflowPaused': self.app.deployments.data['paused'].get(fleet_id, False),
                'presets': PRESETS,
            })
