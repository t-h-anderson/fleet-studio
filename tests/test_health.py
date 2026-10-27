from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_health import DEFAULTS, assess, clean_health_settings, repeated_cycle, turn_waste
from studio_telemetry import call_metadata

NOW = 1_800_000_000.0


def call(tool, label_arguments, at, error=False):
    meta = call_metadata(tool, label_arguments)
    return {**meta, 'at': at, 'error': error, 'seq': 0}


def turn(calls, at, tokens=1000, status='completed', from_studio=False, replies=()):
    classes = {name: 0 for name in ('work', 'explore', 'admin', 'wait', 'other')}
    for item in calls:
        classes[item['cls']] += 1
    return {'id': str(at), 'startedAt': at - 60, 'lastAt': at, 'status': status, 'fromStudio': from_studio,
            'tokens': {'input': tokens, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}, 'calls': calls,
            'callCount': len(calls), 'classes': classes, 'errors': sum(c['error'] for c in calls),
            'replies': list(replies)}


class HealthTests(unittest.TestCase):
    def test_settings_are_validated(self):
        self.assertEqual(clean_health_settings(), DEFAULTS)
        with self.assertRaises(ValueError):
            clean_health_settings({'loopRepeats': 1})
        with self.assertRaises(ValueError):
            clean_health_settings({'unknown': 1})

    def test_repeated_failing_command_is_a_loop(self):
        calls = [call('Bash', {'command': 'pytest tests/x.py'}, NOW - 300 + i, error=True) for i in range(4)]
        health = assess({'status': 'working'}, [turn(calls, NOW - 200)], NOW, DEFAULTS)
        self.assertEqual(health['state'], 'looping')
        self.assertIn('pytest tests/x.py', health['signals'][0]['message'])

    def test_cycle_detection(self):
        self.assertEqual(repeated_cycle(list('xabababab'), 4), 2)
        self.assertEqual(repeated_cycle(list('abcabcab'), 3), 0)
        self.assertEqual(repeated_cycle(list('aaaa'), 4), 1)

    def test_editing_different_files_is_productive(self):
        calls = [call('Edit', {'file_path': '/repo/f' + str(i)}, NOW - 100 + i) for i in range(10)]
        health = assess({'status': 'working'}, [turn(calls, NOW - 50)], NOW, DEFAULTS)
        self.assertEqual(health['state'], 'productive')
        self.assertEqual(health['classes24h']['work'], 10)

    def test_admin_heavy(self):
        calls = [call('Bash', {'command': 'python fleet_progress.py --kind progress-updated --n ' + str(i)}, NOW - 500 + i) for i in range(6)]
        calls += [call('Edit', {'file_path': '/a' + str(i)}, NOW - 400 + i) for i in range(4)]
        health = assess({'status': 'idle'}, [turn(calls, NOW - 300)], NOW, DEFAULTS)
        self.assertEqual(health['state'], 'admin-heavy')

    def test_idle_with_work_is_stalled_but_blocked_is_waiting(self):
        old = [turn([], NOW - 3600)]
        self.assertEqual(assess({'status': 'idle'}, old, NOW, DEFAULTS, open_work=True)['state'], 'stalled')
        self.assertEqual(assess({'status': 'idle'}, old, NOW, DEFAULTS, open_work=False)['state'], 'idle')
        blocked = assess({'status': 'idle'}, old, NOW, DEFAULTS, open_work=True, blocked={'reason': 'Needs API key'})
        self.assertEqual(blocked['state'], 'waiting')
        self.assertEqual(blocked['signals'][0]['message'], 'Needs API key')

    def test_silent_working_turn_is_stalled(self):
        health = assess({'status': 'working'}, [turn([], NOW - 1200, status='open')], NOW, DEFAULTS)
        self.assertEqual(health['state'], 'stalled')
        self.assertEqual(health['signals'][0]['kind'], 'silent-turn')

    def test_burning_without_work(self):
        calls = [call('Read', {'file_path': '/f' + str(i)}, NOW - 100 + i) for i in range(3)]
        health = assess({'status': 'working'}, [turn(calls, NOW - 60, tokens=500000)], NOW, DEFAULTS)
        self.assertEqual(health['state'], 'burning')

    def test_reprompts_without_work_are_a_nudge_loop(self):
        turns = [turn([call('Read', {'file_path': '/f'}, NOW - i * 100)], NOW - i * 100, from_studio=True) for i in range(1, 4)]
        health = assess({'status': 'idle'}, turns, NOW, DEFAULTS)
        self.assertEqual(health['signals'][0]['kind'], 'nudge-loop')
        self.assertEqual(health['waste24h']['nudge'], 3000)

    def test_turn_waste_split(self):
        calls = [call('Bash', {'command': 'python workflow_client.py instructions'}, NOW)]
        calls += [call('Edit', {'file_path': '/a'}, NOW)]
        self.assertEqual(turn_waste(turn(calls, NOW), DEFAULTS)['admin'], 500)
        self.assertEqual(turn_waste(turn(calls, NOW, status='aborted'), DEFAULTS)['aborted'], 1000)


if __name__ == '__main__':
    unittest.main()
