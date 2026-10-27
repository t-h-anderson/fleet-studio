"""Turn and tool-call metadata from native agent transcripts.

Studio records what each turn did, not what it saw: tool names, a short
command or file-path signature, an exit/error flag, token components and
hashes of assistant replies. Tool inputs beyond that signature, tool outputs,
file contents, reasoning and message text are never stored.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from studio_bindings import session_key

CLASSES = ('work', 'explore', 'admin', 'wait', 'other')
SIGNATURE_LIMIT = 160
CALL_LIMIT = 400
INITIAL_TAIL = 8_000_000
STUDIO_MARKERS = ('[Fleet Studio]', 'Automated Fleet Studio')

ADMIN_COMMAND = re.compile(
    r'\b(workflow_client|fleet_progress|fleet_event|fleet_context|fleet report)\b'
    r'|\b(glab|gh)\s+(issue|mr|pr)\s+(note|comment|edit|update|view|list)\b'
    r'|\b(glab|gh)\s+(api)\b.*\b(notes|comments|labels)\b')
WAIT_COMMAND = re.compile(r'(^|[;&|]\s*)(sleep|watch)\b|\b(gh|glab)\s+(run|ci)\s+(watch|status)\b')
EXPLORE_COMMAND = re.compile(
    r'^(ls|cat|head|tail|less|grep|rg|ag|find|fd|tree|wc|pwd|which|file|stat|du|df|env|printenv|echo)\b'
    r'|^sed\s+-n\b|^git\s+(status|log|diff|show|branch|blame|grep|ls-files|remote|rev-parse)\b')
WORK_TOOLS = {'Edit', 'MultiEdit', 'Write', 'NotebookEdit', 'apply_patch'}
EXPLORE_TOOLS = {'Read', 'Grep', 'Glob', 'LS', 'WebFetch', 'WebSearch', 'view_image'}
WAIT_TOOLS = {'BashOutput', 'Monitor', 'wait'}
SHELL_TOOLS = {'Bash', 'shell', 'exec_command', 'shell_command', 'local_shell', 'container.exec'}
EXIT_CODE = re.compile(r'(?:exit code|exited with code|exit_code)["\s:]*(-?\d+)', re.IGNORECASE)


def epoch(stamp):
    if isinstance(stamp, (int, float)):
        return float(stamp)
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def digest(text, size=16):
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:size]


def studio_prompt(text):
    return text.lstrip().startswith(STUDIO_MARKERS)


def shell_label(command):
    """First meaningful line of a shell command, without wrappers or directory changes."""
    if isinstance(command, list):
        words = [str(part) for part in command]
        # Codex wraps commands as ["bash", "-lc", "<script>"].
        if len(words) >= 3 and Path(words[0]).name in ('bash', 'sh', 'zsh', 'pwsh', 'powershell') and words[1] in ('-lc', '-c', '-Command'):
            command = words[2]
        else:
            command = ' '.join(words)
    if not isinstance(command, str):
        return ''
    lines = [line.strip() for line in command.strip().splitlines() if line.strip() and not line.strip().startswith('#')]
    text = ' '.join(lines[0].split()) if lines else ''
    # A leading "cd folder &&" says where, not what, so it is dropped from the signature.
    text = re.sub(r'^(cd\s+\S+\s*(&&|;)\s*)+', '', text)
    return text[:SIGNATURE_LIMIT]


def patch_files(text):
    if not isinstance(text, str):
        return []
    return re.findall(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', text, re.MULTILINE)[:5]


def classify(tool, label):
    if tool in WORK_TOOLS:
        return 'work'
    if tool in EXPLORE_TOOLS:
        return 'explore'
    if tool in WAIT_TOOLS:
        return 'wait'
    if tool in SHELL_TOOLS:
        if ADMIN_COMMAND.search(label):
            return 'admin'
        if WAIT_COMMAND.search(label):
            return 'wait'
        if EXPLORE_COMMAND.search(label):
            return 'explore'
        return 'work' if label else 'other'
    if tool in ('TodoWrite', 'update_plan', 'Task', 'Agent'):
        return 'other'
    return 'other'


def call_metadata(tool, arguments):
    """Reduce a tool call to its name, class and a short signature."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            # Codex custom tools (apply_patch) pass raw text.
            files = patch_files(arguments)
            arguments = {'files': files} if files else {}
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool in SHELL_TOOLS:
        label = shell_label(arguments.get('command') or arguments.get('cmd') or '')
    elif tool == 'apply_patch':
        files = arguments.get('files') or patch_files(arguments.get('input') or arguments.get('patch') or '')
        label = ', '.join(files)
    elif arguments.get('file_path') or arguments.get('notebook_path') or arguments.get('path'):
        label = str(arguments.get('file_path') or arguments.get('notebook_path') or arguments.get('path'))
    elif arguments.get('pattern'):
        label = str(arguments['pattern'])
    elif arguments.get('url'):
        label = str(arguments['url']).split('?')[0]
    elif arguments.get('query'):
        label = str(arguments['query'])
    else:
        label = ''
    label = ' '.join(label.split())[:SIGNATURE_LIMIT]
    return {'tool': str(tool)[:80], 'cls': classify(tool, label), 'label': label, 'sig': digest(str(tool) + '\0' + label)}


def output_failed(output):
    """An error flag from a tool result; the result text itself is not kept."""
    if isinstance(output, dict):
        metadata = output.get('metadata') if isinstance(output.get('metadata'), dict) else {}
        code = metadata.get('exit_code', output.get('exit_code'))
        if isinstance(code, int):
            return code != 0
        output = output.get('output', '')
    if not isinstance(output, str):
        return False
    stripped = output.lstrip()
    if stripped.startswith('{'):
        try:
            return output_failed(json.loads(stripped))
        except ValueError:
            pass
    match = EXIT_CODE.search(output[:400])
    return bool(match and match.group(1) != '0')


def new_turn(session, turn_id, at, from_studio=False):
    return {'id': turn_id, 'session': session, 'startedAt': at, 'endedAt': None, 'lastAt': at, 'status': 'open',
            'fromStudio': from_studio, 'tokens': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            'calls': [], 'callCount': 0, 'classes': {name: 0 for name in CLASSES}, 'errors': 0, 'replies': []}


class TurnParser:
    """Incremental state machine that turns transcript records into turn summaries."""

    def __init__(self, session, backend):
        self.session, self.backend = session, backend
        self.current = None
        self.changed = {}
        self.pending_calls = {}
        self.usage_by_message = {}
        self.codex_total = None

    def touch(self, turn, at):
        if at:
            turn['lastAt'] = max(turn['lastAt'] or at, at)
        self.changed[turn['id']] = turn

    def start(self, turn_id, at, from_studio=False):
        if self.current and self.current['status'] == 'open':
            # A new prompt while the previous turn was still open means it was superseded.
            self.finish('aborted', at)
        self.current = new_turn(self.session, turn_id, at, from_studio)
        self.pending_calls = {}
        self.touch(self.current, at)
        return self.current

    def finish(self, status, at):
        turn = self.current
        if not turn or turn['status'] != 'open':
            return
        turn['status'], turn['endedAt'] = status, at or turn['lastAt']
        self.touch(turn, at)

    def ensure(self, at):
        if not self.current or self.current['status'] != 'open':
            # Records seen before any prompt (e.g. after a tail seek) form a partial turn.
            self.start(digest(self.session + str(at)), at)
            self.current['partial'] = True
        return self.current

    def add_call(self, meta, call_id, at):
        turn = self.ensure(at)
        turn['callCount'] += 1
        turn['classes'][meta['cls']] += 1
        if len(turn['calls']) < CALL_LIMIT:
            turn['calls'].append({**meta, 'seq': turn['callCount'], 'at': at, 'error': False})
            if call_id:
                self.pending_calls[call_id] = turn['calls'][-1]
        self.touch(turn, at)

    def mark_result(self, call_id, failed, at):
        call = self.pending_calls.pop(call_id, None)
        if call is not None and failed and not call['error']:
            call['error'] = True
            self.current['errors'] += 1
        if self.current:
            self.touch(self.current, at)

    def add_reply(self, text, at):
        normalized = ' '.join(text.lower().split())
        if not normalized:
            return
        turn = self.ensure(at)
        turn['replies'] = (turn['replies'] + [digest(normalized)])[-10:]
        self.touch(turn, at)

    def feed(self, row):
        if not isinstance(row, dict):
            return
        if self.backend == 'claude-code':
            self.feed_claude(row)
        else:
            self.feed_codex(row)

    def feed_claude(self, row):
        kind = row.get('type')
        if kind not in ('user', 'assistant') or row.get('isSidechain') or row.get('isMeta'):
            return
        at = epoch(row.get('timestamp'))
        message = row.get('message') if isinstance(row.get('message'), dict) else {}
        content = message.get('content')
        blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}] if isinstance(content, str) else []
        if kind == 'user':
            results = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_result']
            for block in results:
                self.mark_result(block.get('tool_use_id'), bool(block.get('is_error')), at)
            texts = [b.get('text') for b in blocks if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)]
            if texts and not results:
                prompt = '\n'.join(texts).lstrip()
                self.start(row.get('uuid') or digest(self.session + str(at) + prompt), at, studio_prompt(prompt))
            return
        turn = self.ensure(at)
        usage = message.get('usage') if isinstance(message.get('usage'), dict) else {}
        mid = message.get('id') or row.get('requestId') or row.get('uuid')
        fields = (('input_tokens', 'input'), ('output_tokens', 'output'),
                  ('cache_read_input_tokens', 'cacheRead'), ('cache_creation_input_tokens', 'cacheWrite'))
        # Streaming repeats a message's usage; only increases are counted.
        old = self.usage_by_message.get(mid, {})
        new = {}
        for source, target in fields:
            value = usage.get(source)
            if type(value) is int and value >= 0:
                new[target] = max(value, old.get(target, 0))
                turn['tokens'][target] += new[target] - old.get(target, 0)
        if mid:
            self.usage_by_message[mid] = {**old, **new}
            if len(self.usage_by_message) > 2000:
                self.usage_by_message.pop(next(iter(self.usage_by_message)))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                if block.get('id') and any(c.get('callId') == block['id'] for c in turn['calls'][-20:]):
                    continue
                self.add_call(call_metadata(block.get('name') or 'tool', block.get('input')), block.get('id'), at)
                if block.get('id') and turn['calls']:
                    turn['calls'][-1]['callId'] = block['id']
            elif block.get('type') == 'text' and isinstance(block.get('text'), str):
                self.add_reply(block['text'], at)
        self.touch(turn, at)
        if message.get('stop_reason') == 'end_turn':
            self.finish('completed', at)

    def feed_codex(self, row):
        at = epoch(row.get('timestamp'))
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        kind, item = row.get('type'), payload.get('type')
        if kind == 'event_msg':
            if item == 'task_started':
                self.start(payload.get('turn_id') or digest(self.session + str(at)), at)
            elif item == 'task_complete':
                self.finish('completed', at)
            elif item == 'turn_aborted':
                self.finish('aborted', at)
            elif item == 'token_count':
                self.codex_tokens(payload.get('info'), at)
            elif item == 'user_message' and isinstance(payload.get('message'), str):
                turn = self.ensure(at)
                turn['fromStudio'] = turn['fromStudio'] or studio_prompt(payload['message'])
            return
        if kind != 'response_item':
            return
        if item in ('function_call', 'custom_tool_call'):
            arguments = payload.get('arguments') if item == 'function_call' else payload.get('input')
            self.add_call(call_metadata(payload.get('name') or 'tool', arguments), payload.get('call_id'), at)
        elif item == 'local_shell_call':
            action = payload.get('action') if isinstance(payload.get('action'), dict) else {}
            self.add_call(call_metadata('local_shell', {'command': action.get('command')}), payload.get('call_id'), at)
        elif item in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
            self.mark_result(payload.get('call_id'), output_failed(payload.get('output')), at)
        elif item == 'message' and payload.get('role') == 'assistant':
            parts = payload.get('content') if isinstance(payload.get('content'), list) else []
            text = '\n'.join(c.get('text', '') for c in parts if isinstance(c, dict) and isinstance(c.get('text'), str))
            self.add_reply(text, at)
        elif item == 'message' and payload.get('role') == 'user' and (not self.current or self.current['status'] != 'open'):
            parts = payload.get('content') if isinstance(payload.get('content'), list) else []
            text = '\n'.join(c.get('text', '') for c in parts if isinstance(c, dict) and isinstance(c.get('text'), str)).lstrip()
            # Older rollouts have no task events; a user prompt opens the turn.
            if text and not text.startswith('<environment_context>'):
                self.start(digest(self.session + str(at) + text[:200]), at, studio_prompt(text))

    def codex_tokens(self, info, at):
        info = info if isinstance(info, dict) else {}
        total = info.get('total_token_usage') if isinstance(info.get('total_token_usage'), dict) else None
        if not total:
            return
        values = {
            'input': max(0, int(total.get('input_tokens') or 0) - int(total.get('cached_input_tokens') or 0)),
            'cacheRead': int(total.get('cached_input_tokens') or 0),
            'output': int(total.get('output_tokens') or 0),
        }
        previous, self.codex_total = self.codex_total, values
        if previous is None:
            # The first total after a tail seek includes earlier turns; count only the last call.
            last = info.get('last_token_usage') if isinstance(info.get('last_token_usage'), dict) else {}
            delta = {
                'input': max(0, int(last.get('input_tokens') or 0) - int(last.get('cached_input_tokens') or 0)),
                'cacheRead': int(last.get('cached_input_tokens') or 0),
                'output': int(last.get('output_tokens') or 0),
            }
        else:
            delta = {key: max(0, values[key] - previous[key]) for key in values}
        if not any(delta.values()):
            return
        turn = self.ensure(at)
        for key, value in delta.items():
            turn['tokens'][key] += value
        self.touch(turn, at)

    def drain(self):
        changed = [deepcopy(turn) for turn in self.changed.values()]
        self.changed = {}
        for turn in changed:
            for call in turn['calls']:
                call.pop('callId', None)
        return changed


def total_tokens(turn):
    return sum(turn['tokens'].values())


class TranscriptFollower:
    """Follows one transcript file, reading only appended complete records."""

    def __init__(self, path, session, backend):
        self.path, self.offset = Path(path), None
        self.parser = TurnParser(session, backend)

    def read(self):
        size = self.path.stat().st_size
        if self.offset is None or size < self.offset:
            self.offset = max(0, size - INITIAL_TAIL)
            self.parser = TurnParser(self.parser.session, self.parser.backend)
            skip_first = self.offset > 0
        else:
            skip_first = False
        if size == self.offset:
            return []
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            data = stream.read(size - self.offset)
        complete = data.rfind(b'\n') + 1
        lines = data[:complete].splitlines()
        self.offset += complete
        if skip_first and lines:
            lines = lines[1:]
        for line in lines:
            try:
                self.parser.feed(json.loads(line))
            except (ValueError, UnicodeError, TypeError, AttributeError, KeyError):
                continue
        return self.parser.drain()


class Telemetry:
    """Stores turn summaries per agent in the progress database."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.followers = {}
        self.errors = {}
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS turns (
                    session TEXT NOT NULL, id TEXT NOT NULL, fleet TEXT NOT NULL, agent TEXT NOT NULL,
                    started REAL, last REAL, status TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(session, id));
                CREATE INDEX IF NOT EXISTS turns_agent ON turns(fleet, agent, last);
            ''')

    def connect(self):
        db = sqlite3.connect(self.app.root / '.runtime' / 'progress.sqlite', timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def source(self, agent):
        """Transcript path for a local agent; remote transcripts stay on their node."""
        if agent.get('hostId', 'local') != 'local' or not agent.get('sessionId'):
            return None
        backend = self.app.runtimes.for_agent(agent)
        locate = getattr(backend, 'transcript_path', None)
        return locate(agent['sessionId']) if locate else None

    def tick(self, state):
        seen = set()
        for fleet in state['fleets']:
            for agent in fleet['agents']:
                key = (fleet['id'], agent['id'], session_key(agent))
                seen.add(key)
                try:
                    path = self.source(agent)
                    if not path:
                        continue
                    follower = self.followers.get(key)
                    if follower is None or follower.path != Path(path):
                        follower = self.followers[key] = TranscriptFollower(path, key[2], agent.get('backend', 'codex'))
                    self.store(fleet['id'], agent['id'], follower.read())
                    self.errors.pop(key, None)
                except (OSError, sqlite3.Error) as exc:
                    self.errors[key] = str(exc)
        for key in set(self.followers) - seen:
            self.followers.pop(key)

    def store(self, fleet_id, agent_id, turns):
        if not turns:
            return
        with self.lock, closing(self.connect()) as db, db:
            for turn in turns:
                db.execute('INSERT OR REPLACE INTO turns VALUES(?,?,?,?,?,?,?,?)',
                           (turn['session'], turn['id'], fleet_id, agent_id, turn['startedAt'], turn['lastAt'],
                            turn['status'], json.dumps(turn, separators=(',', ':'))))

    def turns(self, fleet_id, agent_id=None, since=0, limit=200):
        query = 'SELECT agent, data FROM turns WHERE fleet=? AND last>=?'
        values = [fleet_id, since]
        if agent_id:
            query += ' AND agent=?'
            values.append(agent_id)
        query += ' ORDER BY last DESC LIMIT ?'
        values.append(limit)
        with self.lock, closing(self.connect()) as db:
            rows = db.execute(query, values).fetchall()
        return [{**json.loads(row['data']), 'agentId': row['agent']} for row in rows]

    def prune(self, db, fleet_id, cutoff):
        db.execute('DELETE FROM turns WHERE fleet=? AND last<?', (fleet_id, cutoff))
