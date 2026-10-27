"""Goal-first briefs, the three agent reports and observed receipts."""
from contextlib import closing
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_design as fixtures
from studio_deployments import brief_changes
from workflow_spec import apply_spec, brief_document, export_spec, validate


class BriefTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app

    def with_goal(self, app):
        state = app.store.snapshot()
        fleet = state['fleets'][0]
        writer = next(r for r in fleet['roles'] if r['key'] == 'writer')
        writer.update(goal='Ship small, tested changes that close tickets.',
                      successCriteria=['Tests pass', 'Ticket closed'], doneWhen='The MR is merged.')
        app.store.save(state)
        return app.store.fleet(fleet['id'])

    def test_brief_leads_with_goal_and_stays_short(self):
        app, fleet = self.app()
        fleet = self.with_goal(app)
        spec = export_spec(fleet)
        self.assertEqual(spec['schemaVersion'], 2)
        text = brief_document(fleet, spec, fleet['agents'][0], 'a' * 32, Path('/studio'))
        self.assertTrue(text.startswith('# ' + fleet['agents'][0]['name'] + ' — Writer'))
        self.assertLess(text.index('Ship small'), text.index('## Reporting'))
        self.assertIn('- Tests pass', text)
        self.assertIn('Done when: The MR is merged.', text)
        self.assertLess(len(text.split()), 400)
        for phrase in ('workflow_client.py" acknowledge', 'handoff-received', 'stable event ID', '## Ticket states'):
            self.assertNotIn(phrase, text)

    def test_fleet_wide_policies_reach_every_brief_without_bloating_it(self):
        app, fleet = self.app()
        spec = export_spec(fleet)
        spec['policies'] = [{'id': 'short-rule', 'name': 'Small commits', 'roles': [], 'sources': [], 'text': 'Keep each commit small and focused.'},
                            {'id': 'long-rule', 'name': 'Release process', 'roles': [], 'sources': [], 'text': ' '.join(['step'] * 200)}]
        text = brief_document(fleet, spec, fleet['agents'][0], 'a' * 32, Path('/studio'))
        self.assertIn('Keep each commit small and focused.', text)
        self.assertIn('Also in force', text)
        self.assertIn('Release process', text)
        self.assertNotIn('step step step', text)

    def test_client_reports_an_unavailable_studio_in_one_line(self):
        import subprocess
        client = Path(__file__).resolve().parents[1] / 'workflow_client.py'
        result = subprocess.run([sys.executable, str(client), 'report', 'done', '--fleet', 'f', '--agent', 'a', '--note', 'x'],
                                cwd=self.root, capture_output=True, text=True, env={'PATH': '', 'PYTHONPATH': str(client.parent)})
        self.assertEqual(result.returncode, 3, result.stderr)  # Distinct from success, still one line.
        self.assertEqual(result.stdout.strip().count('\n'), 0)
        self.assertNotIn('Traceback', result.stdout + result.stderr)

    def test_goal_fields_round_trip_and_v1_specs_still_validate(self):
        app, fleet = self.app()
        fleet = self.with_goal(app)
        spec = export_spec(fleet)
        restored = apply_spec(fleet, spec)
        writer = next(r for r in restored['roles'] if r['key'] == 'writer')
        self.assertEqual(writer['successCriteria'], ['Tests pass', 'Ticket closed'])
        sample = json.loads((Path(__file__).resolve().parents[1] / 'workflows' / 'sample.workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(sample['schemaVersion'], 2)
        sample['schemaVersion'] = 1
        for role in sample['roles']:
            for key in ('goal', 'successCriteria', 'doneWhen'):
                role.pop(key, None)
        self.assertEqual(validate(sample)['schemaVersion'], 1)

    def test_brief_changes_name_sections(self):
        old = '# A\n\n## Goal\n\nOld\n\n## Reporting\n\nSame\n\nBrief 1 · release x'
        new = '# A\n\n## Goal\n\nNew\n\n## Reporting\n\nSame\n\nBrief 2 · release y'
        self.assertEqual(brief_changes(old, new), ['Goal'])


class ReportTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app

    def open_work(self, app, fleet, agent):
        with closing(app.progress.connect()) as db:
            return app.health.open_work(db, fleet['id'], agent)

    def test_blocked_persists_until_done(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        result = app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Need the API key'})
        self.assertTrue(result['recorded'])
        work, blocked = self.open_work(app, fleet, writer)
        self.assertTrue(work)
        self.assertEqual(blocked['reason'], 'Need the API key')
        app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Merged',
                            'ticket': 'https://gitlab.example/team/project/-/issues/1'})
        self.assertEqual(self.open_work(app, fleet, writer), (False, None))

    def test_direct_handoff_reaches_a_role_member(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            result = app.reports.report({'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': writer['id'],
                                         'to': 'Integrator', 'note': 'Packet ready on branch x'})
            preview = app.controls.previews[send.call_args[0][0]]
        self.assertEqual(result['recipient'], integrator['name'])
        self.assertEqual(preview['targets'][0]['agentId'], integrator['id'])
        self.assertIn('Packet ready on branch x', preview['targets'][0]['text'])
        fire = app.automation.data['fires'][-1]
        self.assertEqual((fire['ruleId'], fire['status']), ('agent-handoff', 'sending'))

    def test_unblocked_clears_the_blocker(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Need the API key'})
        app.reports.report({'kind': 'unblocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Key arrived'})
        # Unblocked clears the blocker and leaves open work to the normal task checks (none here).
        self.assertEqual(self.open_work(app, fleet, writer), (False, None))

    def test_retried_direct_handoff_is_sent_once(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        payload = {'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': writer['id'], 'to': 'Integrator', 'note': 'Packet ready'}
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            first = app.reports.report(payload)
            second = app.reports.report(payload)
        send.assert_called_once()
        self.assertEqual(first['recipient'], second['recipient'])

    def test_workflow_handoff_without_ticket_is_delivered_directly(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        state = app.store.snapshot()
        state['fleets'][0]['triggers'] = [fixtures.DesignTests.rule(self, fleet, 'event')]
        app.store.save(state)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            result = app.reports.report({'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': writer['id'], 'to': 'Integrator', 'note': 'Ready'})
            send.assert_called_once()
        self.assertEqual((result['route'], result['recipient']), ('direct', integrator['name']))

    def test_repeated_identical_handoff_with_new_retry_id_is_sent_again(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        payload = {'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': writer['id'], 'to': 'Integrator', 'note': 'Ready for review'}
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.reports.report({**payload, 'retryId': 'a'})
            app.reports.report({**payload, 'retryId': 'a'})
            app.reports.report({**payload, 'retryId': 'b'})
        self.assertEqual(send.call_count, 2)

    def test_unknown_handoff_target_is_one_clear_error(self):
        app, fleet = self.app()
        with self.assertRaisesRegex(ValueError, 'No role or agent called Nobody.*Use --to with one of: .*Integrator'):
            app.reports.report({'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': fleet['agents'][0]['id'],
                                'to': 'Nobody', 'note': 'x'})

    def test_receipt_is_observed_from_the_next_turn(self):
        app, fleet = self.app()
        integrator = fleet['agents'][1]
        created = time.time() - 60
        handoff = {'id': 'h1', 'createdAt': created, 'status': 'queued', 'targetAgentId': integrator['id'], 'receivedAt': None,
                   'targetSessionId': integrator['sessionId']}
        with closing(app.progress.connect()) as db, db:
            db.execute('INSERT INTO handoffs VALUES(?,?,?)', (fleet['id'], 'h1', json.dumps(handoff)))
        app.progress.observe_receipts([fleet])
        with closing(app.progress.connect()) as db:
            self.assertIsNone(json.loads(db.execute('SELECT data FROM handoffs').fetchone()['data'])['receivedAt'])
        unrelated = {'id': 't0', 'session': integrator['sessionId'], 'startedAt': created + 2, 'lastAt': created + 3, 'status': 'completed',
                     'tokens': {}, 'calls': [], 'classes': {}, 'fromStudio': False}
        app.telemetry.store(fleet['id'], integrator['id'], [unrelated])
        app.progress.observe_receipts([fleet])
        with closing(app.progress.connect()) as db:
            self.assertIsNone(json.loads(db.execute('SELECT data FROM handoffs').fetchone()['data'])['receivedAt'])
        app.telemetry.store(fleet['id'], integrator['id'], [{**unrelated, 'id': 't1', 'startedAt': created + 5, 'lastAt': created + 20, 'fromStudio': True}])
        app.progress.observe_receipts([fleet])
        with closing(app.progress.connect()) as db:
            saved = json.loads(db.execute('SELECT data FROM handoffs').fetchone()['data'])
        self.assertEqual((saved['receivedAt'], saved['receiptSource']), (created + 5, 'observed-turn'))


if __name__ == '__main__':
    unittest.main()
