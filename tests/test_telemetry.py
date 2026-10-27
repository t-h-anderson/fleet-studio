import json
from pathlib import Path
import sys
import tempfile
import unittest

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
                     'content': [{'type': 'text', 'text': 'Running the tests.'},
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
                     'content': [{'type': 'text', 'text': 'Done.'}]}},
    ]


def codex_rows():
    return [
        {'timestamp': '2026-10-27T10:00:00Z', 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-1'}},
        {'timestamp': '2026-10-27T10:00:01Z', 'type': 'response_item',
         'payload': {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
                     'arguments': json.dumps({'command': ['bash', '-lc', 'python workflow_client.py instructions']})}},
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


if __name__ == '__main__':
    unittest.main()
