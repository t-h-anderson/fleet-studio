"""Per-agent health from turn telemetry: productive, stalled, looping, admin-heavy or burning.

Detectors are pure functions over recent turn summaries so they can be tested
with synthetic sequences and replayed for trigger backtests. Health is advice
for automation and operators; it never changes tracker state by itself.
"""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import json
import threading
import time

from studio_bindings import session_key

STATES = ('looping', 'burning', 'stalled', 'admin-heavy', 'waiting', 'productive', 'idle', 'offline', 'no-telemetry')
PROBLEM_STATES = ('looping', 'burning', 'stalled', 'admin-heavy')
DEFAULTS = {
    'stallMinutes': 20,
    'silentMinutes': 10,
    'loopRepeats': 4,
    'adminShare': 0.4,
    'burnTokensPerHour': 400000,
    'noProgressFailures': 5,
    'noProgressMinutes': 20,
}
LIMITS = {
    'stallMinutes': (1, 1440),
    'silentMinutes': (1, 1440),
    'loopRepeats': (2, 50),
    'adminShare': (0.05, 1),
    'burnTokensPerHour': (1000, 100000000),
    'noProgressFailures': (2, 100),
    'noProgressMinutes': (1, 1440),
}
WINDOW_SECONDS = 3600
WASTE_KINDS = ('loop', 'admin', 'wait', 'aborted', 'nudge')


def clean_health_settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unknown health setting.')
    result = {**DEFAULTS, **value}
    for key, (low, high) in LIMITS.items():
        number = result[key]
        if type(number) not in (int, float) or not low <= number <= high:
            raise ValueError(key + ' must be between ' + str(low) + ' and ' + str(high) + '.')
    return result


def fresh_tokens(turn):
    """Tokens the model processed anew; cache reads are reported separately."""
    tokens = turn.get('tokens', {})
    return tokens.get('input', 0) + tokens.get('output', 0) + tokens.get('cacheWrite', 0)


def recent_calls(turns, since):
    # A turn's calls are no later than its last activity, so older turns can be skipped whole.
    calls = [call for turn in turns if (turn.get('lastAt') or 0) >= since
             for call in turn.get('calls', []) if (call.get('at') or turn.get('lastAt') or 0) >= since]
    return sorted(calls, key=lambda call: (call.get('at') or 0, call.get('seq', 0)))


def repeated_cycle(steps, repeats):
    """Length of a cycle of 1 to 4 non-work calls repeated at least `repeats` times at the end.

    A cycle containing an edit, build or test is ordinary iteration (edit, test,
    edit, test), so only cycles made entirely of non-work calls count.
    """
    for period in range(1, 5):
        if len(steps) < period * repeats:
            continue
        tail = steps[-period * repeats:]
        pattern = tail[-period:]
        if (all(tail[i]['sig'] == pattern[i % period]['sig'] for i in range(len(tail)))
                and len({c['sig'] for c in pattern}) == period and all(c['cls'] != 'work' for c in pattern)):
            return period
    return 0


def failing_streak(calls):
    """The longest run of one failing call repeated without any other edit, build or test in between."""
    best, streaks = None, {}
    for call in calls:
        if call['cls'] == 'wait':
            continue  # Polling CI or a server exits non-zero until it is ready; that is waiting, not looping.
        if call['cls'] == 'work' and not call.get('error'):
            # Any successful work resets every streak; the agent changed something.
            streaks = {}
            continue
        if call.get('error'):
            count = streaks.get(call['sig'], 0) + 1
            streaks = {call['sig']: count} if call['cls'] == 'work' else {**streaks, call['sig']: count}
            if not best or count > best[1]:
                best = (call, count)
    return best


def no_progress(calls, settings):
    """The same test or build still failing after many attempts and edits, with no passing run in between.

    Edit-and-test iteration is normal until it stops converging: this fires only when one
    command has failed noProgressFailures times across at least noProgressMinutes.
    """
    runs = {}
    for call in calls:
        if call['cls'] != 'work' or call['tool'] not in ('Bash', 'shell', 'exec_command', 'shell_command', 'local_shell', 'container.exec'):
            continue
        failures = runs.setdefault(call['sig'], [])
        if call.get('error'):
            failures.append(call)
        else:
            failures.clear()  # It passed; the agent was converging.
    for sig, failures in runs.items():
        if len(failures) < settings['noProgressFailures']:
            continue
        span = (failures[-1].get('at') or 0) - (failures[0].get('at') or 0)
        if span >= settings['noProgressMinutes'] * 60:
            label = failures[-1]['label'] or failures[-1]['tool']
            return {'kind': 'no-progress', 'label': label, 'count': len(failures),
                    'message': '`' + label + '` has failed ' + str(len(failures)) + ' times over ' + str(int(span // 60))
                    + ' min despite edits in between; the approach is not converging.'}
    return None


def repeats_without_work(calls):
    """Most repeated non-work call since the last work-class call."""
    since_work = []
    for call in calls:
        since_work = [] if call['cls'] == 'work' else since_work + [call]
    counts = Counter(call['sig'] for call in since_work if call['cls'] != 'wait')
    if not counts:
        return None
    sig, count = counts.most_common(1)[0]
    return next(c for c in since_work if c['sig'] == sig), count


def call_window(turns, now):
    """Calls of the longest detector window, sorted once; detectors filter it by their own start."""
    window = recent_calls(turns, now - 2 * WINDOW_SECONDS)
    def since(start):
        return [call for call in window if (call.get('at') or 0) >= start] if start > now - 2 * WINDOW_SECONDS else window
    return since


def detect_loop(turns, now, settings, since=0, calls_since=None):
    """Evidence that the agent is repeating itself, or None.

    Only calls after `since` (the latest Studio intervention) count, so a
    recovered agent is not reported for repetition it has already stopped.
    """
    repeats = settings['loopRepeats']
    start = max(now - WINDOW_SECONDS, since)
    calls = [c for c in (calls_since or (lambda s: recent_calls(turns, s)))(start)[-60:] if not c.get('exempt')]
    streak = failing_streak(calls)
    if streak and streak[1] >= repeats:
        call, count = streak
        label = call['label'] or call['tool']
        return {'kind': 'failing-repeat', 'label': label, 'count': count,
                'message': 'Ran `' + label + '` ' + str(count) + ' times without changing anything in between, failing each time.'}
    stuck = no_progress(calls, settings)
    if stuck:
        return stuck
    steps = [c for c in calls if c['cls'] != 'wait']
    period = repeated_cycle(steps, repeats)
    if period:
        labels = [c['label'] or c['tool'] for c in steps[-period:]]
        return {'kind': 'cycle', 'label': ' → '.join(labels), 'count': repeats,
                'message': 'Repeated the same ' + str(period) + '-step cycle ' + str(repeats) + '+ times without an edit or test: ' + ' → '.join('`' + l + '`' for l in labels) + '.'}
    repeated = repeats_without_work(calls)
    if repeated and repeated[1] >= repeats + 2:
        call, count = repeated
        label = call['label'] or call['tool']
        return {'kind': 'repeat', 'label': label, 'count': count,
                'message': 'Ran `' + label + '` ' + str(count) + ' times without an edit or test in between.'}
    # Replies to periodic checks repeat by design and are not the agent looping.
    recent = [turn for turn in turns if (turn.get('lastAt') or 0) >= start and not turn.get('studioCheck')]
    replies = Counter(reply for turn in recent[:6] for reply in set(turn.get('replies', [])))
    for reply, count in replies.most_common(1):
        if count >= 3:
            return {'kind': 'same-reply', 'label': '', 'count': count,
                    'message': 'Gave the same reply in ' + str(count) + ' of the last ' + str(min(6, len(recent))) + ' turns.'}
    nudges = [turn for turn in recent[:3] if turn.get('fromStudio')]
    if len(nudges) == 3 and not any(turn.get('classes', {}).get('work') for turn in nudges):
        return {'kind': 'nudge-loop', 'label': '', 'count': 3,
                'message': 'The last 3 Studio prompts produced no work; reprompting is not helping.'}
    return None


def detect_admin(turns, now, settings, calls_since=None):
    calls = (calls_since or (lambda s: recent_calls(turns, s)))(now - 2 * WINDOW_SECONDS)
    if len(calls) < 8:
        return None
    admin = [call for call in calls if call['cls'] == 'admin']
    share = len(admin) / len(calls)
    if share < settings['adminShare']:
        return None
    top = Counter(call['label'] or call['tool'] for call in admin).most_common(1)[0][0]
    return {'kind': 'admin', 'share': round(share, 2), 'label': top,
            'message': str(round(share * 100)) + '% of the last ' + str(len(calls)) + ' tool calls were reporting or tracker writes (most often `' + top + '`).'}


def polling_note(turns, now, calls_since=None):
    """How much of the last hour went on polling or sleeping; shown as context, not a problem."""
    calls = (calls_since or (lambda s: recent_calls(turns, s)))(now - WINDOW_SECONDS)
    waits = [call for call in calls if call['cls'] == 'wait']
    if len(calls) < 5 or len(waits) / len(calls) < 0.5:
        return ''
    return str(round(100 * len(waits) / len(calls))) + '% of the last hour\'s calls were polling or waiting.'


def tokens_since(turn, since):
    """Fresh tokens recorded after `since`, using the turn's five-minute buckets when present."""
    buckets = turn.get('buckets')
    if buckets:
        return sum(value for start, value in buckets.items() if int(start) + 300 > since)
    return fresh_tokens(turn) if (turn.get('lastAt') or 0) >= since else 0


def detect_burn(turns, now, settings, calls_since=None):
    since = now - WINDOW_SECONDS
    tokens = sum(tokens_since(turn, since) for turn in turns)
    work = sum(1 for call in (calls_since or (lambda s: recent_calls(turns, s)))(since) if call['cls'] == 'work') \
        or any((turn.get('lastWorkAt') or 0) >= since for turn in turns)
    if tokens < settings['burnTokensPerHour'] or work:
        return None
    return {'kind': 'burn', 'tokens': tokens,
            'message': format(tokens, ',') + ' fresh tokens in the last hour without any edit, build or test.'}


# Tools that wait for the person at the keyboard; a turn paused on one is not stuck.
PERSON_TOOLS = {'AskUserQuestion', 'ExitPlanMode'}


def waiting_on_person(turns):
    current = next((turn for turn in turns if turn.get('status') == 'open'), None)
    last = current['calls'][-1] if current and current.get('calls') else None
    if last and last.get('open') and last.get('tool') in PERSON_TOOLS:
        return 'Waiting for you to answer `' + last['tool'] + '` in the session.'
    return ''


def detect_stall(session, turns, now, settings, open_work):
    status = session.get('status')
    last = max((turn.get('lastAt') or 0 for turn in turns), default=0) or session.get('session', {}).get('updatedAt') or 0
    quiet = now - last if last else None
    current = next((turn for turn in turns if turn.get('status') == 'open'), None)
    running = bool(current and current.get('calls') and current['calls'][-1].get('open'))
    # A long build or test writes nothing until it finishes, so allow it three times as long.
    limit = settings['silentMinutes'] * 60 * (3 if running else 1)
    if status == 'attention' and open_work:
        return {'kind': 'errored-turn', 'minutes': int(quiet // 60) if quiet else None,
                'message': 'The last turn ended in an error (often a rate or usage limit, or a crash) and work is still open.'}
    if status == 'closed' and open_work and session.get('backend', 'codex') == 'codex':
        # A closed Codex session cannot receive Studio's messages; only a person can reopen it.
        return {'kind': 'closed-session', 'minutes': int(quiet // 60) if quiet else None,
                'message': 'The session is closed with open work; Studio cannot message it until it is reopened.'}
    if status == 'working' and quiet is not None and quiet >= limit:
        detail = ' waiting on `' + (current['calls'][-1]['label'] or current['calls'][-1]['tool']) + '`' if running else ' with no new tool calls or tokens'
        return {'kind': 'silent-turn', 'minutes': int(quiet // 60),
                'message': 'Turn has been open ' + str(int(quiet // 60)) + ' min' + detail + '.'}
    if status in ('idle', 'closed') and open_work and (quiet is None or quiet >= settings['stallMinutes'] * 60):
        minutes = int(quiet // 60) if quiet is not None else None
        return {'kind': 'idle-with-work', 'minutes': minutes,
                'message': ('Idle for ' + str(minutes) + ' min' if minutes is not None else 'Idle') + ' with assigned open work.'}
    return None


def turn_waste(turn, settings):
    """Split a turn's fresh tokens into productive and wasted shares by what its calls did."""
    tokens = fresh_tokens(turn)
    waste = dict.fromkeys(WASTE_KINDS, 0)
    if not tokens:
        return waste
    classes = turn.get('classes', {})
    calls = max(1, turn.get('callCount', 0))
    if turn.get('status') == 'aborted':
        waste['aborted'] = tokens
        return waste
    if turn.get('fromStudio') and not classes.get('work'):
        waste['nudge'] = tokens
        return waste
    # Only repeats of non-work calls are waste; re-running a test after an edit is progress.
    # Polling is counted as waiting below, not as a loop, and each call counts as one kind of waste only.
    candidates = [call for call in turn.get('calls', []) if call['cls'] not in ('work', 'wait') and not call.get('exempt')]
    signatures = Counter(call['sig'] for call in candidates)
    looped, seen = Counter(), Counter()
    for call in candidates:
        seen[call['sig']] += 1
        if signatures[call['sig']] >= settings['loopRepeats'] and seen[call['sig']] > 1:
            looped[call['cls']] += 1
    repeated = sum(looped.values())
    # Re-running the same failing command with nothing changed in between is also a loop.
    streak = failing_streak(turn.get('calls', []))
    if streak and streak[1] >= settings['loopRepeats'] and streak[0]['cls'] == 'work':
        repeated += streak[1] - 1
    share = lambda count: round(tokens * max(0, count) / calls)
    waste['loop'] = share(min(repeated, calls))
    waste['admin'] = share(classes.get('admin', 0) - looped['admin'])
    waste['wait'] = share(classes.get('wait', 0))
    # Rounding and partial call lists must never report more waste than the turn spent.
    over = sum(waste.values()) - tokens
    if over > 0:
        waste['wait'] = max(0, waste['wait'] - over)
    return waste


def cached_waste(turn, settings):
    """Waste of a turn, kept on the turn: health reuses unchanged turns from tick to tick."""
    key = (settings['loopRepeats'], turn.get('lastAt'), turn.get('status'), turn.get('callCount'))
    cached = turn.get('_waste')
    if not cached or cached[0] != key:
        cached = turn['_waste'] = (key, turn_waste(turn, settings))
    return cached[1]


def last_work(turns):
    """Latest successful work call; only work that succeeded counts, a failing test run is not recovery."""
    best = max((turn.get('lastWorkAt') or 0 for turn in turns), default=0)
    for turn in turns:
        if (turn.get('lastAt') or 0) <= best:
            continue
        # Calls are in order, so the last successful one is the turn's latest.
        for call in reversed(turn.get('calls', [])):
            if call['cls'] == 'work' and not call.get('error'):
                best = max(best, call.get('at') or 0)
                break
    return best or None


def assess(session, turns, now, settings, open_work=False, blocked=None, since_intervention=0, observed=True):
    """Health for one agent from its observation and turns (newest first).

    `observed` is False when Studio cannot read the agent's transcript (remote or
    unlinked), so the absence of turns is not mistaken for an idle agent.
    """
    turns = sorted(turns, key=lambda turn: turn.get('lastAt') or 0, reverse=True)
    status = session.get('status') or 'unknown'
    signals = []
    calls_since = call_window(turns, now)
    loop = detect_loop(turns, now, settings, since_intervention, calls_since)
    burn = detect_burn(turns, now, settings, calls_since)
    stall = None if blocked or waiting_on_person(turns) else detect_stall(session, turns, now, settings, open_work)
    admin = detect_admin(turns, now, settings, calls_since)
    for state, evidence in (('looping', loop), ('burning', burn), ('stalled', stall), ('admin-heavy', admin)):
        if evidence:
            signals.append({'state': state, **evidence})
    person = waiting_on_person(turns)
    if signals:
        state = signals[0]['state']
    elif person:
        state = 'waiting'
        signals.append({'state': 'waiting', 'kind': 'needs-you', 'message': person})
    elif blocked:
        state = 'waiting'
        signals.append({'state': 'waiting', 'kind': 'blocked', 'message': blocked.get('reason') or 'Reported blocked.'})
    elif not observed and not turns:
        state = 'no-telemetry'
    elif status == 'working' or any((turn.get('lastAt') or 0) >= now - 600 for turn in turns):
        state = 'productive'
    elif status in ('idle', 'attention', 'closed') and observed:
        state = 'idle' if status != 'closed' else 'offline'
    else:
        state = 'offline'
    hour_start = now - WINDOW_SECONDS
    day = [turn for turn in turns if (turn.get('lastAt') or 0) >= now - 86400]
    waste = dict.fromkeys(WASTE_KINDS, 0)
    for turn in day:
        for kind, value in cached_waste(turn, settings).items():
            waste[kind] += value
    classes = Counter()
    for turn in day:
        classes.update(turn.get('classes') or Counter(call['cls'] for call in turn.get('calls', [])))
    current = next((turn for turn in turns if turn.get('status') == 'open'), None)
    return {
        'state': state,
        'signals': signals,
        'sessionStatus': status,
        'lastActivityAt': max((turn.get('lastAt') or 0 for turn in turns), default=None) or None,
        # Only work that succeeded counts: a failing test run is not recovery.
        'lastWorkAt': last_work(turns),
        'currentTurnStartedAt': current['startedAt'] if current else None,
        'tokensLastHour': sum(tokens_since(turn, hour_start) for turn in turns),
        'polling': polling_note(turns, now, calls_since),
        'tokens24h': sum(fresh_tokens(turn) for turn in day),
        'cacheRead24h': sum(turn.get('tokens', {}).get('cacheRead', 0) for turn in day),
        'turns24h': len(day),
        'calls24h': sum(turn.get('callCount', len(turn.get('calls', []))) for turn in day),
        'classes24h': {name: classes.get(name, 0) for name in ('work', 'explore', 'admin', 'wait', 'other')},
        'errors24h': sum(turn.get('errors', 0) for turn in day),
        'waste24h': waste,
        'wasteTokens24h': sum(waste.values()),
    }


class Health:
    """Tracks health state transitions per agent and records them as observations."""

    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.lock = threading.RLock()
        self.current = {}
        self.sends = {}
        self.turn_cache = {}
        with closing(app.progress.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS health_state (fleet TEXT, agent TEXT, state TEXT, since REAL, PRIMARY KEY(fleet, agent))')

    @staticmethod
    def persisted(db, key):
        """The state and start time recorded before a restart, so durations survive it."""
        row = db.execute('SELECT state, since FROM health_state WHERE fleet=? AND agent=?', key).fetchone()
        return {'state': row['state'], 'since': row['since']} if row else None

    def last_intervention(self, fleet_id, agent_id):
        """When Studio last messaged this agent by any route; repetition before that has been acted on."""
        return self.sends.get(agent_id, 0)

    def recent_sends(self):
        """Latest Studio delivery per agent across triggers, automations and dispatch, computed once a tick."""
        latest = {}
        controls = getattr(self.app, 'controls', None)
        if not controls:
            return latest
        with controls.lock:
            for receipt in controls.receipts.values():
                for target in receipt.get('targets', []):
                    if target.get('status') in ('queued', 'started'):
                        latest[target['agentId']] = max(latest.get(target['agentId'], 0), receipt.get('createdAt', 0))
        return latest

    @staticmethod
    def fleet_context(db, fleet_id):
        """What open_work needs that is the same for every agent in a fleet, read once a tick."""
        tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        closed = {row['url'] for row in db.execute('SELECT url, data FROM tasks WHERE fleet=?', (fleet_id,))
                  if json.loads(row['data']).get('state') == 'closed'} if 'tasks' in tables else set()
        return {'tables': tables, 'closed': closed}

    def open_work(self, db, fleet_id, agent, turns=(), context=None):
        """Assigned, unfinished work for this agent, and its current blocker if any.

        A blocked report holds until the agent reports unblocked, done or hand-off,
        or until it is seen doing work again after the report.
        """
        from studio_progress import task_key
        context = context or self.fleet_context(db, fleet_id)
        closed, tables = context['closed'], context['tables']
        note = None
        if 'agent_notes' in tables:
            row = db.execute('SELECT data FROM agent_notes WHERE fleet=? AND agent=?', (fleet_id, agent['id'])).fetchone()
            note = json.loads(row['data']) if row else None
        # A done or hand-off report closes everything the agent held before it.
        settled = note['at'] if note and note['kind'] in ('done', 'handoff') else 0
        if note and note['kind'] == 'blocked':
            resumed = any(call['cls'] == 'work' and (call.get('at') or 0) > note['at']
                          for turn in turns if (turn.get('lastAt') or 0) > note['at'] for call in turn.get('calls', []))
            if not resumed:
                return True, {'reason': note['note'], 'at': note['at']}
        # An unblocked or resumed agent falls through to the normal checks below.
        work, blocked = False, None
        if 'dispatch_claims' in tables:
            # A ticket Studio offered or routed to the agent is its open work until it reports back.
            work = bool(db.execute("SELECT 1 FROM dispatch_claims WHERE fleet=? AND agent=? AND status!='done' AND at>?",
                                   (fleet_id, agent['id'], settled)).fetchone())
        for row in db.execute('SELECT data FROM agent_tasks WHERE fleet=? AND agent=?', (fleet_id, agent['id'])):
            report = json.loads(row['data'])
            if (report.get('taskUrl') in closed or report.get('phase') == 'submitted' or report.get('kind') == 'work-finished'
                    or report['at'] <= settled):
                continue
            work = True
            if report.get('blockedSince'):
                blocked = report
        cutoff = max(self.clock() - 86400, settled)
        if not work and hasattr(self.app, 'automation'):
            work = any(f.get('recipientId') == agent['id'] and f['fleetId'] == fleet_id and f['status'] == 'sent'
                       and f.get('ticket') and task_key(f['ticket']) not in closed and f['at'] > cutoff
                       for f in self.app.automation.data['fires'])
        if not work:
            for row in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fleet_id,)):
                handoff = json.loads(row['data'])
                if (handoff.get('targetAgentId') == agent['id'] and handoff.get('status') == 'queued'
                        and handoff.get('createdAt', 0) > cutoff and task_key(handoff.get('ticket') or '') not in closed
                        and not handoff.get('submittedAt')):
                    work = True
                    break
        return work, blocked

    def tick(self, state, monitor):
        now = self.clock()
        recent = {fleet['id']: self.recent_turns(fleet['id'], now - 86400) for fleet in state['fleets']}
        self.sends = self.recent_sends()
        with self.lock, self.app.progress.lock, closing(self.app.progress.connect()) as db, db:
            for fleet in state['fleets']:
                settings = clean_health_settings(fleet.get('health'))
                context = self.fleet_context(db, fleet['id'])
                by_agent = {}
                for turn in recent[fleet['id']]:
                    by_agent.setdefault(turn['agentId'], []).append(turn)
                for agent in fleet['agents']:
                    session = monitor.get('sessions', {}).get(session_key(agent), {}) if agent.get('sessionId') else {}
                    turns = by_agent.get(agent['id'], [])
                    work, blocked = self.open_work(db, fleet['id'], agent, turns, context)
                    observed = bool(agent.get('sessionId')) and self.app.telemetry.covered(fleet['id'], agent)
                    health = assess(session, turns, now, settings, work, blocked,
                                    self.last_intervention(fleet['id'], agent['id']), observed)
                    health['openWork'] = work
                    key = (fleet['id'], agent['id'])
                    previous = self.current.get(key) or self.persisted(db, key)
                    if previous and previous['state'] == health['state']:
                        health['since'] = previous['since']
                    else:
                        db.execute('INSERT OR REPLACE INTO health_state VALUES(?,?,?,?)', (fleet['id'], agent['id'], health['state'], now))
                        health['since'] = now
                        self.app.progress.record(db, fleet['id'], 'health', agent['id'] + ':' + str(now), now,
                                                 {'agentId': agent['id'], 'state': health['state'],
                                                  'previous': previous['state'] if previous else None,
                                                  'signals': health['signals']})
                    self.current[key] = health
            present = {(fleet['id'], agent['id']) for fleet in state['fleets'] for agent in fleet['agents']}
            for key in set(self.current) - present:
                del self.current[key]

    def prune(self, db, fleet):
        agents = {agent['id'] for agent in fleet['agents']}
        for row in db.execute('SELECT agent FROM health_state WHERE fleet=?', (fleet['id'],)).fetchall():
            if row['agent'] not in agents:
                db.execute('DELETE FROM health_state WHERE fleet=? AND agent=?', (fleet['id'], row['agent']))
        # In-memory state is trimmed by tick(): taking self.lock here, under the progress lock,
        # would invert tick's lock order.

    def recent_turns(self, fleet_id, since):
        """The last day's turns, reloading from the database only those that changed since the last tick."""
        cache = self.turn_cache.setdefault(fleet_id, {})
        index = {(session, turn_id): (agent, last, status) for session, turn_id, agent, last, status in self.app.telemetry.index(fleet_id, since)}
        for key in set(cache) - set(index):
            del cache[key]
        stale = [key for key, marker in index.items() if key not in cache or cache[key][0] != marker]
        for turn in self.app.telemetry.load_turns(stale, {key: cache[key][1] for key in stale if key in cache}):
            key = (turn['session'], turn['id'])
            cache[key] = (index[key], turn)
        return [turn for _, turn in cache.values()]

    def snapshot(self, fleet_id):
        fleet = self.app.store.fleet(fleet_id)
        with self.lock:
            rows = []
            for agent in fleet['agents']:
                health = deepcopy(self.current.get((fleet_id, agent['id'])))
                rows.append({'agentId': agent['id'], 'name': agent['name'], 'roleIds': agent.get('roleIds', []),
                             'backend': agent.get('backend', 'codex'), 'hostId': agent.get('hostId', 'local'),
                             'health': health})
        status = self.app.deployments.draft_status(fleet)
        for row in rows:
            row['briefStale'] = row['agentId'] in status['staleAgents']
        return {'fleetId': fleet_id, 'observedAt': self.clock(), 'agents': rows, 'deployment': status,
                'settings': clean_health_settings(fleet.get('health')), 'progressSettings': fleet.get('progress') or {}}

    def get(self, fleet_id, agent_id):
        with self.lock:
            return deepcopy(self.current.get((fleet_id, agent_id)))

    def history(self, fleet_id, since):
        with self.app.progress.lock, closing(self.app.progress.connect()) as db:
            rows = db.execute("SELECT at, data FROM observations WHERE fleet=? AND category='health' AND at>=? ORDER BY at",
                              (fleet_id, since)).fetchall()
        return [{'at': row['at'], **json.loads(row['data'])} for row in rows]
