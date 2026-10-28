from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_telemetry import TranscriptFollower, TurnParser, call_metadata, classify, output_failed, shell_label

SECRET = 'TOP-SECRET-OUTPUT'


def claude_rows():
    return [
        {'type': 'user', 'uuid': 'u1', 'timestamp': '2026-10-27T10:00:00Z',
         'message': {'role': 'user', 'content': '[Fleet Studio] Fix the failing test'}},
        {'type': 'assistant', 'timestamp': '2026-10-27T10:00:05Z',
         'message': {'id': 'm1', 'model': 'x', 'stop_reason': 'tool_use',
                     'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 100},
                     'content': [{'type': 'text', 'text': 'Running the focused tests for the contract now.'},
                                 {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                                  'input': {'command': 'cd /repo && pytest tests/test_x.py -q'}}]}},
        # Streaming repeats the same message with larger usage.
        {'type': 'assistant', 'timestamp': '2026-10-27T10:00:06Z',
         'message': {'id': 'm1', 'stop_reason': 'tool_use',
                     'usage': {'input_tokens': 10, 'output_tokens': 8, 'cache_read_input_tokens': 100},
                     'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                                  'input': {'command': 'cd /repo && pytest tests/test_x.py -q'}}]}},
        {'type': 'user', 'timestamp': '2026-10-27T10:00:09Z',
         'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'is_error': True,
                                                   'content': SECRET}]}},
        {'type': 'assistant', 'timestamp': '2026-10-27T10:00:12Z',
         'message': {'id': 'm2', 'stop_reason': 'tool_use', 'usage': {'input_tokens': 3, 'output_tokens': 2},
                     'content': [{'type': 'tool_use', 'id': 't2', 'name': 'Edit',
                                  'input': {'file_path': '/repo/x.py', 'old_string': SECRET, 'new_string': SECRET}}]}},
        {'type': 'assistant', 'timestamp': '2026-10-27T10:00:20Z',
         'message': {'id': 'm3', 'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1},
                     'content': [{'type': 'text', 'text': 'Done: the contract test passes again.'}]}},
    ]


def codex_rows():
    return [
        {'timestamp': '2026-10-27T10:00:00Z', 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-1'}},
        {'timestamp': '2026-10-27T10:00:01Z', 'type': 'response_item',
         'payload': {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
                     'arguments': json.dumps({'command': ['bash', '-lc', 'python workflow_client.py report blocked --note waiting']})}},
        {'timestamp': '2026-10-27T10:00:02Z', 'type': 'response_item',
         'payload': {'type': 'function_call_output', 'call_id': 'c1',
                     'output': json.dumps({'output': SECRET, 'metadata': {'exit_code': 0}})}},
        {'timestamp': '2026-10-27T10:00:03Z', 'type': 'response_item',
         'payload': {'type': 'custom_tool_call', 'name': 'apply_patch', 'call_id': 'c2',
                     'input': '*** Begin Patch\n*** Update File: src/a.py\n+' + SECRET + '\n*** End Patch'}},
        {'timestamp': '2026-10-27T10:00:04Z', 'type': 'response_item',
         'payload': {'type': 'function_call_output', 'call_id': 'c2', 'output': 'Exit code: 1\n' + SECRET}},
        {'timestamp': '2026-10-27T10:00:05Z', 'type': 'event_msg',
         'payload': {'type': 'token_count', 'info': {
             'total_token_usage': {'input_tokens': 500, 'cached_input_tokens': 400, 'output_tokens': 50},
             'last_token_usage': {'input_tokens': 500, 'cached_input_tokens': 400, 'output_tokens': 50}}}},
        {'timestamp': '2026-10-27T10:00:06Z', 'type': 'response_item',
         'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Patched.'}]}},
        {'timestamp': '2026-10-27T10:00:07Z', 'type': 'event_msg',
         'payload': {'type': 'token_count', 'info': {
             'total_token_usage': {'input_tokens': 700, 'cached_input_tokens': 500, 'output_tokens': 80}}}},
        {'timestamp': '2026-10-27T10:00:08Z', 'type': 'event_msg', 'payload': {'type': 'task_complete'}},
    ]


class SignatureTests(unittest.TestCase):
    def test_shell_wrappers_and_directory_changes_are_removed(self):
        self.assertEqual(shell_label(['bash', '-lc', 'cd /a && cd b && npm test -- --watch=false\necho']), 'npm test -- --watch=false')
        self.assertEqual(shell_label('  # comment\n  git   status '), 'git status')

    def test_classification(self):
        self.assertEqual(classify('Bash', 'python fleet_progress.py --kind blocked'), 'admin')
        self.assertEqual(classify('Bash', 'gh issue comment 4 --body x'), 'admin')
        self.assertEqual(classify('Bash', 'sleep 30'), 'wait')
        self.assertEqual(classify('Bash', 'git diff HEAD'), 'explore')
        self.assertEqual(classify('Bash', 'pytest -q'), 'work')
        self.assertEqual(classify('Read', '/a'), 'explore')
        self.assertEqual(classify('Edit', '/a'), 'work')

    def test_signature_is_bounded_and_stable(self):
        first = call_metadata('Bash', {'command': 'x' * 500})
        self.assertEqual(len(first['label']), 160)
        self.assertEqual(first['sig'], call_metadata('Bash', {'command': 'x' * 500})['sig'])

    def test_output_failure_flags(self):
        self.assertTrue(output_failed('Exit code: 2\nboom'))
        self.assertFalse(output_failed('Exit code: 0'))
        self.assertTrue(output_failed(json.dumps({'output': '', 'metadata': {'exit_code': 1}})))


class ParserTests(unittest.TestCase):
    def test_claude_turn(self):
        parser = TurnParser('s1', 'claude-code')
        for row in claude_rows():
            parser.feed(row)
        [turn] = parser.drain()
        self.assertEqual(turn['status'], 'completed')
        self.assertTrue(turn['fromStudio'])
        self.assertEqual(turn['tokens'], {'input': 14, 'output': 11, 'cacheRead': 100, 'cacheWrite': 0})
        self.assertEqual([c['tool'] for c in turn['calls']], ['Bash', 'Edit'])
        self.assertEqual(turn['calls'][0]['label'], 'pytest tests/test_x.py -q')
        self.assertTrue(turn['calls'][0]['error'])
        self.assertEqual(turn['errors'], 1)
        self.assertEqual(turn['classes']['work'], 2)
        self.assertEqual(len(turn['replies']), 2)
        self.assertNotIn(SECRET, json.dumps(turn))

    def test_codex_turn(self):
        parser = TurnParser('s2', 'codex')
        for row in codex_rows():
            parser.feed(row)
        [turn] = parser.drain()
        self.assertEqual(turn['id'], 'turn-1')
        self.assertEqual(turn['status'], 'completed')
        self.assertEqual(turn['classes']['admin'], 1)
        self.assertEqual(turn['calls'][1]['label'], 'src/a.py')
        self.assertEqual([c['error'] for c in turn['calls']], [False, True])
        # First total counts only the last call; later totals add deltas.
        self.assertEqual(turn['tokens'], {'input': 200, 'output': 80, 'cacheRead': 500, 'cacheWrite': 0})
        self.assertNotIn(SECRET, json.dumps(turn))

    def test_new_prompt_aborts_an_open_turn(self):
        parser = TurnParser('s1', 'claude-code')
        rows = claude_rows()
        for row in rows[:2] + [dict(rows[0], uuid='u2', timestamp='2026-10-27T10:05:00Z')]:
            parser.feed(row)
        turns = {t['id']: t for t in parser.drain()}
        self.assertEqual(turns['u1']['status'], 'aborted')
        self.assertEqual(turns['u2']['status'], 'open')

    def test_follower_reads_appended_complete_records_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 's.jsonl'
            rows = claude_rows()
            path.write_text('\n'.join(json.dumps(r) for r in rows[:2]) + '\n' + json.dumps(rows[2])[:20], encoding='utf-8')
            follower = TranscriptFollower(path, 's', 'claude-code')
            [turn] = follower.read()
            self.assertEqual(turn['status'], 'open')
            self.assertEqual(follower.read(), [])
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(rows[2])[20:] + '\n' + '\n'.join(json.dumps(r) for r in rows[3:]) + '\n')
            [turn] = follower.read()
            self.assertEqual(turn['status'], 'completed')
            self.assertEqual(turn['tokens']['output'], 11)


class AccuracyTests(unittest.TestCase):
    def test_secrets_are_redacted_from_labels(self):
        commands = {
            'GITHUB_TOKEN=ghp_abcdefghijk123456 gh pr create -t "x"': 'ghp_',
            'curl -H "Authorization: Bearer abc.def.ghi" https://api.example/a?token=zzz': 'abc.def',
            'mysql -u root -pHUNTER2 db': 'HUNTER2',
            'git commit -m "secret launch plan"': 'secret launch',
            'export API_KEY=sk-live-abcdefghijkl && ./deploy': 'sk-live',
            'deploy --token abc123secret': 'abc123secret',
            'git clone https://user:pa55word@example.com/r.git': 'pa55word',
        }
        for command, secret in commands.items():
            label = call_metadata('Bash', {'command': command})['label']
            self.assertNotIn(secret, label, command)
        grep = call_metadata('Grep', {'pattern': 'password=hunter2', 'path': 'src'})
        self.assertNotIn('hunter2', grep['label'])
        self.assertTrue(grep['label'].endswith(' in src'))

    def test_common_credential_forms_are_redacted(self):
        S = 'S3CR3TVALUE'
        LEAK_CASES = [
            f'cd repo && GITHUB_TOKEN={S} gh pr list', f'echo hi; API_KEY={S} npm test', f'PGPASSWORD={S} psql -h db',
            f'env SECRET_KEY={S} python app.py', f'export OPENAI_API_KEY="{S} x"', f'curl -u admin:{S} https://x.io',
            f'curl --user admin:{S} https://x.io', f'curl -H "X-Api-Key: {S}" https://x.io', f"curl -H 'Cookie: session={S}' https://x.io",
            f'curl --header "Private-Token: {S}" https://gitlab', f'curl -d "user=a&password={S}" https://x.io',
            f"curl --data '{{\"password\": \"{S}\"}}' https://x.io", f'docker login -u me -p {S} reg.io', f'sshpass -p {S} ssh host',
            f'mysql -uroot -p{S} db', f'tool --client-secret {S}', f'tool --access-token={S}', f'tool --private-key {S}',
            f'aws configure set aws_secret_access_key {S}', f'kubectl create secret generic s --from-literal=password={S}',
            f'git clone https://me:{S}@github.com/a/b', f'curl https://x.io/a?token={S}', f'curl -H "Authorization: Bearer {S}" x',
            f'npm config set //registry.npmjs.org/:_authToken={S}', f'echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.{S}',
            f'run sk-ant-{S}abcdef', f'run AIzaSy{S}abcdefghijklmnopq', f'pip install --index-url https://u:{S}@pypi.x/simple pkg',
            f'TOKEN={S} make deploy', f'heroku config:set DB_PASSWORD={S}',
        ]
        for command in LEAK_CASES:
            self.assertNotIn(S, call_metadata('Bash', {'command': command})['label'], command)
        for command in ['pytest tests/test_a.py -k token', 'grep -rn password src/', 'mkdir -p build']:
            self.assertEqual(call_metadata('Bash', {'command': command})['label'], command)

    def test_redaction_is_linear_on_adversarial_labels(self):
        from studio_telemetry import redact
        for text in ['pass' * 160, 'auth' * 160, '--pass' * 100, 'x-' * 320, 'a.' * 320]:
            start = time.perf_counter()
            redact(text)
            self.assertLess(time.perf_counter() - start, 0.05)

    def test_signatures_distinguish_full_arguments(self):
        sig = lambda tool, args: call_metadata(tool, args)['sig']
        self.assertNotEqual(sig('Grep', {'pattern': 'a', 'path': 'src'}), sig('Grep', {'pattern': 'b', 'path': 'src'}))
        self.assertNotEqual(sig('Read', {'file_path': 'f', 'offset': 0}), sig('Read', {'file_path': 'f', 'offset': 500}))
        self.assertNotEqual(sig('Edit', {'file_path': 'f', 'new_string': 'x'}), sig('Edit', {'file_path': 'f', 'new_string': 'y'}))
        first = sig('Bash', {'command': "apply_patch <<'P'\n*** Update File: a.py\n+one\nP"})
        self.assertNotEqual(first, sig('Bash', {'command': "apply_patch <<'P'\n*** Update File: a.py\n+two\nP"}))
        self.assertTrue(call_metadata('TodoWrite', {'todos': []}).get('exempt'))

    def test_classification_of_reads_writes_and_polling(self):
        cls = lambda command: call_metadata('Bash', {'command': command})['cls']
        self.assertEqual(cls('gh pr view 12 --comments'), 'explore')
        self.assertEqual(cls('gh pr create -t x'), 'work')
        self.assertEqual(cls('python workflow_client.py instructions --fleet f --agent a'), 'explore')
        self.assertEqual(cls('rg workflow_client report'), 'explore')
        self.assertEqual(cls('echo data > out.txt'), 'work')
        self.assertEqual(cls('ls 2>/dev/null'), 'explore')
        self.assertEqual(cls('gh pr checks 12'), 'wait')
        self.assertEqual(cls('until curl -s localhost:8000; do sleep 2; done'), 'wait')
        self.assertEqual(call_metadata('Task', {'prompt': 'x'})['cls'], 'work')

    def test_quoted_operators_and_common_flags_are_not_misread(self):
        import time as clock
        self.assertEqual(call_metadata('Bash', {'command': 'grep -n "a > b" file'})['cls'], 'explore')
        self.assertEqual(call_metadata('Bash', {'command': 'rg -n "fn .*->" src'})['cls'], 'explore')
        self.assertEqual(call_metadata('Bash', {'command': 'mkdir -p build/out'})['label'], 'mkdir -p build/out')
        started = clock.time()
        call_metadata('Bash', {'command': 'mysql ' * 20000})
        self.assertLess(clock.time() - started, 0.05)

    def test_codex_rollout_without_task_events_splits_turns_per_prompt(self):
        parser = TurnParser('s', 'codex')
        def prompt(text, at):
            return {'timestamp': at, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}
        for row in (prompt('<environment_context>x</environment_context>', '2026-10-27T10:00:00Z'), prompt('First task', '2026-10-27T10:00:01Z'),
                    prompt('Second task', '2026-10-27T10:05:00Z')):
            parser.feed(row)
        turns = sorted(parser.drain(), key=lambda t: t['startedAt'])
        self.assertEqual([t['status'] for t in turns], ['completed', 'open'])

    def test_codex_token_total_reset_counts_the_last_call(self):
        parser = TurnParser('s', 'codex')
        def tokens(total, last, at):
            return {'timestamp': at, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': total, 'output_tokens': 0}, 'last_token_usage': {'input_tokens': last, 'output_tokens': 0}}}}
        parser.feed({'timestamp': '2026-10-27T10:00:00Z', 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't'}})
        for row in (tokens(1000, 1000, '2026-10-27T10:00:01Z'), tokens(5000, 4000, '2026-10-27T10:00:02Z'), tokens(300, 300, '2026-10-27T10:00:03Z')):
            parser.feed(row)
        [turn] = parser.drain()
        self.assertEqual(turn['tokens']['input'], 5300)
        self.assertEqual(sum(turn['buckets'].values()), 5300)

    def test_claude_command_rows_and_split_messages_do_not_open_turns(self):
        parser = TurnParser('s', 'claude-code')
        rows = claude_rows()
        extra = [{'type': 'user', 'uuid': 'cmd', 'timestamp': '2026-10-27T10:00:30Z',
                  'message': {'role': 'user', 'content': '<local-command-stdout>ok</local-command-stdout>'}},
                 {'type': 'assistant', 'timestamp': '2026-10-27T10:00:31Z',
                  'message': {'id': 'm3', 'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 4}, 'content': []}}]
        for row in rows + extra:
            parser.feed(row)
        [turn] = parser.drain()
        self.assertEqual(turn['id'], 'u1')
        self.assertEqual(turn['tokens']['output'], 14)

    def feed_all(self, rows):
        parser = TurnParser('s', 'claude-code')
        turns = {}
        for row in rows:
            parser.feed(row)
            for turn in parser.drain():
                turns[turn['id']] = turn
        return list(turns.values())

    def prompt(self, uid, at, text='Work the ticket'):
        return {'type': 'user', 'uuid': uid, 'timestamp': stamp(at), 'message': {'role': 'user', 'content': text}}

    def reply(self, mid, at, blocks, stop='end_turn'):
        return {'type': 'assistant', 'timestamp': stamp(at), 'message': {'id': mid, 'stop_reason': stop, 'usage': {'output_tokens': 1}, 'content': blocks}}

    def test_a_message_split_across_rows_is_one_turn(self):
        # Claude Code writes thinking and text as separate rows of one message, each marked end_turn.
        turns = self.feed_all([self.prompt('u1', 1000),
                               self.reply('m1', 1001, [{'type': 'thinking', 'thinking': ''}]),
                               self.reply('m1', 1002, [{'type': 'text', 'text': 'The parser now handles split messages correctly.'}])])
        self.assertEqual([(t['id'], t['status']) for t in turns], [('u1', 'completed')])
        self.assertEqual(len(turns[0]['replies']), 1)

    def test_turns_without_stop_reasons_end_completed_unless_cut_off(self):
        rows = [self.prompt('u1', 1000), self.reply('m1', 1001, [{'type': 'text', 'text': 'Finished the change and the tests pass.'}], None),
                self.prompt('u2', 1100), self.reply('m2', 1101, [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'make'}}], None),
                self.prompt('u3', 1200)]
        status = {t['id']: t['status'] for t in self.feed_all(rows)}
        self.assertEqual((status['u1'], status['u2']), ('completed', 'aborted'))

    def test_bash_mode_and_interruptions(self):
        rows = [self.prompt('u1', 1000), self.reply('m1', 1001, [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'make'}}], 'tool_use'),
                self.prompt('x', 1002, '[Request interrupted by user for tool use]'),
                self.prompt('b1', 1003, '<bash-input>git status</bash-input>'),
                self.prompt('b2', 1004, '<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>')]
        self.assertEqual([(t['id'], t['status']) for t in self.feed_all(rows)], [('u1', 'aborted')])

    def test_follower_resumes_from_saved_state_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 's.jsonl'
            rows = claude_rows()
            path.write_text('\n'.join(json.dumps(r) for r in rows[:3]) + '\n', encoding='utf-8')
            first = TranscriptFollower(path, 's', 'claude-code')
            [turn] = first.read()
            saved = json.loads(json.dumps(first.state()))
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n'.join(json.dumps(r) for r in rows[3:]) + '\n')
            resumed = TranscriptFollower(path, 's', 'claude-code', saved)
            [after] = resumed.read()
            self.assertEqual(after['id'], turn['id'])
            self.assertEqual(after['status'], 'completed')
            self.assertTrue(after['calls'][0]['error'])
            self.assertEqual(after['tokens']['output'], 11)


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def call_rows(index, at, command='make build', error=False, prompt=False):
    """One Claude Code tool call and its result; `prompt` opens a new turn first."""
    rows = [{'type': 'user', 'uuid': 'u' + str(index), 'timestamp': stamp(at), 'message': {'role': 'user', 'content': 'Work the ticket'}}] if prompt else []
    rows.append({'type': 'assistant', 'timestamp': stamp(at), 'message': {'id': 'm' + str(index), 'stop_reason': 'tool_use',
                 'usage': {'input_tokens': 100, 'output_tokens': 10},
                 'content': [{'type': 'tool_use', 'id': 't' + str(index), 'name': 'Bash', 'input': {'command': command}}]}})
    rows.append({'type': 'user', 'timestamp': stamp(at + 1), 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't' + str(index), 'is_error': error, 'content': 'failed' if error else 'ok'}]}})
    return rows


class StorageTests(unittest.TestCase):
    """Calls live in their own table so long turns stay cheap to store, read and keep."""

    def setUp(self):
        from fleet_studio import Application
        self.temporary = tempfile.TemporaryDirectory(prefix='.test-unit-', dir=Path(__file__).resolve().parent)
        self.root = Path(self.temporary.name).resolve()
        agents = [{'name': 'Agent ' + str(i), 'directory': str(self.root), 'sessionId': str(uuid.uuid4()), 'enabled': True} for i in range(10)]
        (self.root / 'fleet.json').write_text(json.dumps({'version': 1, 'windowName': 'scale', 'codexPath': sys.executable, 'agents': agents}))
        self.app = Application(self.root)
        state = self.app.store.snapshot()
        for agent in state['fleets'][0]['agents']:
            agent['backend'] = 'claude-code'
        self.app.store.save(state)
        self.state = self.app.store.snapshot()
        self.fleet = self.state['fleets'][0]
        self.paths = {a['id']: self.root / (a['id'] + '.jsonl') for a in self.fleet['agents']}
        self.app.telemetry.source = lambda agent: self.paths.get(agent['id'])
        self.now = time.time() - 3600
        self.app.health.clock = lambda: self.now

    def tearDown(self):
        self.temporary.cleanup()

    def append(self, agent_id, rows):
        with self.paths[agent_id].open('a', encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(row) + '\n' for row in rows))

    def test_long_turn_keeps_every_call_and_health_sees_a_late_loop(self):
        agent = self.fleet['agents'][0]['id']
        rows = []
        for i in range(450):
            rows += call_rows(i, self.now - 900 + i, 'edit step ' + str(i), prompt=i == 0)
        for i in range(450, 455):
            rows += call_rows(i, self.now - 400 + i, 'pytest tests/test_a.py', error=True)
        self.append(agent, rows)
        self.app.telemetry.tick(self.state)
        [turn] = self.app.telemetry.turns(self.fleet['id'], agent)
        self.assertEqual(len(turn['calls']), 455)
        with closing(self.app.telemetry.connect()) as db:
            stored = json.loads(db.execute('SELECT data FROM turns').fetchone()['data'])
        self.assertEqual(len(stored['calls']), 20)
        self.app.health.tick(self.state, {'sessions': {}})
        self.assertEqual(self.app.health.get(self.fleet['id'], agent)['state'], 'looping')
        # A later tick reloads only the new calls of the open turn.
        self.append(agent, call_rows(455, self.now + 60, 'git status'))
        self.app.telemetry.tick(self.state)
        self.app.health.tick(self.state, {'sessions': {}})
        cached = next(t for _, t in self.app.health.turn_cache[self.fleet['id']].values())
        self.assertEqual([c['seq'] for c in cached['calls']], list(range(1, 457)))

    def test_work_past_the_stored_call_limit_is_not_burning(self):
        agent = self.fleet['agents'][0]['id']
        rows = []
        for i in range(30):
            rows += call_rows(i, self.now - 12000 + i * 400, 'edit f' + str(i), prompt=i == 0)
        for row in rows:
            if row['type'] == 'assistant':
                row['message']['usage']['input_tokens'] = 50000
                row['message']['content'][0].update(name='Edit', input={'file_path': '/repo/f' + row['message']['id'], 'new_string': 'x'})
        self.append(agent, rows)
        with patch('studio_telemetry.CALL_LIMIT', 5):
            self.app.telemetry.tick(self.state)
        self.app.health.tick(self.state, {'sessions': {}})
        health = self.app.health.get(self.fleet['id'], agent)
        self.assertNotEqual(health['state'], 'burning')
        self.assertIsNotNone(health['lastWorkAt'])

    def test_read_position_is_saved_at_most_every_30_seconds_and_replay_does_not_duplicate(self):
        agent = self.fleet['agents'][0]['id']
        self.append(agent, call_rows(0, self.now, prompt=True))
        self.app.telemetry.tick(self.state)
        self.append(agent, call_rows(1, self.now + 5))
        self.app.telemetry.tick(self.state)
        with closing(self.app.telemetry.connect()) as db:
            saved = json.loads(db.execute('SELECT data FROM telemetry_offsets').fetchone()['data'])
        self.assertLess(saved['offset'], self.paths[agent].stat().st_size)
        # A restart replays from the older position; keyed rows are overwritten, not added.
        from studio_telemetry import Telemetry
        restarted = Telemetry(self.app)
        restarted.source = self.app.telemetry.source
        restarted.tick(self.state)
        [turn] = restarted.turns(self.fleet['id'], agent)
        self.assertEqual((turn['callCount'], len(turn['calls']), turn['tokens']['input']), (2, 2, 200))

    def test_pruning_drops_old_call_detail_but_keeps_turn_summaries(self):
        agent = self.fleet['agents'][0]['id']
        self.append(agent, call_rows(0, self.now, prompt=True))
        self.app.telemetry.tick(self.state)
        with closing(self.app.telemetry.connect()) as db, db:
            db.execute("INSERT INTO telemetry_offsets VALUES(?, '{}')", (json.dumps([self.fleet['id'], 'gone', 'x']),))
            self.app.telemetry.prune(db, self.fleet['id'], self.now - 90 * 86400, now=self.now + 4 * 86400)
            self.app.health.prune(db, {**self.fleet, 'agents': []})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turn_calls').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turns').fetchone()[0], 1)
            followers = [json.loads(row[0])[1] for row in db.execute('SELECT follower FROM telemetry_offsets')]
        self.assertEqual(followers, [agent])

    def test_completed_receipts_are_trimmed(self):
        controls = self.app.controls
        controls.receipts = {'old': {'id': 'old', 'status': 'completed', 'createdAt': 10, 'targets': []},
                             'open': {'id': 'open', 'status': 'needs-attention', 'createdAt': 10, 'targets': []},
                             'new': {'id': 'new', 'status': 'completed', 'createdAt': time.time(), 'targets': []}}
        controls.prune(time.time() - 86400)
        self.assertEqual(sorted(controls.receipts), ['new', 'open'])

    def test_a_busy_fleet_tick_stays_fast(self):
        """Ten agents, each making a call every five seconds, for 200 ticks."""
        start = time.perf_counter()
        for tick in range(200):
            self.now += 5
            for index, agent in enumerate(self.paths):
                self.append(agent, call_rows(tick, self.now, 'edit f' + str(tick % 30), prompt=tick == 0))
            self.app.telemetry.tick(self.state)
            self.app.health.tick(self.state, {'sessions': {}})
        mean = (time.perf_counter() - start) / 200
        self.assertLess(mean, 0.2)
        with closing(self.app.telemetry.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM turn_calls').fetchone()[0], 2000)


if __name__ == '__main__':
    unittest.main()
