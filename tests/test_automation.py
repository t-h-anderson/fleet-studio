"""Health automation, throttles and Claude delivery holds. Native sends are mocked."""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_design as fixtures
from fleet_studio import Application, ValidationError
from studio_automation import backtest, clean_automations


def rule(**changes):
    base = {'id': str(uuid.uuid4()), 'name': 'Break loops', 'when': {'signal': 'looping', 'forMinutes': 0},
            'action': 'reprompt', 'cooldownMinutes': 1, 'maxFires': 1, 'thenAction': 'escalate'}
    base.update(changes)
    return base


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class BacktestTests(unittest.TestCase):
    def test_replay_respects_delay_cooldown_and_limit(self):
        [cleaned] = clean_automations([rule(when={'signal': 'stalled', 'forMinutes': 10}, cooldownMinutes=20, maxFires=2)], set())
        now = 100000
        history = [{'at': now - 7200, 'agentId': 'a', 'state': 'stalled'},
                   {'at': now - 3600, 'agentId': 'a', 'state': 'productive'},
                   {'at': now - 1800, 'agentId': 'b', 'state': 'stalled'},
                   {'at': now - 1500, 'agentId': 'b', 'state': 'idle'}]
        result = backtest(cleaned, history, now)
        # Agent a: fires at +10 and +30 min, then the limit is reached and the fallback runs once.
        self.assertEqual(result['agents'], {'a': 2})
        self.assertEqual(result['fallbacks'], 1)
        # Agent b's stall ended after 5 min, before the 10 min delay.
        self.assertEqual(result['fires'], 2)

    def test_flapping_state_gets_one_fallback_like_the_live_rule(self):
        [cleaned] = clean_automations([rule(when={'signal': 'stalled', 'forMinutes': 0}, cooldownMinutes=10, maxFires=1)], set())
        now = 100000
        history = []
        for i in range(6):
            history += [{'at': now - 3600 + i * 600, 'agentId': 'a', 'state': 'stalled'},
                        {'at': now - 3600 + i * 600 + 300, 'agentId': 'a', 'state': 'productive'}]
        self.assertEqual(backtest(cleaned, history, now)['fallbacks'], 1)

    def test_a_named_agent_decides_who_a_rule_watches(self):
        from studio_automation import watches
        alice, bob = {'id': 'a', 'roleIds': ['writer']}, {'id': 'b', 'roleIds': ['reviewer']}
        self.assertTrue(watches({'roleId': 'reviewer', 'agentId': 'a'}, alice))
        self.assertFalse(watches({'roleId': 'reviewer', 'agentId': 'a'}, bob))
        self.assertEqual([watches({'roleId': 'writer'}, x) for x in (alice, bob)], [True, False])

    def test_validation(self):
        with self.assertRaisesRegex(ValueError, 'health signal'):
            clean_automations([rule(when={'signal': 'bored'})], set())
        with self.assertRaisesRegex(ValueError, 'roles'):
            clean_automations([rule(roleId='missing')], set())
        [cleaned] = clean_automations([rule()], set())
        self.assertTrue(cleaned['enabled'])
        self.assertEqual(cleaned['windowHours'], 24)


class AutomationTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor

    def configure(self, app, **fleet_changes):
        state = app.store.snapshot()
        state['fleets'][0].update(fleet_changes)
        app.store.save(state)
        return app.store.fleet(state['fleets'][0]['id'])

    def set_health(self, app, fleet, agent, state, since, **extra):
        app.health.current[(fleet['id'], agent['id'])] = {
            'state': state, 'since': since, 'signals': [{'state': state, 'message': 'Ran `pytest` 5 times, failing each time.'}],
            'tokensLastHour': extra.pop('tokens', 0), **extra}

    def test_active_children_hold_all_parent_interventions(self):
        app, fleet = self.app()
        agent = fleet['agents'][0]
        monitor = self.monitor(fleet)
        monitor['sessions'][agent['sessionId']]['subagents'] = {
            'known': True, 'observedAt': time.time(), 'active': 1, 'uncertain': 0}
        app.monitor = monitor
        health = {'state': 'looping', 'signals': []}
        with patch.object(app.automation, 'stop_turn') as stop, patch.object(app.automation, 'send') as send:
            for action in ('reprompt', 'handoff', 'stop-turn', 'stop', 'pause'):
                fire = {'action': action, 'status': 'pending'}
                app.automation.execute(fleet, {}, agent, health, fire, monitor)
                self.assertEqual(fire['status'], 'held')
                self.assertIn('Subagents are working', fire['detail'])
            stop.assert_not_called()
            send.assert_not_called()
        self.assertIn('Subagents are working', app.triggers.hold_reason([agent], monitor))

    def test_native_interrupt_rechecks_child_activity(self):
        app, fleet = self.app()
        agent = fleet['agents'][0]
        backend = app.runtimes.local['codex']
        session = {'turnId': 'parent-turn', 'turnStatus': 'inProgress',
                   'subagents': {'known': True, 'observedAt': time.time(), 'active': 1}}
        from studio_agents import NotReady
        with patch.object(backend, 'observe', return_value={'sessions': {agent['sessionId']: session}}), patch('studio_control.app_server') as rpc:
            with self.assertRaises(NotReady):
                backend.interrupt(agent['sessionId'])
            rpc.assert_not_called()

    def test_codex_without_subagent_metadata_holds_only_automatic_stops(self):
        app, fleet = self.app()
        agent = fleet['agents'][0]
        monitor = self.monitor(fleet)
        unsupported = {'known': False, 'supported': False, 'observedAt': time.time(), 'active': 0, 'uncertain': 0}
        monitor['sessions'][agent['sessionId']]['subagents'] = unsupported
        app.monitor = monitor
        health = {'state': 'looping', 'signals': [], 'since': time.time() - 60}
        with patch.object(app.automation, 'stop_turn') as stop, patch.object(app.automation, 'send') as send:
            for action in ('stop-turn', 'stop'):
                fire = {'action': action, 'status': 'pending'}
                app.automation.execute(fleet, {}, agent, health, fire, monitor)
                self.assertEqual(fire['status'], 'held')
                self.assertIn('unavailable on this Codex version', fire['detail'])
            stop.assert_not_called()
            fire = {'action': 'reprompt', 'status': 'pending'}
            app.automation.execute(fleet, rule(message=''), agent, health, fire, monitor)
            self.assertNotEqual(fire['status'], 'held')
            send.assert_called()
        self.assertNotIn('Subagent', app.triggers.hold_reason([agent], monitor))
        # The operator's Stop still reaches Codex.
        backend = app.runtimes.local['codex']
        session = {'turnId': 'parent-turn', 'turnStatus': 'inProgress', 'subagents': unsupported}
        with patch.object(backend, 'observe', return_value={'sessions': {agent['sessionId']: session}}), patch('studio_control.app_server') as rpc:
            backend.interrupt(agent['sessionId'])
            rpc.assert_called_once()

    def test_reprompt_then_fallback_then_silence(self):
        app, fleet = self.app()
        clock = Clock()
        app.automation.clock = clock
        fleet = self.configure(app, automations=[rule()])
        writer = fleet['agents'][0]
        self.set_health(app, fleet, writer, 'looping', clock.now - 10)
        monitor = self.monitor(fleet)
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.automation.tick(app.store.snapshot(), monitor)
            send.assert_called_once()
            message = app.controls.previews[send.call_args[0][0]]['targets'][0]['text']
            self.assertTrue(message.startswith('[Fleet Studio] Break loops'))
            self.assertIn('Ran `pytest` 5 times', message)
            [fire] = app.automation.data['fires']
            self.assertEqual(fire['status'], 'sending')
            fire['status'] = 'sent'
            # Within the cooldown nothing happens.
            clock.now += 30
            app.automation.tick(app.store.snapshot(), monitor)
            self.assertEqual(len(app.automation.data['fires']), 1)
            # The limit is reached, so the fallback escalates once.
            clock.now += 60
            app.automation.tick(app.store.snapshot(), monitor)
            self.assertEqual(app.automation.data['fires'][-1]['action'], 'escalate')
            self.assertTrue(app.automation.data['fires'][-1]['fallback'])
            self.assertEqual(len(app.automation.snapshot(fleet['id'])['escalations']), 1)
            clock.now += 600
            app.automation.tick(app.store.snapshot(), monitor)
            self.assertEqual(len(app.automation.data['fires']), 2)
            send.assert_called_once()
        attention = app.progress.snapshot(fleet['id'])['attention']
        self.assertIn('escalation', [a['kind'] for a in attention])

    def test_handoff_picks_a_healthy_agent_and_parks_the_source(self):
        app, fleet = self.app()
        clock = Clock()
        app.automation.clock = clock
        writer, integrator = fleet['agents']
        fleet = self.configure(app, automations=[rule(action='handoff', roleId=writer['roleIds'][0],
                                                      handoffRoleId=integrator['roleIds'][0], maxFires=0)])
        self.set_health(app, fleet, writer, 'looping', clock.now - 10)
        self.set_health(app, fleet, integrator, 'idle', clock.now - 10)
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.automation.tick(app.store.snapshot(), self.monitor(fleet))
            preview = app.controls.previews[send.call_args[0][0]]
        self.assertEqual(preview['targets'][0]['agentId'], integrator['id'])
        self.assertIn(writer['name'] + ' is looping', preview['targets'][0]['text'])
        # The source is parked only once the hand-off is confirmed delivered.
        self.assertEqual(app.automation.paused_reason(fleet['id'], writer), '')
        app.controls.receipts['r1'] = {'id': 'r1', 'status': 'completed', 'targets': [{'status': 'queued', 'detail': 'Queued.'}]}
        app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        self.assertIn('paused by automation', app.automation.paused_reason(fleet['id'], writer))

    def test_throttle_holds_and_records_time(self):
        app, fleet = self.app()
        clock = Clock()
        app.automation.clock = clock
        fleet = self.configure(app, throttles={'maxWorkingAgents': 1})
        writer, integrator = fleet['agents']
        monitor = self.monitor(fleet)
        monitor['sessions'][integrator['sessionId']]['status'] = 'working'
        self.assertIn('Throttled', app.automation.hold(fleet['id'], [writer], monitor))
        clock.now += 120
        monitor['sessions'][integrator['sessionId']]['status'] = 'idle'
        self.assertEqual(app.automation.hold(fleet['id'], [writer], monitor), '')
        self.assertEqual(app.automation.snapshot(fleet['id'])['throttledSeconds24h'], 120)

    def test_throttle_validation(self):
        app, fleet = self.app()
        with self.assertRaises(ValidationError):
            self.configure(app, throttles={'maxWorkingAgents': 0})
        with self.assertRaises(ValidationError):
            self.configure(app, alerts={'webhookUrl': 'http://example.com/hook'})

    def test_claude_agents_receive_automatic_turns_only_when_closed(self):
        app, fleet = self.app()
        agent = dict(fleet['agents'][0], backend='claude-code')
        now = time.time()
        item = {'status': 'closed', 'pids': [], 'livenessKnown': True, 'observedAt': now}
        monitor = {'observedAt': now, 'sessions': {'local:claude-code:' + agent['sessionId']: item}}
        self.assertEqual(app.triggers.hold_reason([agent], monitor), '')
        item.update(status='idle', pids=[5])
        self.assertIn('open in a Claude Code client', app.triggers.hold_reason([agent], monitor))
        item.update(status='working', pids=[])
        self.assertIn('mid-turn', app.triggers.hold_reason([agent], monitor))
        item.update(status='closed', livenessKnown=False)
        self.assertIn('unknown process ownership', app.triggers.hold_reason([agent], monitor))


class LifecycleTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor
    configure = AutomationTests.configure
    set_health = AutomationTests.set_health

    def test_held_delivery_waits_without_using_the_budget(self):
        from studio_control import AutomationHeld
        app, fleet = self.app()
        clock = Clock()
        app.automation.clock = clock
        fleet = self.configure(app, automations=[rule(maxFires=1)])
        writer = fleet['agents'][0]
        self.set_health(app, fleet, writer, 'looping', clock.now - 10)
        with patch.object(app.controls, 'send', side_effect=AutomationHeld('Writer A is mid-turn.')):
            for _ in range(3):
                clock.now += 120
                app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        self.assertEqual([f['status'] for f in app.automation.data['fires']], ['held'])
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.automation.tick(app.store.snapshot(), self.monitor(fleet))
            send.assert_called_once()
        self.assertEqual(app.automation.data['fires'][0]['status'], 'sending')
        self.assertFalse(any(f['action'] == 'escalate' for f in app.automation.data['fires']))

    def test_handoff_without_a_candidate_escalates(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        fleet = self.configure(app, automations=[rule(action='handoff', roleId=writer['roleIds'][0])])
        self.set_health(app, fleet, writer, 'looping', time.time() - 10)
        app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        [fire] = app.automation.data['fires']
        self.assertEqual(fire['status'], 'failed')
        self.assertIn('Escalated instead', fire['detail'])
        self.assertEqual(len(app.automation.snapshot(fleet['id'])['escalations']), 1)

    def test_sending_fire_settles_after_the_agent_recovers(self):
        app, fleet = self.app()
        fleet = self.configure(app, automations=[rule()])
        writer = fleet['agents'][0]
        self.set_health(app, fleet, writer, 'looping', time.time() - 10)
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}):
            app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        self.set_health(app, fleet, writer, 'productive', time.time(), lastWorkAt=time.time() + 1)
        app.controls.receipts['r1'] = {'id': 'r1', 'status': 'completed', 'targets': [{'status': 'queued', 'detail': 'Queued.'}]}
        app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        [fire] = app.automation.data['fires']
        self.assertEqual((fire['status'], fire['outcome']), ('sent', 'recovered'))

    def test_undeliverable_direct_handoff_does_not_stop_other_rules(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        app.automation.data['fires'].append({'id': 'h', 'fleetId': fleet['id'], 'ruleId': 'agent-handoff', 'ruleName': 'Agent hand-off',
            'agentId': writer['id'], 'agentName': writer['name'], 'action': 'handoff', 'fallback': False, 'at': time.time(),
            'episode': None, 'healthState': 'handoff', 'evidence': 'x', 'status': 'held', 'detail': '', 'outcome': None,
            'recipientId': integrator['id'], 'recipientName': integrator['name'], 'ticket': '', 'message': 'hi'})
        fleet = self.configure(app, automations=[rule()])
        self.set_health(app, fleet, writer, 'looping', time.time() - 10)
        with patch.object(app.controls, 'preview', side_effect=[ValueError('needs a linked session'), {'id': 'p'}]), \
                patch.object(app.controls, 'send', return_value={'id': 'r1'}):
            app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        statuses = {f['id']: f['status'] for f in app.automation.data['fires']}
        self.assertEqual(statuses['h'], 'failed')
        self.assertIn('sending', statuses.values())

    def test_long_held_delivery_escalates_once_and_keeps_waiting(self):
        from studio_control import AutomationHeld
        app, fleet = self.app()
        clock = Clock()
        app.automation.clock = clock
        fleet = self.configure(app, automations=[rule(cooldownMinutes=5)])
        writer = fleet['agents'][0]
        self.set_health(app, fleet, writer, 'looping', clock.now - 10)
        with patch.object(app.controls, 'send', side_effect=AutomationHeld('Writer A is open in a client.')):
            for _ in range(4):
                app.automation.tick(app.store.snapshot(), self.monitor(fleet))
                clock.now += 240
        self.assertEqual(len(app.automation.snapshot(fleet['id'])['escalations']), 1)
        self.assertEqual(app.automation.data['fires'][0]['status'], 'held')

    def test_closed_session_escalates_instead_of_reprompting(self):
        app, fleet = self.app()
        fleet = self.configure(app, automations=[rule(when={'signal': 'stalled', 'forMinutes': 0})])
        writer = fleet['agents'][0]
        app.health.current[(fleet['id'], writer['id'])] = {'state': 'stalled', 'since': time.time() - 10,
            'signals': [{'state': 'stalled', 'kind': 'closed-session', 'message': 'closed'}]}
        with patch.object(app.controls, 'send') as send:
            app.automation.tick(app.store.snapshot(), self.monitor(fleet))
            send.assert_not_called()
        self.assertEqual(app.automation.data['fires'][0]['action'], 'escalate')

    def test_stop_action_stops_the_turn_and_mutes(self):
        app, fleet = self.app()
        fleet = self.configure(app, automations=[rule(action='stop')])
        writer = fleet['agents'][0]
        self.set_health(app, fleet, writer, 'looping', time.time() - 10)
        with patch.object(type(app.runtimes.local['codex']), 'interrupt', return_value='Asked Codex to stop the running turn.'):
            app.automation.tick(app.store.snapshot(), self.monitor(fleet))
        [fire] = app.automation.data['fires']
        self.assertEqual(fire['status'], 'sent')
        self.assertIn('muted', fire['detail'])
        self.assertIn('paused by automation', app.automation.paused_reason(fleet['id'], writer))

    def test_one_failing_monitor_step_does_not_stop_the_others(self):
        app, fleet = self.app()
        with patch.object(app.health, 'tick', side_effect=RuntimeError('boom')), patch.object(app.automation, 'tick') as automation, \
                patch.object(app.triggers, 'tick') as triggers:
            failures = app.run_steps(app.store.snapshot(), {'sessions': {}})
        automation.assert_called_once()
        triggers.assert_called_once()
        self.assertEqual(failures, ['Health refresh failed: boom'])


class EndToEndTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp

    def tearDown(self):
        os.environ.pop('CLAUDE_CONFIG_DIR', None)
        fixtures.DesignTests.tearDown(self)

    def test_looping_claude_transcript_triggers_one_reprompt(self):
        claude = self.root / 'claude'
        session = str(uuid.uuid4())
        project = claude / 'projects' / 'repo'
        project.mkdir(parents=True)
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 60))
        rows = [{'type': 'user', 'uuid': 'u1', 'timestamp': stamp, 'cwd': str(self.root),
                 'message': {'role': 'user', 'content': 'Fix the build'}}]
        for index in range(5):
            rows.append({'type': 'assistant', 'timestamp': stamp, 'message': {'id': 'm' + str(index), 'stop_reason': 'tool_use',
                         'usage': {'input_tokens': 100, 'output_tokens': 10},
                         'content': [{'type': 'tool_use', 'id': 't' + str(index), 'name': 'Bash', 'input': {'command': 'make build'}}]}})
            rows.append({'type': 'user', 'timestamp': stamp, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 't' + str(index), 'is_error': True, 'content': 'error'}]}})
        rows.append({'type': 'assistant', 'timestamp': stamp, 'message': {'id': 'end', 'stop_reason': 'end_turn',
                     'usage': {'input_tokens': 1, 'output_tokens': 1}, 'content': [{'type': 'text', 'text': 'Still failing.'}]}})
        (project / (session + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        os.environ['CLAUDE_CONFIG_DIR'] = str(claude)
        app = Application(self.root)
        state = app.store.snapshot()
        fleet = state['fleets'][0]
        fleet['agents'][0].update(backend='claude-code', sessionId=session)
        fleet['automations'] = [rule(maxFires=1)]
        app.store.save(state)
        state = app.store.snapshot()
        app.telemetry.tick(state)
        observed = {'status': 'closed', 'pids': [], 'livenessKnown': True, 'observedAt': time.time()}
        monitor = {'observedAt': time.time(), 'sessions': {'local:claude-code:' + session: observed}}
        app.health.tick(state, monitor)
        health = app.health.get(fleet['id'], fleet['agents'][0]['id'])
        self.assertEqual(health['state'], 'looping')
        self.assertEqual(health['calls24h'], 5)
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.automation.tick(state, monitor)
            app.automation.tick(state, monitor)
            send.assert_called_once()
            text = app.controls.previews[send.call_args[0][0]]['targets'][0]['text']
        self.assertIn('`make build` 5 times', text)


if __name__ == '__main__':
    unittest.main()
