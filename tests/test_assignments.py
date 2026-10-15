"""Assignment observations never dispatch work or infer acceptance from activity."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_progress as fixtures
from studio_progress import Progress
from workflow_spec import apply_spec, digest, export_spec, validate


class AssignmentTests(unittest.TestCase):
    setUp, tearDown = fixtures.ProgressTests.setUp, fixtures.ProgressTests.tearDown
    app, observe, snapshot, handoff = (fixtures.ProgressTests.app, fixtures.ProgressTests.observe,
                                     fixtures.ProgressTests.snapshot, fixtures.ProgressTests.handoff)

    def assignment(self, app, fleet):
        return self.snapshot(app, fleet)['agents'][0]['assignments'][0]

    @staticmethod
    def states():
        return [{'id': 'closed', 'name': 'Closed', 'roles': [], 'issueState': 'closed',
                 'labelsAny': [], 'labelPrefixes': [], 'fallback': False},
                {'id': 'backlog', 'name': 'Backlog', 'roles': [], 'issueState': 'any',
                 'labelsAny': [], 'labelPrefixes': [], 'fallback': True}]

    def acceptance_policy(self, app, fleet):
        state = app.store.snapshot()
        spec = export_spec(fleet)
        spec['states'] = self.states()
        next(s for s in spec['states'] if s['id'] == 'closed')['acceptsDelivery'] = True
        state['fleets'][0] = apply_spec(fleet, spec)
        app.store.save(state)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        self.payload['releaseId'] = app.deployments.data['active'][fleet['id']]
        return app.store.fleet(fleet['id'])

    def test_queue_receipt_start_verification_and_submission_are_distinct(self):
        app, fleet = self.app()
        self.observe(app, fleet)
        handoff = {**self.handoff(fleet), 'deploymentId': self.payload['releaseId']}
        app.progress.observe_handoffs([handoff])
        with patch.object(app.controls, 'send') as send, patch.object(app.triggers, 'report') as dispatch:
            self.assertEqual(self.assignment(app, fleet)['state'], 'assigned')
            app.progress.report({**self.payload, 'kind': 'handoff-received', 'handoffId': handoff['id'], 'eventId': 'receipt'})
            self.assertEqual(self.assignment(app, fleet)['state'], 'received')
            self.now += 1
            app.progress.report(self.payload)
            self.assertEqual(self.assignment(app, fleet)['state'], 'implementing')
            app.progress.report({**self.payload, 'kind': 'test-result', 'eventId': 'tests', 'testStatus': 'passed',
                                 'suite': 'Example', 'revision': 'a' * 40, 'passed': 4})
            self.assertEqual(self.assignment(app, fleet)['state'], 'implementing')
            app.progress.report({**self.payload, 'kind': 'progress-updated', 'phase': 'verifying', 'eventId': 'verify'})
            self.assertEqual(self.assignment(app, fleet)['state'], 'verifying')
            app.progress.report({**self.payload, 'kind': 'work-finished', 'eventId': 'finished'})
            self.assertEqual(self.assignment(app, fleet)['state'], 'submitted')
            send.assert_not_called()
            dispatch.assert_not_called()

    def test_generic_closure_cannot_establish_acceptance(self):
        app, fleet = self.app()
        app.progress.report({**self.payload, 'kind': 'work-finished'})
        self.observe(app, fleet, 'closed', 'closed')
        self.assertEqual(self.assignment(app, fleet)['state'], 'closed')

    def test_acceptance_requires_fresh_configured_tracker_evidence(self):
        app, fleet = self.app()
        fleet = self.acceptance_policy(app, fleet)
        app.progress.report({**self.payload, 'kind': 'progress-updated', 'phase': 'submitted'})
        self.observe(app, fleet, 'closed', 'closed')
        item = self.assignment(app, fleet)
        self.assertEqual((item['state'], item['source']), ('accepted', 'Tracker acceptance rule'))
        self.assertFalse(any(r['kind'] == 'conflict' for r in self.snapshot(app, fleet)['attention']))
        for tracker in ({'observedAt': self.now, 'stale': True}, {'observedAt': self.now, 'partial': True}, {'observedAt': None}):
            with patch.object(app.backlog, 'snapshot', return_value=tracker):
                item = app.progress.snapshot(fleet['id'])['agents'][0]['assignments'][0]
            self.assertEqual(item['state'], 'unknown')
        self.now += 181
        self.assertEqual(self.assignment(app, fleet)['state'], 'unknown')

    def test_active_work_conflicting_with_closure_is_not_accepted(self):
        app, fleet = self.app()
        fleet = self.acceptance_policy(app, fleet)
        app.progress.report(self.payload)
        self.observe(app, fleet, 'closed', 'closed')
        self.assertEqual(self.assignment(app, fleet)['state'], 'conflicting')

    def test_closure_observed_before_submission_requires_a_fresh_read(self):
        app, fleet = self.app()
        fleet = self.acceptance_policy(app, fleet)
        self.observe(app, fleet, 'closed', 'closed')
        self.now += 10
        app.progress.report({**self.payload, 'kind': 'work-finished'})
        self.assertEqual(self.assignment(app, fleet)['state'], 'unknown')
        self.observe(app, fleet, 'closed', 'closed')
        self.assertEqual(self.assignment(app, fleet)['state'], 'accepted')

    def test_blocker_owner_and_observed_wait_survive_progress_and_restart(self):
        app, fleet = self.app()
        blocked = {**self.payload, 'kind': 'blocked', 'reason': 'Dependency is not ready',
                   'blockerOwner': 'Coordinator', 'nextAction': 'Supply the reviewed input',
                   'dependsOn': ['https://gitlab.example/team/project/-/issues/2']}
        app.progress.report(blocked)
        self.now += 120
        app.progress.report({**self.payload, 'kind': 'progress-updated', 'eventId': 'still-waiting'})
        item = self.assignment(app, fleet)
        self.assertEqual((item['state'], item['owner'], item['waitingObservedSeconds']), ('waiting', 'Coordinator', 120))
        self.assertEqual(item['dependsOn'], blocked['dependsOn'])
        self.now += 600
        app.progress.report({**self.payload, 'kind': 'progress-updated', 'eventId': 'after-gap'})
        app.progress = Progress(app, lambda: self.now)
        item = self.assignment(app, fleet)
        self.assertEqual(item['waitingObservedSeconds'], 120)
        self.assertTrue(item['waitingObservationGap'])
        self.assertTrue(app.progress.report(blocked)['duplicate'])
        app.progress.report({**self.payload, 'kind': 'unblocked', 'eventId': 'ready'})
        self.assertNotEqual(self.assignment(app, fleet)['state'], 'waiting')

    def test_later_handoff_does_not_inherit_previous_completion(self):
        app, fleet = self.app()
        app.progress.report({**self.payload, 'kind': 'work-finished'})
        self.now += 10
        handoff = {**self.handoff(fleet), 'deploymentId': self.payload['releaseId']}
        app.progress.observe_handoffs([handoff])
        self.assertEqual(self.assignment(app, fleet)['state'], 'assigned')

    def test_unknown_delivery_is_not_replayed_or_called_received(self):
        app, fleet = self.app()
        for status in ('sending', 'needs-attention'):
            handoff = {**self.handoff(fleet), 'deploymentId': self.payload['releaseId'], 'status': status}
            app.progress.observe_handoffs([handoff])
            app.progress = Progress(app, lambda: self.now)
            with patch.object(app.controls, 'send') as send:
                item = self.assignment(app, fleet)
                self.assertEqual(item['state'], 'unknown')
                self.assertIn('before retrying', item['nextAction'])
                send.assert_not_called()

    def test_old_binding_or_release_does_not_establish_current_work(self):
        app, fleet = self.app()
        handoff = {**self.handoff(fleet), 'deploymentId': 'old-release'}
        app.progress.observe_handoffs([handoff])
        self.assertEqual(self.assignment(app, fleet)['state'], 'unknown')
        handoff.update(deploymentId=self.payload['releaseId'], targetSessionId='different-session')
        app.progress.observe_handoffs([handoff])
        self.assertEqual(self.assignment(app, fleet)['state'], 'unknown')
        app.progress.report({**self.payload, 'kind': 'blocked', 'reason': 'Old dependency', 'nextAction': 'Supply input'})
        fleet = self.acceptance_policy(app, fleet)  # A new immutable workflow release.
        app.progress.report({**self.payload, 'kind': 'task-started', 'eventId': 'new-assignment'})
        self.assertEqual(self.assignment(app, fleet)['state'], 'implementing')

    def test_no_assignment_report_is_separate_from_idle_or_stopped_session(self):
        app, fleet = self.app()
        agent = fleet['agents'][0]
        app.monitor.update(observedAt=self.now, sessions={agent['sessionId']: {'sessionId': agent['sessionId'],
                           'status': 'idle', 'goalStatus': 'paused', 'observedAt': self.now, 'queueCount': 0, 'pids': [1]}})
        item = self.snapshot(app, fleet)['agents'][0]
        self.assertEqual(item['assignments'], [])
        self.assertEqual(item['availability']['state'], 'held')
        self.assertIn('paused', item['availability']['reason'])
        self.now += 21
        app.monitor['sessions'][agent['sessionId']]['status'] = 'working'
        item = self.snapshot(app, fleet)['agents'][0]
        self.assertEqual(item['availability']['state'], 'held')
        self.assertIn('stale', item['availability']['reason'])

    def test_phase_validation_and_legacy_workflow_digest(self):
        app, fleet = self.app()
        for update in ({'phase': 'accepted', 'kind': 'progress-updated'}, {'phase': 'verifying'}, {'blockerOwner': True}):
            with self.assertRaises(ValueError):
                app.progress.report({**self.payload, **update})
        spec = export_spec(fleet)
        self.assertTrue(all('acceptsDelivery' not in s for s in spec['states']))
        self.assertEqual(digest(export_spec(apply_spec(fleet, spec))), digest(spec))
        changed = deepcopy(spec)
        changed['states'] = self.states()
        next(s for s in changed['states'] if s['fallback'])['acceptsDelivery'] = True
        with self.assertRaisesRegex(ValueError, 'acceptance rule'):
            validate(changed)
        spec['states'] = self.states()
        next(s for s in spec['states'] if s['id'] == 'closed')['acceptsDelivery'] = True
        self.assertEqual(digest(export_spec(apply_spec(fleet, spec))), digest(spec))
