"""Typed handoff contracts; mocked native boundaries and no live tracker actions."""
from contextlib import closing
from copy import deepcopy
import json
import re
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import test_design as fixtures
from studio_contracts import SCHEMA, current_key, digest, validate
from studio_progress import Progress
from studio_triggers import Triggers


class ContractTests(unittest.TestCase):
    setUp, tearDown = fixtures.DesignTests.setUp, fixtures.DesignTests.tearDown
    rule, monitor = fixtures.DesignTests.rule, fixtures.DesignTests.monitor

    def prepare(self):
        app, fleet = fixtures.DesignTests.app(self)
        app.launch_enabled = False
        state = app.store.snapshot()
        state['fleets'][0]['backlog'] = {'projectUrl': 'https://gitlab.example/team/project', 'scope': 'all', 'agentTickets': {}}
        state['fleets'][0]['triggers'] = [self.rule(fleet, 'event', 'review')]
        app.store.save(state)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        fleet = app.store.fleet(fleet['id'])
        rid = app.deployments.data['active'][fleet['id']]
        for agent in fleet['agents']:
            app.deployments.acknowledge({'fleetId': fleet['id'], 'agentId': agent['id'], 'releaseId': rid})
        agent = fleet['agents'][1]
        self.contract = {'schemaVersion': 1, 'id': 'module-one-v1', 'taskUrl': fleet['backlog']['projectUrl'] + '/-/issues/1',
                         'agentId': agent['id'], 'releaseId': rid,
                         'binding': {**{k: agent[k] for k in ('sessionId', 'directory', 'roleIds')}, 'backend': 'codex', 'hostId': 'local'},
                         'scope': ['src/module_one', 'tests/test_module_one.py'], 'deliverable': 'Reviewed module first pass',
                         'requirements': [{'id': 'tests', 'description': 'Module tests pass'}, {'id': 'review', 'description': 'Review findings resolved'}],
                         'dependencies': [fleet['backlog']['projectUrl'] + '/-/issues/2'], 'evidence': [fleet['backlog']['projectUrl'] + '/-/issues/1#note_1'],
                         'nextAgentId': fleet['agents'][0]['id'], 'baseRevision': '', 'candidateRevision': 'a' * 40}
        self.event = {'fleetId': fleet['id'], 'agentId': fleet['agents'][0]['id'], 'targetAgentId': agent['id'],
                      'eventId': 'assignment-one', 'eventName': 'packet-ready', 'ticket': self.contract['taskUrl'],
                      'artifact': 'Module packet in the tracker', 'context': 'Review this bounded module.', 'assignmentContract': self.contract}
        self.report = {'fleetId': fleet['id'], 'agentId': agent['id'], 'sessionId': agent['sessionId'], 'releaseId': rid,
                       'eventId': 'started', 'kind': 'task-started', 'taskUrl': self.contract['taskUrl'],
                       'evidenceUrl': self.contract['evidence'][0], 'summary': 'Starting module', 'contractId': self.contract['id']}
        app.monitor.update(self.monitor(fleet))
        return app, fleet

    def test_preview_and_schema_roundtrip_have_no_side_effects(self):
        app, fleet = self.prepare()
        before = deepcopy(app.triggers.data)
        with patch.object(app.controls, 'send') as send:
            result = app.triggers.report(self.event, preview=True)
            send.assert_not_called()
        self.assertEqual(app.triggers.data, before)
        self.assertEqual(result['assignmentContract'], self.contract)
        self.assertIn('Assignment contract', result['messages'][0])
        self.assertEqual(validate(json.loads(json.dumps(self.contract)), app, fleet), self.contract)
        schema = json.loads((Path(__file__).resolve().parents[1] / 'workflows/assignment.schema.json').read_text())
        self.assertEqual(schema, SCHEMA)
        for value in ('main', 'a' * 39, 'b' * 41, 'x' + 'a' * 40):
            self.assertIsNone(re.search(schema['properties']['candidateRevision']['pattern'], value))
        self.assertIsNone(re.search(schema['properties']['id']['pattern'], '@valid-id'))

    def test_http_preview_requires_authentication_and_never_reserves(self):
        from fleet_studio import Server
        app, fleet = self.prepare()
        server = Server(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = Request('http://127.0.0.1:' + str(server.server_port) + '/api/events/preview',
                          data=json.dumps(self.event).encode(), headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as failure:
                urlopen(req)
            self.assertEqual(failure.exception.code, 403)
            req.add_header('X-Fleet-Token', app.token)
            with urlopen(req) as response:
                self.assertEqual(json.load(response)['assignmentContract']['id'], self.contract['id'])
            self.assertEqual(app.triggers.data['actions'], [])
            self.assertEqual(app.triggers.data['contractIds'], {})
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_invalid_scope_links_bindings_and_missing_facts_are_rejected(self):
        app, fleet = self.prepare()
        mutations = [{'scope': []}, {'scope': ['.']}, {'scope': ['../other']}, {'scope': ['C:/other']}, {'scope': ['a:stream']},
                     {'scope': ['a/../b']}, {'scope': ['//server/share']}, {'scope': ['*.py']}, {'requirements': []},
                     {'requirements': [self.contract['requirements'][0]] * 2}, {'releaseId': 'old'}, {'agentId': 'wrong'},
                     {'taskUrl': 'https://gitlab.example/other/project/-/issues/1'},
                     {'evidence': [self.contract['taskUrl'] + '?token=secret']}, {'evidence': ['https://user:secret@gitlab.example/team/project/1']},
                     {'dependencies': [self.contract['taskUrl']]}, {'candidateRevision': 'main'}, {'command': 'run me'},
                     {'binding': {**self.contract['binding'], 'sessionId': 'wrong'}}, {'nextAgentId': 'wrong'}]
        with patch.object(app.controls, 'send') as send:
            for mutation in mutations:
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    app.triggers.report({**self.event, 'assignmentContract': {**self.contract, **mutation}}, preview=True)
            send.assert_not_called()
        self.assertEqual(app.triggers.data['contractIds'], {})

    def test_event_and_contract_identities_survive_restart_and_supersession(self):
        app, fleet = self.prepare()
        original = app.triggers.report(self.event)
        app.triggers = Triggers(app)
        app.progress = Progress(app)
        self.assertTrue(app.triggers.report(self.event)['duplicate'])
        self.assertEqual(len(app.triggers.data['actions']), 1)
        with self.assertRaisesRegex(ValueError, 'contract ID'):
            app.triggers.report({**self.event, 'eventId': 'duplicate-contract'})
        replacement = {**self.contract, 'id': 'module-one-v2'}
        with self.assertRaisesRegex(ValueError, 'supersedes'):
            app.triggers.report({**self.event, 'eventId': 'replacement', 'assignmentContract': replacement})
        replacement['supersedes'] = self.contract['id']
        new = app.triggers.report({**self.event, 'eventId': 'replacement', 'assignmentContract': replacement})
        self.assertTrue(app.triggers.report(self.event)['duplicate'])
        with self.assertRaisesRegex(ValueError, 'superseded'):
            app.triggers.review(original['actions'][0]['id'])
        self.assertEqual(new['actions'][0]['assignmentContract']['id'], replacement['id'])
        self.assertEqual(app.triggers.data['currentContracts'][current_key(fleet['id'], replacement)], replacement['id'])

    def test_missing_requirements_and_wrong_candidate_cannot_submit(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        without_id = {k: v for k, v in self.report.items() if k != 'contractId'}
        with self.assertRaisesRegex(ValueError, 'contract ID'):
            app.progress.report(without_id)
        app.progress.report(self.report)
        app.progress.report({**self.report, 'eventId': 'waiting', 'kind': 'blocked', 'reason': 'Review pending', 'nextAction': 'Reviewer checks module'})
        submission = {**self.report, 'eventId': 'done', 'kind': 'work-finished', 'revision': 'a' * 40}
        with self.assertRaisesRegex(ValueError, 'Missing contract completion evidence'):
            app.progress.report(submission)
        evidence = {r['id']: self.contract['taskUrl'] + '#note_' + str(i) for i, r in enumerate(self.contract['requirements'], 2)}
        with self.assertRaisesRegex(ValueError, 'candidate'):
            app.progress.report({**submission, 'revision': 'b' * 40, 'completionEvidence': evidence})
        with self.assertRaisesRegex(ValueError, 'unknown contract requirement'):
            app.progress.report({**submission, 'completionEvidence': {'invented': self.contract['taskUrl']}})
        result = app.progress.report({**submission, 'completionEvidence': evidence})
        self.assertFalse(result['duplicate'])
        app.progress.report({**self.report, 'kind': 'progress-updated', 'eventId': 'more-context'})
        with patch.object(app.backlog, 'snapshot', return_value={}):
            assignment = app.progress.snapshot(fleet['id'])['agents'][1]['assignments'][0]
        self.assertEqual(assignment['state'], 'submitted')
        self.assertEqual(assignment['contract']['missingRequirements'], [])
        with self.assertRaisesRegex(ValueError, 'Missing contract completion evidence'):
            app.progress.report({**self.report, 'kind': 'progress-updated', 'eventId': 'clear-evidence', 'completionEvidence': {}})

    def test_base_revision_is_rechecked_immediately_before_native_delivery(self):
        app, fleet = self.prepare()
        self.contract['baseRevision'] = 'a' * 40
        fake = SimpleNamespace(returncode=0, stdout='a' * 40, stderr='')
        with patch('studio_contracts.subprocess.run', return_value=fake):
            action = app.triggers.report(self.event)['actions'][0]
            preview = app.triggers.review(action['id'])
            app.launch_enabled = True
            with patch('studio_control.threading.Thread'):
                app.controls.send(preview['id'])
        fake.stdout = 'b' * 40
        with patch('studio_contracts.subprocess.run', return_value=fake), patch.object(app.runtimes, 'for_agent') as backend:
            app.controls._dispatch(preview)
            backend.assert_not_called()
        self.assertEqual(app.controls.receipts[preview['id']]['targets'][0]['status'], 'held')
        self.assertIn('base revision', app.controls.receipts[preview['id']]['targets'][0]['detail'])

    def test_failed_reservation_does_not_consume_id_or_supersede(self):
        app, fleet = self.prepare()
        before = deepcopy(app.triggers.data)
        with patch.object(app.triggers, 'persist', side_effect=OSError('full disk')):
            with self.assertRaises(OSError):
                app.triggers.report(self.event)
        self.assertEqual(app.triggers.data, before)
        self.assertFalse(app.triggers.report(self.event)['duplicate'])

    def test_untyped_event_and_preexisting_dispatch_cannot_bypass_contract(self):
        app, fleet = self.prepare()
        legacy = {k: v for k, v in self.event.items() if k != 'assignmentContract'}
        legacy['eventId'] = 'legacy-before-contract'
        old = app.triggers.report(legacy)['actions'][0]
        preview = app.triggers.review(old['id'])
        app.launch_enabled = True
        with patch('studio_control.threading.Thread'):
            app.controls.send(preview['id'])
        app.triggers.report(self.event)
        with self.assertRaisesRegex(ValueError, 'typed assignment contract'):
            app.triggers.report({**legacy, 'eventId': 'legacy-after-contract'})
        with patch.object(app.runtimes, 'for_agent') as backend:
            app.controls._dispatch(preview)
            backend.assert_not_called()
        target = app.controls.receipts[preview['id']]['targets'][0]
        self.assertEqual(target['status'], 'held')
        self.assertIn('typed assignment contract', target['detail'])

    def test_noncanonical_task_alias_cannot_report_around_contract(self):
        app, fleet = self.prepare()
        app.triggers.report(self.event)
        for task in (self.contract['taskUrl'] + '?view=1', self.contract['taskUrl'][:-1] + '%31'):
            with self.assertRaisesRegex(ValueError, 'canonical task URL'):
                app.progress.report({k: v for k, v in {**self.report, 'taskUrl': task}.items() if k != 'contractId'})

    def test_earlier_receipts_remain_reportable_after_contract_replacement(self):
        app, fleet = self.prepare()
        old = app.triggers.report(self.event)['actions'][0]
        replacement = {**self.contract, 'id': 'module-one-v2', 'supersedes': self.contract['id']}
        new = app.triggers.report({**self.event, 'eventId': 'replacement', 'assignmentContract': replacement})['actions'][0]
        app.progress.report({**self.report, 'kind': 'handoff-received', 'handoffId': old['id']})
        with closing(app.progress.connect()) as db:
            old_row = json.loads(db.execute('SELECT data FROM handoffs WHERE id=?', (old['id'],)).fetchone()['data'])
            new_row = json.loads(db.execute('SELECT data FROM handoffs WHERE id=?', (new['id'],)).fetchone()['data'])
        self.assertIsNotNone(old_row['receivedAt'])
        self.assertIsNone(new_row['receivedAt'])

    def test_retention_keeps_typed_contract_and_dedupe_identity(self):
        app, fleet = self.prepare()
        action = app.triggers.report(self.event)['actions'][0]
        app.progress.report({**self.report, 'kind': 'handoff-received', 'handoffId': action['id']})
        app.progress.report({**self.report, 'eventId': 'implement'})
        app.progress.clock = lambda: action['createdAt'] + 100 * 86400
        with patch.object(app.backlog, 'snapshot'):
            app.progress.tick(app.store.snapshot(), {})
        app.progress = Progress(app)
        with patch.object(app.backlog, 'snapshot', return_value={}):
            assignment = app.progress.snapshot(fleet['id'])['agents'][1]['assignments'][0]
        self.assertEqual(assignment['contract']['id'], self.contract['id'])
        self.assertTrue(app.triggers.report(self.event)['duplicate'])

    def test_withdrawal_never_reactivates_an_older_envelope(self):
        app, fleet = self.prepare()
        old = app.triggers.report(self.event)['actions'][0]
        replacement = {**self.contract, 'id': 'module-one-v2', 'supersedes': self.contract['id']}
        new = app.triggers.report({**self.event, 'eventId': 'replacement', 'assignmentContract': replacement})['actions'][0]
        app.triggers.dismiss(new['id'])
        with self.assertRaisesRegex(ValueError, 'superseded'):
            app.triggers.review(old['id'])
        with self.assertRaisesRegex(ValueError, 'withdrawn'):
            app.progress.report({**self.report, 'contractId': replacement['id']})
        with patch.object(app.backlog, 'snapshot', return_value={}):
            assignment = app.progress.snapshot(fleet['id'])['agents'][1]['assignments'][0]
        self.assertEqual(assignment['contract']['id'], replacement['id'])
        self.assertEqual(assignment['state'], 'waiting')
        self.assertIn('withdrawn', assignment['reason'])
