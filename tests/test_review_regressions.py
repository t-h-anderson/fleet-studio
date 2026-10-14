"""Regression cases found in the backend/host/tracker review. No native effects."""
from contextlib import closing
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
import uuid

import test_design as fixtures
import test_progress as progress_fixtures
from fleet_studio import ValidationError
from fleet_node import Node
from test_agents import assistant, write_transcript
from studio_agents import ClaudeCodeBackend, ClaudeTranscript, Runtimes, RemoteHost, clean_hosts
from studio_bindings import session_key
from studio_progress import configuration
from studio_trackers import GitHubTracker, RUN_STATUS


class ReviewRegressions(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app

    def test_session_ids_are_scoped_to_host_backend_in_config_and_monitoring(self):
        app, f = self.app(); state = app.store.snapshot()
        a, b = state['fleets'][0]['agents']
        b.update(sessionId=a['sessionId'], backend='claude-code')
        saved = app.store.save(state); agents = saved['fleets'][0]['agents']
        local = {'sessions': {a['sessionId']: {'sessionId': a['sessionId'], 'status': 'working'}}, 'catalogue': [], 'errors': []}
        claude = {'sessions': {a['sessionId']: {'sessionId': a['sessionId'], 'status': 'closed'}}, 'catalogue': [], 'errors': []}
        with patch.object(app.runtimes.local['codex'], 'observe', return_value=local), patch.object(app.runtimes.local['claude-code'], 'observe', return_value=claude), patch.object(app.runtimes.local['claude-code'], 'catalogue', return_value=[]):
            m = app.runtimes.observe(agents)
        self.assertEqual(len(m['sessions']), 2)
        self.assertEqual(m['sessions'][session_key(a)]['status'], 'working')
        self.assertEqual(m['sessions'][session_key(b)]['status'], 'closed')
        state = app.store.snapshot(); state['fleets'][0]['agents'][1]['backend'] = 'codex'
        with self.assertRaises(ValidationError): app.store.save(state)

    def test_token_counters_do_not_mix_same_uuid_on_different_hosts(self):
        app, f = self.app(); a, b = f['agents']; b.update(sessionId=a['sessionId'], hostId='other')
        now = time.time()
        sessions = {session_key(a): {'sessionId': a['sessionId'], 'observedAt': now, 'usage': {'tokensUsed': 100}},
                    session_key(b): {'sessionId': b['sessionId'], 'hostId': 'other', 'observedAt': now, 'usage': {'tokensUsed': 900}}}
        app.progress.metrics.observe_tokens([f], {'sessions': sessions})
        with closing(app.progress.connect()) as db:
            totals = {r['session']: json.loads(r['data'])['total'] for r in db.execute('SELECT * FROM token_counters')}
        self.assertEqual(totals, {session_key(a): 100, session_key(b): 900})

    def queued_preview(self, app, f):
        p = app.controls.preview({'fleetId': f['id'], 'agentIds': [f['agents'][0]['id']], 'action': 'message', 'message': 'Bounded instruction'})
        with patch('studio_control.threading.Thread'):
            app.controls.send(p['id'])
        return p

    def test_rebinding_after_send_before_dispatch_never_reroutes_instruction(self):
        app, f = self.app(); p = self.queued_preview(app, f)
        state = app.store.snapshot(); state['fleets'][0]['agents'][0]['backend'] = 'claude-code'; app.store.save(state)
        with patch.object(app.runtimes.local['codex'], 'send') as codex, patch.object(app.runtimes.local['claude-code'], 'send') as claude:
            app.controls._dispatch(p)
        codex.assert_not_called(); claude.assert_not_called()
        self.assertEqual(app.controls.receipts[p['id']]['targets'][0]['status'], 'failed')

    def test_retargeted_host_after_preview_cannot_receive_old_control(self):
        app, f = self.app(); state = app.store.snapshot()
        state['hosts'] = [{'id': 'remote', 'url': 'https://original.example', 'tokenEnv': 'TEST_TOKEN'}]
        state['fleets'][0]['agents'][0]['hostId'] = 'remote'; app.store.save(state)
        f = app.store.fleet(f['id']); p = self.queued_preview(app, f)
        state = app.store.snapshot(); state['hosts'][0]['url'] = 'https://replacement.example'; app.store.save(state)
        with patch('studio_agents.RemoteHost.call') as call:
            app.controls._dispatch(p)
        call.assert_not_called()
        self.assertEqual(app.controls.receipts[p['id']]['targets'][0]['status'], 'failed')

    def test_retargeted_host_cannot_receive_a_previously_scheduled_launch(self):
        app, f = self.app(); state = app.store.snapshot()
        state['hosts'] = [{'id': 'remote', 'url': 'https://original.example', 'tokenEnv': 'TEST_TOKEN'}]
        state['fleets'][0]['agents'][0]['hostId'] = 'remote'; app.store.save(state)
        f = app.store.fleet(f['id']); binding = app.store.launch_config(f, [f['agents'][0]['id']])['bindings'][0]
        state = app.store.snapshot(); state['hosts'][0]['url'] = 'https://replacement.example'; app.store.save(state)
        with patch('studio_agents.RemoteHost.call') as call:
            with self.assertRaisesRegex(ValueError, 'binding changed'): app._launch_agent(f, binding)
        call.assert_not_called()

    def test_runtime_rebinding_invalidates_adoption_reports_and_goal_links(self):
        app, f = progress_fixtures.ProgressTests.app(self)
        rid = app.deployments.data['active'][f['id']]; aid = f['agents'][0]['id']
        app.deployments.acknowledge({'fleetId': f['id'], 'agentId': aid, 'releaseId': rid})
        state = app.store.snapshot(); state['fleets'][0]['agents'][0]['backend'] = 'claude-code'; app.store.save(state)
        with self.assertRaises(ValueError): app.deployments.acknowledge({'fleetId': f['id'], 'agentId': aid, 'releaseId': rid})
        with self.assertRaises(ValueError): app.progress.report(self.payload)
        with self.assertRaises(ValueError): app.goals.binding(f['id'], aid)
        self.assertTrue(app.deployments.adoption_hold(f['id'], [aid]))
        row = next(a for a in app.deployments.overview(app.store.fleet(f['id']))['adoption'] if a['agentId'] == aid)
        self.assertFalse(row['acknowledged']); self.assertFalse(row['bindingMatches'])
        from workflow_client import instructions
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            instructions(f['id'], aid, self.root, f['agents'][0]['directory'])

    def test_claude_assignment_is_not_installed_as_a_codex_entrypoint(self):
        app, f = self.app(); state = app.store.snapshot()
        state['fleets'][0]['agents'][0]['backend'] = 'claude-code'; app.store.save(state)
        p = app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'}); app.deployments.apply(p['id'])
        with self.assertRaisesRegex(ValueError, 'Codex only'):
            app.entrypoints.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'agentIds': [f['agents'][0]['id']]})

    def test_remote_folder_never_receives_a_local_entrypoint(self):
        app, f = self.app(); p = app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'}); app.deployments.apply(p['id'])
        state = app.store.snapshot(); state['hosts'] = [{'id': 'remote', 'url': 'https://host.example', 'tokenEnv': 'HOST_TOKEN'}]
        state['fleets'][0]['agents'][0]['hostId'] = 'remote'; app.store.save(state)
        with patch.object(app.entrypoints, 'target') as target:
            with self.assertRaisesRegex(ValueError, 'Remote entry-point'):
                app.entrypoints.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'agentIds': [f['agents'][0]['id']]})
        target.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'remote assignment'):
            app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})

    def claude(self):
        sid = str(uuid.uuid4()); path = write_transcript(self.root, sid, [assistant('Done')], cwd=str(self.root))
        os.utime(path, (time.time()-120, time.time()-120))
        return ClaudeCodeBackend(root=self.root, runtime=self.root / '.runtime'), sid, path

    def test_unknown_claude_ownership_never_starts_a_turn(self):
        backend, sid, _ = self.claude()
        with patch('studio_agents.claude_processes', return_value=None), patch('studio_agents.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'unavailable'): backend.send(sid, 'Review', str(self.root))
            self.assertEqual(backend.observe([sid])['sessions'][sid]['status'], 'unknown')
        popen.assert_not_called()

    def test_only_one_studio_claude_process_can_run_per_session(self):
        backend, sid, _ = self.claude(); process = Mock(); process.poll.return_value = None
        with patch('studio_agents.claude_processes', return_value=[]), patch('studio_agents.subprocess.Popen', return_value=process) as popen:
            self.assertEqual(backend.send(sid, '--help is part of this instruction', str(self.root))[0], 'started')
            with self.assertRaisesRegex(RuntimeError, 'already running'): backend.send(sid, 'Again', str(self.root))
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0][-2:], ['--', '--help is part of this instruction'])

    def test_node_launch_rechecks_native_ownership(self):
        node = Node(self.root); sid = str(uuid.uuid4()); backend = node.backends['codex']
        payload = {'backend': 'codex', 'sessionId': sid, 'directory': str(self.root), 'name': 'Review', 'window': 'review'}
        with patch.object(backend, 'observe', return_value={'sessions': {sid: {'status': 'idle', 'pids': [99]}}}), patch('fleet_node.tmux_launch') as launch:
            self.assertFalse(node.handle('launch', payload)['opened'])
            launch.assert_not_called()
        with patch.object(backend, 'observe', return_value={'sessions': {sid: {'status': 'unknown', 'pids': []}}}), patch('fleet_node.tmux_launch') as launch:
            with self.assertRaisesRegex(RuntimeError, 'not confirmed closed'): node.handle('launch', payload)
            launch.assert_not_called()

    def test_remote_server_error_after_mutation_is_uncertain(self):
        import io
        host = RemoteHost({'id': 'node', 'url': 'https://node.example', 'tokenEnv': 'REVIEW_NODE_TOKEN'})
        failure = HTTPError(host.url, 502, 'Bad Gateway', {}, io.BytesIO(b'{"error":"timeout after effect"}'))
        with patch.dict(os.environ, {'REVIEW_NODE_TOKEN': 't'*32}), patch('studio_agents.build_opener') as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaisesRegex(TimeoutError, 'inspect native state'): host.call('/node/send', {})

    def test_claude_tokens_deduplicate_streamed_usage_and_include_cache_reads(self):
        sid = str(uuid.uuid4()); one = assistant('Block 1'); one['message']['id'] = 'api-message'
        two = deepcopy(one); two['uuid'] = uuid.uuid4().hex; two['message']['usage']['output_tokens'] = 9
        path = write_transcript(self.root, sid, [one, two, two])
        self.assertEqual(ClaudeTranscript(path).refresh()['tokensUsed'], 1019)

    def test_inactive_tool_turn_is_not_inferred_completed_from_age(self):
        backend, sid, path = self.claude(); write_transcript(self.root, sid, [assistant('Tool pending', stop='tool_use')])
        os.utime(path, (time.time()-120, time.time()-120))
        with patch('studio_agents.claude_processes', return_value=[123]):
            self.assertEqual(backend.observe([sid])['sessions'][sid]['turnStatus'], 'inProgress')

    def test_provider_change_does_not_reuse_backlog_or_ci_evidence(self):
        app, f = progress_fixtures.ProgressTests.app(self)
        old_config, old_source = configuration(f)
        app.progress.observe_backlog(f, {'items': [], 'observedAt': self.now, 'partial': False})
        metrics = app.progress.metrics
        metrics.ci[f['id']] = {'project': old_config['projectUrl'], 'provider': 'gitlab', 'pipelines': [{'status': 'success'}]}
        f['backlog']['provider'] = 'github'
        self.assertNotEqual(configuration(f)[1], old_source)
        self.assertIsNone(app.progress.cached_backlog(f))
        self.assertEqual(metrics.ci_snapshot(f)['pipelines'], [])
        self.assertEqual(metrics.ci_snapshot(f)['label'], 'GitHub CI')

    def test_progress_rejects_issue_prefix_lookalikes(self):
        app, _ = progress_fixtures.ProgressTests.app(self)
        with self.assertRaises(ValueError):
            app.progress.report({**self.payload, 'taskUrl': self.payload['taskUrl'].replace('/issues/1', '/issues-other/1')})

    def test_github_history_reads_beyond_oldest_page_and_preserves_neutral_status(self):
        oldest = [{'event': 'assigned'}] * 100
        newest = [{'event': 'labeled', 'created_at': '2026-10-14', 'label': {'name': 'AI Ready'}}]
        with patch('studio_trackers.github_get', side_effect=[oldest, newest]) as get:
            history = GitHubTracker().history({'projectUrl': 'https://github.com/o/r'}, 1)
        self.assertEqual(history[0]['label']['name'], 'AI Ready')
        self.assertIn('page=2', get.call_args.args[1])
        self.assertEqual(RUN_STATUS['neutral'], 'neutral')

    def test_node_redirect_cannot_forward_bearer_token(self):
        received = []
        class Target(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                received.append(self.headers.get('Authorization')); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.send_response(302); self.send_header('Location', 'http://127.0.0.1:' + str(target.server_port) + '/'); self.end_headers()
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        for server in (target, origin): threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict(os.environ, {'REVIEW_NODE_TOKEN': 't'*32}):
                host = RemoteHost({'id': 'node', 'url': 'http://127.0.0.1:' + str(origin.server_port), 'tokenEnv': 'REVIEW_NODE_TOKEN'})
                with self.assertRaises(RuntimeError): host.call('/node/send', {})
            self.assertEqual(received, [])
        finally:
            for server in (origin, target): server.shutdown(); server.server_close()
        with self.assertRaises(ValueError): clean_hosts([{'id': 'bad', 'url': 'https:///missing', 'tokenEnv': 'TOKEN'}])
