"""Bounded context and presentation retirement preserve canonical safety evidence."""
from contextlib import closing
from copy import deepcopy
import json
import time
import subprocess
import sys
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

import test_contracts as fixtures
from studio_context import CurrentContext
from studio_contracts import delivery_hold
from studio_progress import Progress
from studio_triggers import Triggers


class ContextTests(unittest.TestCase):
    setUp, tearDown = fixtures.ContractTests.setUp, fixtures.ContractTests.tearDown
    rule, monitor = fixtures.ContractTests.rule, fixtures.ContractTests.monitor
    prepare = fixtures.ContractTests.prepare

    def setup_context(self):
        app, fleet = self.prepare()
        old = app.triggers.report(self.event)['actions'][0]
        app.progress.report({**self.report, 'kind': 'handoff-received', 'eventId': 'receipt', 'handoffId': old['id']})
        app.progress.report({**self.report, 'kind': 'work-finished', 'eventId': 'submitted', 'revision': 'a' * 40,
                             'completionEvidence': {r['id']: self.contract['evidence'][0] for r in self.contract['requirements']}})
        new_contract = {**self.contract, 'id': 'module-two', 'supersedes': self.contract['id'], 'deliverable': 'Current work'}
        new = app.triggers.report({**self.event, 'eventId': 'new', 'assignmentContract': new_contract})['actions'][0]
        payload = {'fleetId': fleet['id'], 'agentId': self.contract['agentId']}
        return app, fleet, old, new, payload

    def test_retirement_preview_keeps_evidence_and_never_reactivates_old_contract(self):
        app, fleet, old, new, payload = self.setup_context()
        before = deepcopy(app.triggers.data)
        with patch.object(app.controls, 'send') as send:
            p = app.context.preview_retirement(payload)
            self.assertEqual([r['handoff']['id'] for r in p['records']], [old['id']])
            self.assertEqual(app.triggers.data, before)
            app.context.retire(p['id'])
            app.triggers.persist()  # Subsequent observations must preserve the overlay.
            app.progress = Progress(app)
            app.triggers = Triggers(app)
            app.context = CurrentContext(app)
            d = app.context.page(payload)
            send.assert_not_called()
        self.assertEqual(d['retiredCount'], 1)
        self.assertNotIn(old['id'], [i.get('handoffId') for i in d['items']])
        self.assertIn(new['id'], [i.get('handoffId') for i in d['items']])
        record = app.context.handoff({**payload, 'handoffId': old['id']})
        self.assertTrue(record['handoff']['retiredAt'])
        self.assertTrue(record['handoff']['receiptEvidence'])
        self.assertIn('superseded', delivery_hold(app, fleet['id'], self.contract))
        self.assertTrue(app.triggers.report(self.event)['duplicate'])

    def test_current_or_unresolved_contracts_cannot_retire(self):
        app, fleet = self.prepare()
        old = app.triggers.report(self.event)['actions'][0]
        payload = {'fleetId': fleet['id'], 'agentId': self.contract['agentId'], 'handoffIds': [old['id']]}
        with self.assertRaisesRegex(ValueError, 'still require'):
            app.context.preview_retirement(payload)
        app.triggers.dismiss(old['id'])
        with self.assertRaisesRegex(ValueError, 'still require'):
            app.context.preview_retirement(payload)
        page = app.context.page(payload)
        self.assertEqual(page['items'][0]['checkpoint']['delivery'], 'withdrawn')

    def test_semantic_changes_invalidate_retirement_but_poll_timestamps_do_not(self):
        app, fleet, old, new, payload = self.setup_context()
        p = app.context.preview_retirement(payload)
        app.triggers.persist()
        app.context.retire(p['id'])  # Poll-only observedAt changed; semantic facts did not.
        with closing(app.progress.connect()) as db, db:
            row = json.loads(db.execute('SELECT data FROM handoffs WHERE id=?', (old['id'],)).fetchone()['data'])
            row.pop('retiredAt'); row.pop('retirement')
            db.execute('UPDATE handoffs SET data=? WHERE id=?', (json.dumps(row), old['id']))
        p = app.context.preview_retirement(payload)
        app.monitor['sessions'][self.report['sessionId']]['goalStatus'] = 'paused'
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            app.context.retire(p['id'])
        p = app.context.preview_retirement(payload)
        with closing(app.progress.connect()) as db, db:
            row['status'] = 'needs-attention'
            db.execute('UPDATE handoffs SET data=? WHERE id=?', (json.dumps(row), old['id']))
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            app.context.retire(p['id'])

    def test_bounded_pages_repeat_stops_and_reject_changed_snapshot(self):
        app, fleet, old, new, payload = self.setup_context()
        for i in range(3, 8):
            h = {k: v for k, v in old.items() if k != 'assignmentContract'}
            h.update(id='unresolved-' + str(i), ticket=self.contract['taskUrl'].rsplit('/', 1)[0] + '/' + str(i))
            app.progress.observe_handoffs([h])
        app.monitor['sessions'][self.report['sessionId']]['goalStatus'] = 'paused'
        page = app.context.page({**payload, 'maxChars': 2000})
        self.assertLessEqual(len(page['text']), 2000)
        self.assertIn('paused', page['text'])
        self.assertIn('complete deployed instructions', page['text'])
        self.assertTrue(page['nextCursor'])
        next_page = app.context.page({**payload, 'maxChars': 2000, 'cursor': page['nextCursor']})
        self.assertIn('paused', next_page['text'])
        self.assertLessEqual(len(next_page['text']), 2000)
        self.assertTrue(any(i.get('overflow') for i in page['items'] + next_page['items']))
        app.triggers.persist()
        self.assertEqual(app.context.page({**payload, 'maxChars': 2000})['fingerprint'], page['fingerprint'])
        app.monitor['sessions'][self.report['sessionId']]['goalStatus'] = 'blocked'
        with self.assertRaisesRegex(ValueError, 'Context changed'):
            app.context.page({**payload, 'maxChars': 2000, 'cursor': page['nextCursor']})

    def test_unchanged_context_and_stale_goal_are_explicit(self):
        app, fleet, old, new, payload = self.setup_context()
        page = app.context.page(payload)
        unchanged = app.context.page({**payload, 'since': page['fingerprint']})
        self.assertTrue(unchanged['unchanged'])
        self.assertIn('Preserve explicit stops', unchanged['text'])
        app.monitor['sessions'][self.report['sessionId']]['observedAt'] = time.time() - 30
        stale = app.context.page({**payload, 'since': page['fingerprint']})
        self.assertNotEqual(stale['fingerprint'], page['fingerprint'])
        self.assertIn('unknown/stale', stale['text'])

    def test_long_policy_reference_returns_explicit_header_overflow(self):
        app, fleet = self.prepare()
        original = app.context.records
        def oversized(*args):
            header, records, reports = original(*args)
            header['instructions'] += ' Mandatory source constraints.' * 200
            return header, records, reports
        with patch.object(app.context, 'records', side_effect=oversized):
            result = app.context.page({'fleetId': fleet['id'], 'agentId': self.contract['agentId'], 'maxChars': 2000})
        self.assertTrue(result['overflow'])
        self.assertGreater(result['requiredChars'], 2000)
        self.assertIn('complete deployed instructions', result['text'])

    def test_goal_replacement_and_full_binding_invalidate_context(self):
        app, fleet, old, new, payload = self.setup_context()
        observed = app.monitor['sessions'][self.report['sessionId']]
        observed.update(goalStatus='active', goal={'id': 'one', 'createdAt': 1, 'objective': 'First scope'})
        page = app.context.page(payload)
        p = app.context.preview_retirement(payload)
        observed['goal'] = {'id': 'two', 'createdAt': 2, 'objective': 'Different scope'}
        self.assertNotEqual(app.context.page(payload)['fingerprint'], page['fingerprint'])
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            app.context.retire(p['id'])
        state = app.store.snapshot()
        state['fleets'][0]['agents'][1]['directory'] += '/different-checkout'
        Path(state['fleets'][0]['agents'][1]['directory']).mkdir()
        app.store.save(state)
        page = app.context.page(payload)
        self.assertTrue(any(i.get('reportBindingCurrent') is False for i in page['items']))

    def test_default_cli_stdout_obeys_bound_and_supplies_cursor(self):
        from fleet_studio import Server
        app, fleet, old, new, payload = self.setup_context()
        server = Server(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (app.root / '.runtime/server.json').write_text(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port), 'instanceId': app.instance_id}))
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'fleet_context.py'),
                                     '--fleet', payload['fleetId'], '--agent', payload['agentId'], '--data-root', str(app.root), '--max-chars', '2000'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(len(result.stdout), 2000)
            self.assertIn('Next cursor:', result.stdout)
            self.assertIn('Preserve explicit stops', result.stdout)
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_retirement_waiting_for_backlog_does_not_hold_progress_lock(self):
        app, fleet, old, new, payload = self.setup_context()
        p = app.context.preview_retirement(payload)
        waiting, finished, errors = threading.Event(), threading.Event(), []
        original_lock = app.backlog.lock
        class SignalledLock:
            def __enter__(self):
                waiting.set()
                original_lock.acquire()
            def __exit__(self, *args):
                original_lock.release()
        app.backlog.lock = SignalledLock()
        def retire():
            try:
                app.context.retire(p['id'])
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with original_lock:
            thread = threading.Thread(target=retire)
            thread.start()
            self.assertTrue(waiting.wait(2))
            acquired = app.progress.lock.acquire(timeout=2)
            self.assertTrue(acquired, 'Retirement must not invert backlog -> progress lock order.')
            if acquired:
                app.progress.lock.release()
        thread.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])


import test_continuations as continuation_fixtures


class RetirementAcceptanceTests(unittest.TestCase):
    setUp, tearDown = ContextTests.setUp, ContextTests.tearDown
    rule, monitor = ContextTests.rule, ContextTests.monitor
    prepare, observe = continuation_fixtures.ContinuationTests.prepare, continuation_fixtures.ContinuationTests.observe

    def accepted(self):
        app, fleet = self.prepare()
        app.progress.report({**self.report, 'kind': 'work-finished', 'revision': 'a' * 40,
                             'completionEvidence': {r['id']: self.contract['evidence'][0] for r in self.contract['requirements']}})
        rows = deepcopy(app.backlog.cache[fleet['id']]['items'])
        rows[0].update(state='closed', labels=['Accepted'])
        self.observe(app, fleet, items=rows)
        payload = {'fleetId': fleet['id'], 'agentId': self.contract['agentId']}
        return app, fleet, payload

    def test_tracker_reopen_or_removed_acceptance_restores_retired_obligation(self):
        for change in ({'state': 'opened'}, {'labels': []}):
            with self.subTest(change=change):
                app, fleet, payload = self.accepted()
                p = app.context.preview_retirement(payload)
                self.assertEqual(len(p['records']), 1)
                app.context.retire(p['id'])
                self.assertEqual(app.context.page(payload)['totalItems'], 0)
                rows = deepcopy(app.backlog.cache[fleet['id']]['items'])
                rows[0].update(change)
                self.observe(app, fleet, items=rows)
                page = app.context.page(payload)
                self.assertEqual(page['totalItems'], 1)
                with patch.object(app.backlog, 'snapshot', return_value=app.backlog.cache[fleet['id']]):
                    self.assertEqual(len(app.progress.snapshot(fleet['id'])['handoffs']), 1)
                self.tearDown(); self.setUp()

    def test_later_blocker_or_unrevalidated_release_cannot_retire_as_accepted(self):
        app, fleet, payload = self.accepted()
        p = app.context.preview_retirement(payload)
        app.context.retire(p['id'])
        app.progress.report({**self.report, 'kind': 'blocked', 'eventId': 'later-blocker', 'reason': 'New evidence conflicts', 'nextAction': 'Reconcile'})
        self.assertEqual(app.context.page(payload)['totalItems'], 1)
        self.assertEqual(app.context.preview_retirement(payload)['records'], [])
        p = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'reporting'})
        app.deployments.apply(p['id'])
        self.observe(app, app.store.fleet(fleet['id']), items=app.backlog.cache[fleet['id']]['items'])
        self.assertEqual(app.context.preview_retirement(payload)['records'], [])
