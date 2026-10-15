"""Dependency continuations with synthetic observations and mocked native effects."""
from contextlib import closing
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch

import test_contracts as fixtures
from studio_continuations import hold, reconcile
from studio_control import Controls
from studio_progress import Progress
from studio_triggers import Triggers
from workflow_spec import apply_spec, export_spec


class ContinuationTests(unittest.TestCase):
    setUp, tearDown = fixtures.ContractTests.setUp, fixtures.ContractTests.tearDown
    rule, monitor = fixtures.ContractTests.rule, fixtures.ContractTests.monitor

    def prepare(self, automatic=False):
        app, fleet = fixtures.ContractTests.prepare(self)
        state = app.store.snapshot()
        spec = export_spec(fleet)
        spec['states'] = [
            {'id': 'accepted', 'name': 'Accepted', 'roles': [], 'issueState': 'closed', 'labelsAny': ['Accepted'],
             'labelPrefixes': [], 'fallback': False, 'acceptsDelivery': True},
            {'id': 'backlog', 'name': 'Backlog', 'roles': [], 'issueState': 'any', 'labelsAny': [],
             'labelPrefixes': [], 'fallback': True}]
        state['fleets'][0] = apply_spec(fleet, spec)
        state['fleets'][0]['triggers'][0]['enabled'] = True
        if automatic:
            state['fleets'][0]['triggers'][0]['mode'] = 'automatic'
        app.store.save(state)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        fleet = app.store.fleet(fleet['id'])
        rid = app.deployments.data['active'][fleet['id']]
        for agent in fleet['agents']:
            app.deployments.acknowledge({'fleetId': fleet['id'], 'agentId': agent['id'], 'releaseId': rid})
        self.contract['releaseId'] = self.report['releaseId'] = rid
        self.contract['continuation'] = {'authorized': True, 'when': 'dependencies-accepted',
                                          'message': 'Recheck the dependency evidence and continue this module if current stops allow.'}
        self.original = app.triggers.report(self.event)['actions'][0]
        app.progress.report({**self.report, 'kind': 'handoff-received', 'eventId': 'original-receipt', 'handoffId': self.original['id']})
        self.observe(app, fleet)
        return app, fleet

    def observe(self, app, fleet, accepted=True, **changes):
        rows = [{'iid': 1, 'url': self.contract['taskUrl'], 'title': 'Current work', 'state': 'opened', 'labels': [], 'stage': 'active'},
                {'iid': 2, 'url': self.contract['dependencies'][0], 'title': 'Dependency', 'state': 'closed' if accepted else 'opened',
                 'labels': ['Accepted'] if accepted else [], 'stage': 'closed' if accepted else 'active'}]
        snapshot = {'items': rows, 'observedAt': time.time(), 'partial': False, **changes}
        app.progress.observe_backlog(fleet, snapshot)
        app.backlog.cache[fleet['id']] = deepcopy(snapshot)

    def reserve(self, app):
        with app.triggers.lock:
            reconcile(app.triggers, app.store.snapshot(), app.monitor)
        return next((a for a in app.triggers.data['actions'] if a.get('continuationOf')), None)

    def test_initial_accepted_dependencies_reserve_once_and_receipt_preserves_assignment(self):
        app, fleet = self.prepare()
        app.progress.report(self.report)
        action = self.reserve(app)
        self.assertEqual(action['continuationOf'], self.original['id'])
        self.assertEqual(self.reserve(app)['id'], action['id'])
        app.triggers = Triggers(app)
        app.progress = Progress(app)
        self.assertEqual(self.reserve(app)['id'], action['id'])
        app.progress.report({**self.report, 'eventId': 'continuation-receipt', 'kind': 'handoff-received', 'handoffId': action['id']})
        app.progress.report({**self.report, 'eventId': 'verifying', 'kind': 'progress-updated', 'phase': 'verifying'})
        with closing(app.progress.connect()) as db:
            report = json.loads(db.execute('SELECT data FROM agent_tasks').fetchone()['data'])
        self.assertEqual(report['obligationId'], self.original['id'])
        with patch.object(app.backlog, 'snapshot', return_value=app.backlog.cache[fleet['id']]):
            assignment = app.progress.snapshot(fleet['id'])['agents'][1]['assignments'][0]
        self.assertEqual(assignment['state'], 'verifying')
        self.assertEqual(assignment['contract']['id'], self.contract['id'])

    def test_repeated_automatic_reconciliation_sends_once(self):
        app, fleet = self.prepare(automatic=True)
        app.launch_enabled = True
        with patch('studio_control.threading.Thread'), patch.object(app.runtimes, 'for_agent') as backend:
            backend.return_value.send.return_value = ('queued', 'Queued')
            action = self.reserve(app)
            preview = app.controls.previews[action['controlId']]
            app.controls._dispatch(preview)
            for _ in range(3):
                app.triggers.tick(app.store.snapshot(), app.monitor)
            self.assertEqual(backend.return_value.send.call_count, 1)
        self.assertEqual(action['status'], 'queued')
        # Compact reservation survives removal of bounded display history.
        app.triggers.data['actions'] = []
        app.triggers.persist()
        app.triggers = Triggers(app)
        self.assertIsNone(self.reserve(app))

    def test_final_effect_rechecks_reopened_stale_missing_or_failed_observations(self):
        for scenario in ('reopened', 'stale', 'partial', 'failed', 'missing', 'loading'):
            with self.subTest(scenario=scenario):
                app, fleet = self.prepare()
                action = self.reserve(app)
                preview = app.triggers.review(action['id'])
                app.launch_enabled = True
                with patch('studio_control.threading.Thread'):
                    app.controls.send(preview['id'])
                if scenario == 'reopened':
                    self.observe(app, fleet, accepted=False)
                elif scenario == 'stale':
                    self.observe(app, fleet, observedAt=time.time() - 181)
                elif scenario == 'partial':
                    self.observe(app, fleet, partial=True)
                elif scenario == 'missing':
                    self.observe(app, fleet, items=[])
                else:
                    app.backlog.cache[fleet['id']][{'failed': 'error', 'loading': 'loading'}[scenario]] = True
                with patch.object(app.runtimes, 'for_agent') as backend:
                    app.controls._dispatch(preview)
                    backend.assert_not_called()
                self.assertEqual(app.controls.receipts[preview['id']]['targets'][0]['status'], 'held')
                # Isolate the next scenario's event identity within this fixture.
                self.tearDown(); self.setUp()

    def test_every_dependency_and_explicit_acceptance_rule_required(self):
        app, fleet = self.prepare()
        self.observe(app, fleet, accepted=False)
        self.assertIsNone(self.reserve(app))
        self.observe(app, fleet)
        app.backlog.cache[fleet['id']]['items'][1]['labels'] = []
        app.progress.observe_backlog(fleet, app.backlog.cache[fleet['id']])
        self.assertIsNone(self.reserve(app))
        self.observe(app, fleet)
        self.assertEqual(hold(app, fleet['id'], self.contract, self.original['id']), '')
        # A missing second dependency stays unknown; absence is never satisfaction.
        other = {**self.contract, 'id': 'two-deps', 'supersedes': self.contract['id'],
                 'dependencies': self.contract['dependencies'] + [self.contract['taskUrl'].rsplit('/', 1)[0] + '/3']}
        new = app.triggers.report({**self.event, 'eventId': 'two-deps', 'assignmentContract': other})['actions'][0]
        app.progress.report({**self.report, 'contractId': other['id'], 'kind': 'handoff-received', 'eventId': 'two-receipt', 'handoffId': new['id']})
        self.assertIsNone(self.reserve(app))

    def test_stop_completion_and_supersession_hold_prepared_continuation(self):
        app, fleet = self.prepare()
        action = self.reserve(app)
        preview = app.triggers.review(action['id'])
        app.launch_enabled = True
        for status in ('paused', 'blocked', 'budget_limited', 'usage_limited'):
            app.monitor['sessions'][self.report['sessionId']]['goalStatus'] = status
            with self.assertRaisesRegex(ValueError, 'paused, blocked or limited'):
                app.controls.send(preview['id'])
        app.monitor['sessions'][self.report['sessionId']]['goalStatus'] = None
        with patch('studio_control.threading.Thread'):
            app.controls.send(preview['id'])
        app.progress.report({**self.report, 'kind': 'work-finished', 'eventId': 'finished', 'revision': 'a' * 40,
                             'completionEvidence': {r['id']: self.contract['evidence'][0] for r in self.contract['requirements']}})
        with patch.object(app.runtimes, 'for_agent') as backend:
            app.controls._dispatch(preview)
            backend.assert_not_called()
        self.assertIn('submitted', app.controls.receipts[preview['id']]['targets'][0]['detail'])
        replacement = {**self.contract, 'id': 'replacement', 'supersedes': self.contract['id']}
        app.triggers.report({**self.event, 'eventId': 'replacement', 'assignmentContract': replacement})
        self.assertIn('superseded', hold(app, fleet['id'], self.contract, self.original['id']))

    def test_reservation_failure_and_unknown_delivery_never_duplicate(self):
        app, fleet = self.prepare()
        before = deepcopy(app.triggers.data)
        with patch.object(app.triggers, 'persist', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.reserve(app)
        self.assertEqual(app.triggers.data, before)
        action = self.reserve(app)
        preview = app.triggers.review(action['id'])
        app.launch_enabled = True
        with patch('studio_control.threading.Thread'):
            app.controls.send(preview['id'])
        with patch.object(app.runtimes, 'for_agent') as backend:
            backend.return_value.send.side_effect = TimeoutError()
            app.controls._dispatch(preview)
            app.controls = Controls(app)
            app.triggers = Triggers(app)
            app.progress = Progress(app)
            for _ in range(3):
                app.triggers.tick(app.store.snapshot(), app.monitor)
            self.assertEqual(backend.return_value.send.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'already been dispatched'):
            app.triggers.review(action['id'])

    def test_dependencies_are_included_in_ai_tracker_reads(self):
        app, fleet = self.prepare()
        self.assertEqual(set(app.backlog.references(fleet, {**fleet['backlog'], 'scope': 'ai'})), {1, 2})
        invalid = {**self.contract, 'dependencies': []}
        from studio_contracts import validate
        with self.assertRaisesRegex(ValueError, 'continuation needs dependencies'):
            validate(invalid, app, fleet)

    def test_unrelated_and_unknown_blockers_hold_even_with_matching_dependencies(self):
        app, fleet = self.prepare()
        base = {**self.report, 'kind': 'blocked', 'reason': 'Awaiting an external condition',
                'nextAction': 'Owner reviews it', 'dependsOn': self.contract['dependencies']}
        for category in ('approval', 'restricted', 'host', 'capacity', 'other'):
            app.progress.report({**base, 'eventId': category, 'blockerCategory': category})
            self.assertIsNone(self.reserve(app))
        app.progress.report({**base, 'eventId': 'unknown'})
        self.assertIsNone(self.reserve(app))
        app.progress.report({**base, 'eventId': 'dependency', 'blockerCategory': 'dependency'})
        self.assertIsNotNone(self.reserve(app))

