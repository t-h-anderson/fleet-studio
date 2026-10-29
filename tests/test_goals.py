"""Goal evidence and control safety. All native effects are mocked."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_studio as fixtures
import test_progress as progress_fixtures
from studio_goals import Goals
from studio_runtime import observe


class GoalTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown

    def setup_goal(self):
        app, f = progress_fixtures.ProgressTests.app(self)
        self.now = time.time()
        a = f['agents'][0]
        self.native = {'id': 'native-1', 'objective': 'Deliver one reviewed packet', 'status': 'blocked',
                       'createdAt': self.now - 5000, 'updatedAt': self.now - 1000,
                       'tokensUsed': 500, 'tokenBudget': 8000, 'timeUsedSeconds': 60}
        self.session = {'sessionId': a['sessionId'], 'status': 'closed', 'pids': [], 'goalKnown': True,
                        'goalStatus': 'blocked', 'goal': self.native, 'queueCount': 0, 'observedAt': self.now,
                        'subagents': {'known': True, 'observedAt': self.now, 'active': 0, 'uncertain': 0, 'children': []}}
        app.monitor.update(observedAt=self.now, sessions={a['sessionId']: self.session})
        app.goals.observe([f], app.monitor)
        self.link = {**self.payload, 'goalId': self.native['id'], 'completionCriteria': 'Reviewed packet has a recipient receipt'}
        app.progress.report(self.link)
        self.decision = {'fleetId': f['id'], 'agentId': a['id'], 'action': 'resume', 'confirmed': True,
                         'note': 'Owner authorized resumption after the input arrived', 'evidenceUrl': self.payload['evidenceUrl']}
        return app, f

    def rows(self, app, f):
        return app.goals.snapshot(f, app.monitor, 60)['records']

    def native_patch(self, app):
        return patch('studio_goals.observe', side_effect=lambda *_: deepcopy(app.monitor))

    def test_manual_goal_change_waits_for_children_even_when_parent_is_closed(self):
        app, _ = self.setup_goal()
        self.session['subagents']['active'] = 1
        with self.native_patch(app), patch('studio_goals.GoalRPC') as rpc:
            with self.assertRaisesRegex(ValueError, 'Subagents are working'):
                app.goals.preview(self.decision)
            rpc.assert_not_called()

    def rpc_goal(self):
        return {'threadId': self.session['sessionId'], **{k:v for k,v in self.native.items() if k != 'id'}}

    def test_goal_reports_capture_blocker_without_native_actions(self):
        app, f = self.setup_goal()
        blocked = {**self.link, 'eventId': 'block-2', 'kind': 'blocked', 'reason': 'Waiting for input',
                   'nextAction': 'Owner supplies input', 'blockerCategory': 'dependency', 'blockerOwner': 'Coordinator',
                   'resumeCondition': 'Input approved and owner authorizes continuation', 'dependsOn': [self.payload['taskUrl']]}
        with patch('studio_goals.GoalRPC') as rpc:
            app.progress.report(blocked)
            self.assertTrue(app.progress.report(blocked)['duplicate'])
            b = self.rows(app, f)[0]['blocker']
            self.assertEqual(b['blockerOwner'], 'Coordinator')
            app.progress.report({**self.link, 'eventId': 'resolve-3', 'kind': 'unblocked'})
            self.assertTrue(self.rows(app, f)[0]['blocker']['resolvedAt'])
            self.assertEqual(self.native['status'], 'blocked')
            rpc.assert_not_called()
        for field in ('blockerOwner', 'resumeCondition', 'blockerCategory'):
            with self.assertRaises(ValueError):
                app.progress.report({**blocked, 'eventId': 'bad-' + field, field: ''})

    def test_goal_identity_replacement_and_rebinding_keep_separate_history(self):
        app, f = self.setup_goal()
        self.native['objective'] = 'Another packet with a reused native ID'
        app.goals.observe([f], app.monitor)
        rows = self.rows(app, f)
        self.assertEqual(len(rows), 2)
        current = next(r for r in rows if r['current'])
        self.assertNotIn('taskUrl', current)
        self.assertEqual(next(r for r in rows if not r['current'])['taskUrl'], self.payload['taskUrl'])
        f['agents'][0]['sessionId'] = 'new-session'
        self.assertTrue(all(not r['bindingCurrent'] for r in self.rows(app, f)))

    def test_missing_goal_observation_does_not_prove_completion(self):
        app, f = self.setup_goal()
        self.session['goalKnown'] = False
        rows = self.rows(app, f)
        self.assertFalse(rows[0]['current'])
        self.assertFalse(rows[0]['observationFresh'])
        self.assertEqual(rows[0]['native']['status'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            app.progress.report({**self.link, 'eventId': 'new'})

    def test_stale_goals_flag_review_without_effect(self):
        app, f = self.setup_goal()
        self.now += 3700
        self.session['observedAt'] = self.now
        with patch('studio_goals.GoalRPC') as rpc:
            self.assertTrue(self.rows(app, f)[0]['staleGoal'])
            rpc.assert_not_called()

    def test_operator_link_is_separate_from_agent_report_and_rejects_wrong_goal(self):
        app, f = self.setup_goal()
        with self.assertRaisesRegex(ValueError, 'Goal changed'):
            app.goals.link_metadata({**self.link, 'goalId': 'replaced'})
        with self.assertRaisesRegex(ValueError, 'another ticket'):
            app.goals.link_metadata({**self.link, 'taskUrl': self.payload['taskUrl'] + '2'})
        app.goals.link_metadata({**self.link, 'completionCriteria': 'Exact acceptance packet recorded'})
        self.assertEqual(self.rows(app, f)[0]['completionCriteria'], 'Exact acceptance packet recorded')
        with closing(app.progress.connect()) as db:
            last = json.loads(db.execute("SELECT data FROM observations WHERE category='goal-link'").fetchone()[0])
        self.assertEqual(last['source'], 'operator-link')

    def test_receipts_and_tests_do_not_modify_goal_or_blocker(self):
        app, f = self.setup_goal()
        before = self.rows(app, f)
        for kind in ('test-result', 'handoff-received'):
            with self.assertRaises(ValueError):
                app.progress.report({**self.link, 'eventId': kind, 'kind': kind})
        app.progress.report({**self.payload, 'kind': 'test-result', 'eventId': 'tests', 'testStatus': 'passed',
                             'suite': 'Packet tests', 'revision': 'a' * 40})
        self.assertEqual(before, self.rows(app, f))

    def test_actions_reject_live_unknown_queued_and_in_progress_sessions(self):
        app, f = self.setup_goal()
        for changes in ({'pids': [42], 'status': 'idle'}, {'queueCount': 1}, {'queueCount': None},
                        {'turnStatus': 'inProgress'}, {'observedAt': self.now - 60}, {'goalKnown': False}):
            with patch('studio_goals.observe', return_value={'sessions': {self.session['sessionId']: {**self.session, **changes}}}):
                with self.assertRaises(ValueError):
                    app.goals.preview(self.decision)

    def test_actions_require_linked_evidence_and_explicit_confirmation(self):
        app, f = self.setup_goal()
        with self.native_patch(app):
            for change in ({'confirmed': False}, {'note': ''}, {'evidenceUrl': 'https://example.com/other'}, {'action': 'delete'}):
                with self.assertRaises(ValueError):
                    app.goals.preview({**self.decision, **change})
            for status in ('budget_limited', 'usage_limited', 'active', 'complete'):
                self.native['status'] = status
                with self.assertRaises(ValueError):
                    app.goals.preview(self.decision)

    def stale_change(self, change):
        app, f = self.setup_goal()
        app.launch_enabled = True
        with self.native_patch(app), patch('studio_goals.GoalRPC') as rpc:
            p = app.goals.preview(self.decision)
            if change == 'goal':
                self.native['tokensUsed'] += 1
            elif change == 'release':
                app.deployments.data['active'][f['id']] = None
            elif change == 'binding':
                state = app.store.snapshot();state['fleets'][0]['agents'][0]['notes'] = 'New scope';app.store.save(state)
            else:
                app.goals.link_metadata({**self.link, 'completionCriteria': 'Changed done criterion'})
            with self.assertRaises(ValueError):
                app.goals.apply(p['id'])
            rpc.assert_not_called()

    def test_changed_goal_invalidates_preview(self):
        self.stale_change('goal')

    def test_changed_release_invalidates_preview(self):
        self.stale_change('release')

    def test_changed_binding_invalidates_preview(self):
        self.stale_change('binding')

    def test_changed_evidence_invalidates_preview(self):
        self.stale_change('evidence')

    def test_reviewed_actions_preserve_usage_and_never_resend(self):
        app, f = self.setup_goal();app.launch_enabled = True
        for action in ('complete', 'resume', 'cancel'):
            with self.subTest(action=action), self.native_patch(app), patch('studio_goals.GoalRPC') as factory:
                p = app.goals.preview({**self.decision, 'action': action})
                before = self.rpc_goal()
                after = None if action == 'cancel' else {**before, 'status': 'active' if action == 'resume' else 'complete'}
                rpc = factory.return_value.__enter__.return_value
                def call(method, params):
                    self.assertIn(p['id'], json.loads(app.goals.path.read_text()))
                    return {'goal': after if rpc.call.call_count == 3 else before}
                rpc.call.side_effect = call
                result = app.goals.apply(p['id'])
                self.assertEqual(result['status'], 'applied', result)
                method, params = rpc.call.call_args_list[1].args
                self.assertEqual(method, 'thread/goal/clear' if action == 'cancel' else 'thread/goal/set')
                self.assertEqual(set(params), {'threadId'} if action == 'cancel' else {'threadId', 'status'})
                self.assertEqual(app.goals.apply(p['id']), result)
                self.assertEqual(rpc.call.call_count, 3)

    def test_timeout_and_restart_are_unknown_without_automatic_retry(self):
        app, f = self.setup_goal();app.launch_enabled = True
        with self.native_patch(app), patch('studio_goals.GoalRPC') as factory:
            p = app.goals.preview(self.decision)
            rpc = factory.return_value.__enter__.return_value
            rpc.call.side_effect = [{'goal': self.rpc_goal()}, TimeoutError('lost acknowledgement')]
            result = app.goals.apply(p['id'])
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(app.goals.apply(p['id']), result)
            self.assertEqual(rpc.call.call_count, 2)
            app.goals.receipts[p['id']]['status'] = 'applying';app.goals.persist()
            self.assertEqual(Goals(app).receipts[p['id']]['status'], 'unknown')

    def test_last_read_rejects_native_change_before_mutation(self):
        app, f = self.setup_goal();app.launch_enabled = True
        with self.native_patch(app), patch('studio_goals.GoalRPC') as factory:
            p = app.goals.preview(self.decision)
            rpc = factory.return_value.__enter__.return_value
            rpc.call.return_value = {'goal': {**self.rpc_goal(), 'objective': 'New work'}}
            self.assertEqual(app.goals.apply(p['id'])['status'], 'failed')
            self.assertEqual(rpc.call.call_count, 1)

    def test_test_instance_never_applies_native_controls(self):
        app, f = self.setup_goal()
        with self.native_patch(app):
            p = app.goals.preview(self.decision)
            with self.assertRaisesRegex(ValueError, 'disabled'):
                app.goals.apply(p['id'])

    def test_policy_only_deployment_preserves_roles_routes_and_stops(self):
        app, f = self.setup_goal()
        for kind in ('reporting', 'bounded-goals'):
            p = app.deployments.preview({'fleetId': f['id'], 'kind': kind, 'revision': app.store.snapshot()['revision']})
            app.deployments.apply(p['id'])
        release = app.deployments.release(app.deployments.data['active'][f['id']])
        self.assertEqual(release['fleet']['roles'], f['roles'])
        self.assertEqual(release['fleet']['triggers'], f['triggers'])
        text = (app.deployments.root / release['id'] / 'agents' / (f['agents'][0]['id'] + '.md')).read_text()
        # Untagged process policies stay in WORKFLOW.md; the brief keeps only the agent's own content.
        self.assertNotIn('--resume-condition', text)
        self.assertIn('report blocked', text)
        self.assertLess(len(text.split()), 500)
        self.assertEqual(self.native['status'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'already deployed'):
            app.deployments.preview({'fleetId': f['id'], 'kind': 'bounded-goals', 'revision': app.store.snapshot()['revision']})
        p = app.deployments.preview({'fleetId': f['id'], 'kind': 'coordination', 'revision': app.store.snapshot()['revision']})
        self.assertEqual(p['changes']['policies']['added'], ['integration-coordination'])
        self.assertEqual(p['bindingChanges']['before'], p['bindingChanges']['after'])
        self.assertIn('externally managed', p['document'])
        self.assertIn('exact revisions tested', p['document'])
        app.deployments.apply(p['id'])
        state = app.store.snapshot();state['fleets'][0]['agents'][0]['notes'] = 'Unrelated draft edit';app.store.save(state)
        with self.assertRaisesRegex(ValueError, 'unrelated draft'):
            app.deployments.preview({'fleetId': f['id'], 'kind': 'coordination', 'revision': app.store.snapshot()['revision']})

    def test_native_observation_reads_identity_and_normalizes_status(self):
        with closing(sqlite3.connect(self.root / 'goals_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_goals(thread_id TEXT, goal_id TEXT, objective TEXT, status TEXT, token_budget INT, tokens_used INT, time_used_seconds INT, created_at_ms INT, updated_at_ms INT)')
            db.execute('INSERT INTO thread_goals VALUES(?,?,?,?,?,?,?,?,?)', ('s', 'g', 'Packet', 'usageLimited', 100, 50, 3, 100000, 200000))
        with patch('studio_runtime.writer_owners', return_value=[]):
            s = observe(self.root, ['s'])['sessions']['s']
        self.assertEqual(s['goal']['id'], 'g')
        self.assertEqual(s['goal']['createdAt'], 100)
        self.assertEqual(s['goalStatus'], 'usage_limited')
