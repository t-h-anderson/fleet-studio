"""Synthetic native graphs; no real child threads or model requests."""
from contextlib import closing
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_studio as fixtures
from studio_subagents import observe_children, hold_reason
from studio_health import assess, DEFAULTS
from studio_telemetry import classify


class SubagentTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown

    def graph(self):
        self.now = time.time()
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,agent_nickname TEXT,agent_path TEXT,model TEXT,tokens_used INT)')
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT)')
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,started_at INT,completed_at INT,rollout_ordinal INT)')
            db.execute('CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_type TEXT,item_json TEXT)')

    def child(self, sid, parent='parent', status='inProgress', edge='open', tokens=20):
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (sid, 'Reviewer', '/root/' + sid, 'model', tokens))
            db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', (parent, sid, edge))
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)', (sid, sid + '-turn', status, self.now - 60, None if status == 'inProgress' else self.now - 10, 1))

    def observe(self, parent='parent'):
        report = observe_children(self.root, [parent])[parent]
        report['observedAt'] = self.now
        return report

    def test_nested_children_and_tokens_are_counted_once(self):
        self.graph()
        self.child('writer', tokens=100)
        self.child('reviewer', 'writer', status='completed', tokens=30)
        self.child('closed-review', status='completed', edge='closed', tokens=10)
        report = self.observe()
        self.assertTrue(report['known'])
        self.assertEqual(report['active'], 1)
        self.assertEqual(report['uncertain'], 0)
        self.assertEqual(report['tokensUsed'], 140)
        self.assertEqual(next(c for c in report['children'] if c['sessionId'] == 'reviewer')['depth'], 2)
        self.assertEqual(next(c for c in report['children'] if c['sessionId'] == 'reviewer')['parentSessionId'], 'writer')
        self.assertEqual(next(c for c in report['children'] if c['sessionId'] == 'closed-review')['state'], 'closed')

    def test_finished_child_with_open_tool_remains_uncertain(self):
        self.graph()
        self.child('matlab', status='completed')
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.execute('INSERT INTO thread_items VALUES(?,?,?,?)', ('matlab', 'matlab-turn', 'mcpToolCall', json.dumps({'status': 'inProgress', 'output': 'PRIVATE'})))
        report = self.observe()
        self.assertEqual(report['uncertain'], 1)
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertTrue(hold_reason({'subagents': report}))

    def test_completed_tools_do_not_create_an_artificial_size_limit(self):
        self.graph()
        self.child('tests', status='completed')
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.executemany('INSERT INTO thread_items VALUES(?,?,?,?)', [('tests', 'tests-turn', 'commandExecution', '{"status":"completed"}')] * 150)
        self.assertEqual(self.observe()['uncertain'], 0)

    def test_failures_and_missing_metadata_hold_recovery(self):
        self.graph()
        self.child('failed', status='failed')
        report = self.observe()
        self.assertEqual(report['uncertain'], 1)
        self.assertTrue(hold_reason({'subagents': report}))
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.execute('DROP TABLE thread_turns')
        self.assertFalse(self.observe()['known'])

    def test_reused_child_latest_turn_is_working(self):
        self.graph()
        self.child('worker', status='completed')
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db, db:
            db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)', ('worker', 'next-turn', 'inProgress', self.now, None, 2))
        self.assertEqual(self.observe()['active'], 1)

    def test_empty_graph_is_known_but_missing_schema_is_not(self):
        self.graph()
        self.assertTrue(self.observe()['known'])
        self.assertEqual(hold_reason({'subagents': self.observe()}), '')
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('DROP TABLE thread_spawn_edges')
        report = self.observe()
        self.assertFalse(report['known'])
        self.assertIs(report['supported'], False)
        # A Codex without subagent metadata keeps ordinary supervision; only risky actions need known children.
        self.assertEqual(hold_reason({'subagents': report}), '')
        self.assertIn('unavailable on this Codex version', hold_reason({'subagents': report}, require_known=True))

    def test_unsupported_codex_keeps_ordinary_health_detection(self):
        self.graph()
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('DROP TABLE thread_spawn_edges')
        health = assess({'status': 'idle', 'subagents': self.observe()}, [], self.now, DEFAULTS, open_work=True)
        self.assertEqual(health['state'], 'stalled')

    def test_no_codex_state_database_is_unsupported_not_uncertain(self):
        # No graph() here: this Codex home has no state database to read at all.
        self.now = time.time()
        report = self.observe()
        self.assertIs(report['supported'], False)
        self.assertEqual(hold_reason({'subagents': report}), '')
        self.assertTrue(hold_reason({'subagents': report}, require_known=True))

    def test_missing_history_database_follows_the_edges_table(self):
        self.graph()
        (self.root / 'thread_history_1.sqlite').unlink()
        # The edges table exists, so children may be there: fail closed.
        self.assertFalse(self.observe()['known'])
        self.assertTrue(hold_reason({'subagents': self.observe()}))
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('DROP TABLE thread_spawn_edges')
        # No edges table at all: this Codex records no subagents, history or not.
        self.assertIs(self.observe()['supported'], False)
        self.assertEqual(hold_reason({'subagents': self.observe()}), '')

    def test_an_unreadable_codex_home_is_uncertain_not_a_crash(self):
        self.graph()
        with patch('pathlib.Path.is_file', side_effect=PermissionError('denied')):
            report = observe_children(self.root, ['parent'])['parent']
        self.assertFalse(report['known'])
        self.assertTrue(hold_reason({'subagents': report}))

    def test_a_transient_read_failure_still_holds(self):
        self.graph()
        with patch('studio_runtime.connect', side_effect=sqlite3.OperationalError('database is locked')):
            report = observe_children(self.root, ['parent'])['parent']
        self.assertFalse(report['known'])
        self.assertNotIn('supported', report)
        self.assertTrue(hold_reason({'subagents': report}))

    def test_cycles_and_truncation_fail_closed(self):
        self.graph()
        self.child('a')
        self.child('b', 'a')
        with patch('studio_subagents.MAX_CHILDREN', 1):
            self.assertFalse(self.observe()['known'])
        with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('b', 'parent', 'open'))
        self.assertFalse(self.observe()['known'])

    def test_active_child_suppresses_parent_stall_and_burn(self):
        self.graph()
        self.child('worker')
        health = assess({'status': 'working', 'subagents': self.observe()}, [], self.now, DEFAULTS, open_work=True)
        self.assertEqual(health['state'], 'delegating')
        self.assertEqual(health['signals'][0]['kind'], 'subagents')

    def test_uncertain_or_stale_child_state_is_waiting(self):
        self.graph()
        report = self.observe()
        report['observedAt'] = self.now - 21
        self.assertTrue(hold_reason({'subagents': report}, self.now))
        self.assertEqual(assess({'status': 'working', 'subagents': report}, [], self.now, DEFAULTS)['state'], 'waiting')

    def test_completion_returns_parent_to_normal_health(self):
        self.graph()
        self.child('worker', status='completed')
        health = assess({'status': 'idle', 'subagents': self.observe()}, [], self.now, DEFAULTS, open_work=True)
        self.assertEqual(health['state'], 'stalled')

    def test_missing_child_tokens_are_unknown_not_zero(self):
        self.graph()
        self.child('worker', tokens=None)
        self.assertIsNone(self.observe()['tokensUsed'])

    def test_coordination_is_not_classified_as_implementation(self):
        self.assertEqual(classify('collaboration.wait_agent', ''), 'wait')
        self.assertEqual(classify('collaboration.spawn_agent', ''), 'other')
        self.assertTrue(hold_reason({}, require_known=True))
