"""Composed orchestration scenarios and durable measured counts; no live calls."""
from unittest.mock import patch
import unittest

import test_continuations as fixtures
from studio_progress import Progress
from studio_triggers import Triggers
from studio_control import Controls
from contextlib import closing


class ReliabilityTests(unittest.TestCase):
    setUp, tearDown = fixtures.ContinuationTests.setUp, fixtures.ContinuationTests.tearDown
    rule, monitor, prepare, observe, reserve = (fixtures.ContinuationTests.rule, fixtures.ContinuationTests.monitor,
                                              fixtures.ContinuationTests.prepare, fixtures.ContinuationTests.observe, fixtures.ContinuationTests.reserve)

    def metrics(self, app, fleet):
        with patch.object(app.backlog, 'snapshot', return_value=app.backlog.cache[fleet['id']]):
            return app.progress.snapshot(fleet['id'])['metrics']['reliability']

    def test_duplicate_receipts_stopped_dependencies_and_restart_have_measured_outcomes(self):
        app, fleet = self.prepare(automatic=True)
        app.triggers.report(self.event)
        app.progress.report(self.report)
        app.progress.report(self.report)
        action = self.reserve(app)  # Launch is disabled; stable held intent only.
        app.launch_enabled = True
        app.monitor['sessions'][self.report['sessionId']]['goalStatus'] = 'paused'
        with patch.object(app.runtimes, 'for_agent') as backend:
            self.observe(app, fleet, accepted=False)
            app.triggers.tick(app.store.snapshot(), app.monitor)
            self.observe(app, fleet)
            app.triggers.tick(app.store.snapshot(), app.monitor)
            app.triggers = Triggers(app)
            app.progress = Progress(app)
            app.controls = Controls(app)
            app.triggers.tick(app.store.snapshot(), app.monitor)
            backend.assert_not_called()
        metrics = self.metrics(app, fleet)
        self.assertEqual(metrics['counts']['duplicateEvents'], 1)
        self.assertEqual(metrics['counts']['duplicateReports'], 1)
        self.assertEqual(metrics['counts']['manualReconciliations'], 0)
        self.assertEqual(len(app.triggers.data['continuations']), 1)
        self.assertIsNone(metrics['currentBlockerObservedSeconds'])
        self.assertIn('downtime', metrics['notice'])

    def test_manual_reconciliation_count_commits_once_and_survives_observation_pruning(self):
        app, fleet = self.prepare()
        action = self.reserve(app)
        request = {'actionId': action['id'], 'mode': 'record-receipt', 'note': 'Recipient acknowledged this notification.',
                   'evidenceUrl': self.contract['evidence'][0]}
        p = app.reconciliation.preview(request)
        app.reconciliation.apply(p['id'])
        app.triggers.persist()
        app.progress = Progress(app)
        self.assertEqual(self.metrics(app, fleet)['counts']['manualReconciliations'], 1)
        # A retained action can recreate a pruned display row. Count its durable
        # intervention identity once, independently of that row's retention.
        with closing(app.progress.connect()) as db, db:
            db.execute('DELETE FROM handoffs WHERE fleet=? AND id=?', (fleet['id'], action['id']))
        app.triggers.persist()
        self.assertEqual(self.metrics(app, fleet)['counts']['manualReconciliations'], 1)
        app.progress.clock = lambda: 9999999999
        with patch.object(app.backlog, 'snapshot'):
            app.progress.tick(app.store.snapshot(), {})
        self.assertEqual(self.metrics(app, fleet)['counts']['manualReconciliations'], 1)
