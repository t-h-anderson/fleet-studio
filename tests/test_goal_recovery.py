"""Native activation and queue effects are mocked; no real goals are touched."""
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import Mock, patch

from studio_goal_recovery import failure, recover
from studio_automation import clean_automations, default_automations
import test_goals as goal_fixtures


class RecoveryTests(unittest.TestCase):
    setUp = goal_fixtures.GoalTests.setUp
    tearDown = goal_fixtures.GoalTests.tearDown
    setup_goal = goal_fixtures.GoalTests.setup_goal

    def setup_recovery(self):
        app, fleet = self.setup_goal()
        app.launch_enabled = True
        self.native['updatedAt'] = self.now - 1000
        self.session.update(status='attention', pids=[123], turnId='failed-turn',
                            turnStatus='failed', turnCompletedAt=self.now - 1000,
                            subagents={'known': True, 'observedAt': self.now, 'active': 0, 'uncertain': 0, 'children': []})
        self.rule = {'id': 'recovery-rule', 'enabled': True, 'maxFires': 3, 'cooldownMinutes': 1,
                     'when': {'forMinutes': 0}}
        self.error = {'kind': 'gateway', 'at': self.now - 1000}
        self.backend = Mock()
        self.backend.send.return_value = ('queued', 'Acknowledged by native queue')
        self.app, self.fleet = app, fleet
        return app, fleet

    def run_recovery(self, rpc=None):
        app, fleet = self.app, self.fleet
        before = {k: v for k, v in self.native.items() if k != 'id'}
        after = {**before, 'status': 'active'}
        def observations(*_):
            session = deepcopy(self.session)
            session['observedAt'] = time.time()
            if self.backend.send.call_count or native.call.call_count >= 3:
                session['goal']['status'] = 'active'
            return {'sessions': {session['sessionId']: session}}
        with patch('studio_goal_recovery.observe', side_effect=observations), \
                patch('studio_goal_recovery.failure', return_value=self.error), \
                patch.object(app.triggers, 'hold_reason', return_value=''), \
                patch.object(app.automation, 'hold', return_value=''), \
                patch.object(app.runtimes, 'for_agent', return_value=self.backend), \
                patch('studio_goal_recovery.GoalRPC') as helper:
            native = helper.return_value.__enter__.return_value
            native.call.side_effect = rpc or [{'goal': before}, {}, {'goal': after}]
            recover(app, fleet, self.rule, fleet['agents'][0], self.now)
            return native

    def test_resume_preserves_goal_and_queues_once(self):
        app, _ = self.setup_recovery()
        rpc = self.run_recovery()
        self.assertEqual(rpc.call.call_args_list[1].args,
                         ('thread/goal/set', {'threadId': self.session['sessionId'], 'status': 'active'}))
        self.backend.send.assert_called_once()
        self.assertEqual(next(iter(app.goals.receipts.values()))['status'], 'queued')
        self.assertEqual(app.automation.snapshot(self.fleet['id'])['lastFire']['recovery-rule']['status'], 'queued')
        self.run_recovery()
        self.backend.send.assert_called_once()

    def test_unknown_activation_is_never_replayed(self):
        app, _ = self.setup_recovery()
        before = {k: v for k, v in self.native.items() if k != 'id'}
        self.run_recovery([{'goal': before}, TimeoutError('uncertain')])
        self.assertEqual(next(iter(app.goals.receipts.values()))['status'], 'unknown')
        self.run_recovery()
        self.backend.send.assert_not_called()

    def test_rate_limit_waits_at_least_fifteen_minutes(self):
        self.setup_recovery()
        self.error.update(kind='rate-limit', at=self.now - 600)
        self.native['updatedAt'] = self.error['at']
        self.run_recovery().call.assert_not_called()

    def test_active_uncertain_and_missing_children_prevent_activation(self):
        self.setup_recovery()
        for report in (None, {'known': False},
                       {'known': True, 'observedAt': self.now, 'active': 1},
                       {'known': True, 'observedAt': self.now, 'uncertain': 1}):
            with self.subTest(report=report):
                self.session['subagents'] = report
                self.run_recovery().call.assert_not_called()
                self.backend.send.assert_not_called()

    def test_unknown_queue_and_restart_do_not_repeat(self):
        app, _ = self.setup_recovery()
        self.backend.send.side_effect = TimeoutError('queue acknowledgement lost')
        self.run_recovery()
        self.assertEqual(next(iter(app.goals.receipts.values()))['status'], 'unknown')
        app.goals.receipts = json.loads(app.goals.path.read_text(encoding='utf-8'))
        self.session['turnId'] = 'later-failed-turn'
        self.run_recovery()
        self.backend.send.assert_called_once()

    def test_changed_native_goal_is_not_activated(self):
        self.setup_recovery()
        changed = {**self.native, 'objective': 'Different work'}
        rpc = self.run_recovery([{'goal': changed}])
        self.assertEqual(rpc.call.call_count, 1)
        self.backend.send.assert_not_called()

    def test_budget_and_workflow_pause_hold_recovery(self):
        app, fleet = self.setup_recovery()
        self.native['tokensUsed'] = self.native['tokenBudget']
        self.run_recovery().call.assert_not_called()
        self.native['tokensUsed'] = 500
        app.deployments.data['paused'][fleet['id']] = True
        self.run_recovery().call.assert_not_called()

    def test_attempt_cap_escalates_without_native_effects(self):
        app, fleet = self.setup_recovery()
        from studio_goals import record_id
        for i in range(3):
            app.goals.receipts[str(i)] = {'recoveryGoal': record_id(self.native),
                'sessionId': self.session['sessionId'], 'fleetId': fleet['id'], 'status': 'queued'}
        self.error['at'] = self.now - 10000
        self.native['updatedAt'] = self.error['at']
        self.run_recovery().call.assert_not_called()
        self.backend.send.assert_not_called()
        self.assertEqual(len(app.automation.data['escalations']), 1)
        self.run_recovery()
        self.assertEqual(len(app.automation.data['escalations']), 1)

    def test_real_holds_and_uncertain_sessions_do_not_write(self):
        self.setup_recovery()
        original = deepcopy(self.session)
        for changes in ({'status': 'working'}, {'pids': []}, {'queueCount': None},
                        {'queueCount': 1}, {'turnCompletedAt': None}, {'goalKnown': False}):
            with self.subTest(changes=changes):
                self.session = deepcopy(original)
                self.session.update(changes)
                self.run_recovery().call.assert_not_called()
                self.backend.send.assert_not_called()

    def test_paused_limited_and_unrelated_goals_do_not_write(self):
        self.setup_recovery()
        for status in ('paused', 'budget_limited', 'usage_limited', 'complete'):
            self.native['status'] = status
            self.run_recovery().call.assert_not_called()
        self.native.update(status='blocked', updatedAt=self.now - 5000)
        self.run_recovery().call.assert_not_called()

    def test_transcript_requires_exact_error_and_no_tools(self):
        self.now = time.time()
        path = self.root / 'transcript.jsonl'
        start = {'type': 'event_msg', 'timestamp': self.now - 1000,
                 'payload': {'type': 'task_started', 'turn_id': 't'}}
        error = {'type': 'event_msg', 'timestamp': self.now - 1000,
                 'payload': {'type': 'error', 'codex_error_info': {'http_connection_failed': {'http_status_code': 502}}}}
        def write(rows):
            path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        write([start, error])
        self.assertEqual(failure(path, 't')['kind'], 'gateway')
        with self.assertRaises(ValueError):
            failure(path, 'another-turn')
        for info in ('rate_limit_exceeded', 'context_window_exceeded', None):
            error['payload']['codex_error_info'] = info
            write([start, error])
            if info == 'rate_limit_exceeded':
                self.assertEqual(failure(path, 't')['kind'], 'rate-limit')
            else:
                with self.assertRaises(ValueError):
                    failure(path, 't')
        error['payload']['codex_error_info'] = 'rate_limit_exceeded'
        call = {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'matlab'}}
        output = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'matlab'}}
        for rows in ([start, call, error], [start, call, output, error], [call, start, error]):
            write(rows)
            with self.assertRaises(ValueError):
                failure(path, 't')

    def test_default_rules_do_not_enable_recovery(self):
        self.assertNotIn('resume-goal', [r['action'] for r in default_automations('fleet')])
        rule = {**default_automations('fleet')[0], 'action': 'resume-goal',
                'when': {'signal': 'transient-goal-error'}, 'thenAction': 'escalate', 'maxFires': 3}
        self.assertEqual(clean_automations([rule], [])[0]['action'], 'resume-goal')
        for changes in ({'maxFires': 0}, {'maxFires': 0.5}, {'thenAction': 'handoff'}, {'action': 'reprompt'}):
            with self.assertRaises(ValueError):
                clean_automations([{**rule, **changes}], [])
