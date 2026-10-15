"""Restart checkpoints and terminal work evidence with no external effects."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_contracts as fixtures
from studio_contracts import digest, delivery_hold
from studio_control import Controls
from studio_progress import Progress
from studio_recovery import checkpoint, replay_hold
from studio_triggers import Triggers


class RecoveryTests(unittest.TestCase):
    setUp, tearDown = fixtures.ContractTests.setUp, fixtures.ContractTests.tearDown
    rule, monitor = fixtures.ContractTests.rule, fixtures.ContractTests.monitor
    prepare = fixtures.ContractTests.prepare

    def test_receipt_or_submission_after_preview_suppresses_final_send_and_survives_restart(self):
        for kind in ('handoff-received', 'work-finished'):
            with self.subTest(kind=kind):
                app, fleet = self.prepare()
                app.triggers.report(self.event)
                action = app.triggers.data['actions'][-1]
                preview = app.triggers.review(action['id'])
                app.launch_enabled = True
                with patch('studio_control.threading.Thread'):
                    app.controls.send(preview['id'])
                app.progress.report({**self.report, 'kind': kind, 'handoffId': action['id'], 'revision': 'a' * 40,
                                     **({'completionEvidence': {r['id']: self.contract['evidence'][0] for r in self.contract['requirements']}} if kind == 'work-finished' else {})})
                with patch.object(app.runtimes, 'for_agent') as backend:
                    app.controls._dispatch(preview)
                    backend.assert_not_called()
                self.assertEqual(app.controls.receipts[preview['id']]['targets'][0]['status'], 'held')
                app.progress = Progress(app)
                app.triggers = Triggers(app)
                app.controls = Controls(app)
                self.assertTrue(replay_hold(app, fleet['id'], action['id']))
                if kind == 'work-finished':
                    app.progress.report({**self.report, 'kind': 'task-started', 'eventId': 'late-start'})
                    self.assertIn('submitted', replay_hold(app, fleet['id'], action['id']))
                self.tearDown(); self.setUp()

    def test_restart_at_every_dispatch_boundary_never_replays_uncertain_effects(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        action = app.triggers.data['actions'][-1]
        app.triggers = Triggers(app)
        preview = app.triggers.review(action['id'])  # Proven unsent can be reviewed.
        app.launch_enabled = True
        with patch('studio_control.threading.Thread'):
            app.controls.send(preview['id'])
        app.controls = Controls(app)
        app.triggers = Triggers(app)
        with patch.object(app.controls, 'send') as send:
            app.triggers.tick(app.store.snapshot(), app.monitor)
            send.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'already been dispatched'):
            app.triggers.review(action['id'])
        # Saved target acknowledgement is useful even if aggregate finalization crashed.
        receipt = app.controls.receipts[preview['id']]
        receipt['targets'][0]['status'] = 'queued'
        receipt['status'] = 'sending'
        app.controls.persist()
        app.controls = Controls(app)
        with patch.object(app.backlog, 'snapshot', return_value={}):
            h = app.progress.snapshot(fleet['id'])['handoffs'][0]
        self.assertEqual(h['checkpoint']['delivery'], 'queued')
        self.assertFalse(h['checkpoint']['retryable'])

    def test_new_contract_is_not_suppressed_by_prior_submission(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        app.progress.report({**self.report, 'kind': 'work-finished', 'revision': 'a' * 40,
                             'completionEvidence': {r['id']: self.contract['evidence'][0] for r in self.contract['requirements']}})
        replacement = {**self.contract, 'id': 'replacement', 'supersedes': self.contract['id']}
        new = app.triggers.report({**self.event, 'eventId': 'replacement', 'assignmentContract': replacement})['actions'][0]
        self.assertEqual(replay_hold(app, fleet['id'], new['id']), '')
        self.assertTrue(app.triggers.review(new['id']))

    def test_policy_only_revalidation_preserves_typed_envelope_and_pruned_event_identity(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        action = app.triggers.data['actions'][-1]
        before = deepcopy(action)
        app.triggers.data['events'] = []  # Display event retention is independent of durable identity.
        app.triggers.persist()
        p = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'reporting'})
        app.deployments.apply(p['id'])
        request = {'actionId': action['id'], 'mode': 'refresh-release', 'note': 'Same assignment under updated reporting policy.',
                   'evidenceUrl': self.contract['evidence'][0]}
        p = app.reconciliation.preview(request)
        app.reconciliation.apply(p['id'])
        self.assertEqual(action['assignmentContract'], before['assignmentContract'])
        self.assertEqual(digest(action['assignmentContract']), digest(self.contract))
        self.assertEqual(action['eventKey'], before['eventKey'])
        self.assertEqual(delivery_hold(app, fleet['id'], self.contract), '')
        self.assertTrue(app.deployments.adoption_hold(fleet['id'], [self.contract['agentId']]))
        app.triggers = Triggers(app)
        app.progress = Progress(app)
        self.assertEqual(delivery_hold(app, fleet['id'], self.contract), '')
        app.progress.report({**self.report, 'releaseId': app.deployments.data['active'][fleet['id']]})

    def test_received_contract_revalidation_updates_reporting_without_resend(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        action = app.triggers.data['actions'][-1]
        app.progress.report({**self.report, 'kind': 'handoff-received', 'handoffId': action['id']})
        p = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'reporting'})
        app.deployments.apply(p['id'])
        p = app.reconciliation.preview({'actionId': action['id'], 'mode': 'refresh-release',
                                        'note': 'Received work retains its scope.', 'evidenceUrl': self.contract['evidence'][0]})
        self.assertIn('without rewriting', p['effect'])
        with patch.object(app.controls, 'send') as send:
            app.reconciliation.apply(p['id'])
            app.triggers.tick(app.store.snapshot(), app.monitor)
            send.assert_not_called()
        self.assertEqual(delivery_hold(app, fleet['id'], self.contract), '')
        self.assertIn('recipient receipt', replay_hold(app, fleet['id'], action['id']))

    def test_current_received_contract_survives_action_history_pruning(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        action = app.triggers.data['actions'][-1]
        action['status'] = 'queued'
        app.triggers.data['actions'] = [action]
        app.triggers.persist()
        app.progress.report({**self.report, 'kind': 'handoff-received', 'handoffId': action['id']})
        for i in range(105):
            old = {k: v for k, v in action.items() if k not in ('assignmentContract', 'eventKey', 'eventId')}
            app.triggers.data['actions'].append({**old, 'id': 'history-' + str(i)})
        app.triggers.tick(app.store.snapshot(), app.monitor)
        self.assertIn(action['id'], [a['id'] for a in app.triggers.data['actions']])
        app.triggers = Triggers(app)
        p = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'reporting'})
        app.deployments.apply(p['id'])
        p = app.reconciliation.preview({'actionId': action['id'], 'mode': 'refresh-release',
                                        'note': 'Received assignment remains current.', 'evidenceUrl': self.contract['evidence'][0]})
        app.reconciliation.apply(p['id'])
        self.assertEqual(delivery_hold(app, fleet['id'], self.contract), '')

    def test_reconciled_receipt_can_revalidate_policy_and_report_without_resend(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        action = app.triggers.data['actions'][-1]
        request = {'actionId': action['id'], 'mode': 'record-receipt', 'note': 'Recipient confirmed the assignment in the ticket.',
                   'evidenceUrl': self.contract['evidence'][0]}
        p = app.reconciliation.preview(request)
        app.reconciliation.apply(p['id'])
        p = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'reporting'})
        app.deployments.apply(p['id'])
        p = app.reconciliation.preview({**request, 'mode': 'refresh-release'})
        with patch.object(app.controls, 'send') as send:
            app.reconciliation.apply(p['id'])
            app.progress.report({**self.report, 'releaseId': app.deployments.data['active'][fleet['id']]})
            app.triggers.tick(app.store.snapshot(), app.monitor)
            send.assert_not_called()
        self.assertEqual(action['status'], 'reconciled')

    def test_legacy_comment_ticket_submission_prevents_replay(self):
        app, fleet = self.prepare()
        legacy = {k: v for k, v in self.event.items() if k != 'assignmentContract'}
        legacy['ticket'] += '#note_1'
        action = app.triggers.report(legacy)['actions'][0]
        payload = {k: v for k, v in self.report.items() if k != 'contractId'}
        app.progress.report({**payload, 'kind': 'work-finished'})
        self.assertIn('submitted', replay_hold(app, fleet['id'], action['id']))
        with patch.object(app.backlog, 'snapshot', return_value={}):
            h = app.progress.snapshot(fleet['id'])['handoffs'][0]
        self.assertEqual(h['checkpoint']['work'], 'submitted')
