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
import time

from studio_bindings import session_key

CLASSES = ('work', 'explore', 'admin', 'wait', 'other')
SIGNATURE_LIMIT = 160
# Calls are stored in their own table; a turn row and the parser keep only the latest few.
CALL_LIMIT = 5000
TAIL_CALLS = 20
PENDING_LIMIT = 500
OFFSET_SAVE_SECONDS = 30
CALL_RETENTION_DAYS = 3
INITIAL_TAIL = 8_000_000
STUDIO_MARKERS = ('[Fleet Studio]', '[Fleet Studio · check]', 'Automated Fleet Studio')
CHECK_MARKER = '[Fleet Studio · check]'

# Only writes to the tracker or Studio count as admin; reading a ticket is exploring.
ADMIN_COMMAND = re.compile(
    r'\b(fleet_progress|fleet_event|fleet_context)(\.py)?\b'
    r'|\bworkflow_client(\.py)?[\'"]?\s+(report|acknowledge)\b'
    r'|\b(glab|gh)\s+(issue|mr|pr)\s+(note|comment|edit|update|close|reopen)\b'
    r'|\b(glab|gh)\s+api\b.*(-X|--method)\s*(POST|PATCH|PUT)\b.*\b(notes|comments|labels)\b')
WAIT_COMMAND = re.compile(
    r'(^|[;&|]\s*)(sleep|watch)\b|\b(while|until)\b.*\bsleep\b'
    r'|\b(gh|glab)\s+(run|ci)\s+(watch|status|view|list)\b|\bgh\s+pr\s+checks\b')
EXPLORE_COMMAND = re.compile(
    r'^(ls|cat|head|tail|less|grep|rg|ag|find|fd|tree|wc|pwd|which|file|stat|du|df|env|printenv|echo|curl|wget)\b'
    r'|^sed\s+-n\b|^git\s+(status|log|diff|show|branch|blame|grep|ls-files|remote|rev-parse)\b'
    r'|^kubectl\s+(get|describe|logs)\b|^(gh|glab)\s+(issue|pr|mr|repo)\s+(view|list|diff|status)\b'
    r'|^(gh|glab)\s+api\b(?!.*(-X|--method)\s*(POST|PATCH|PUT|DELETE))'
    r'|\bworkflow_client(\.py)?[\'"]?\s+instructions\b')
# Redirecting into a file is writing, whatever the command.
WRITE_REDIRECT = re.compile(r'(^|[^0-9&>])>{1,2}\s*(?!&|/dev/null)\S|\btee\b')
WORK_TOOLS = {'Edit', 'MultiEdit', 'Write', 'NotebookEdit', 'apply_patch', 'Task', 'Agent'}
EXPLORE_TOOLS = {'Read', 'Grep', 'Glob', 'LS', 'WebFetch', 'WebSearch', 'view_image'}
WAIT_TOOLS = {'BashOutput', 'Monitor', 'wait', 'write_stdin'}
SHELL_TOOLS = {'Bash', 'shell', 'exec_command', 'shell_command', 'local_shell', 'container.exec'}
# Planning calls repeat by design and never indicate a loop.
LOOP_EXEMPT = {'TodoWrite', 'update_plan'}
SECRET_PATTERNS = (
    (re.compile(r'^(?:\w+=\S+\s+)+'), ''),
    (re.compile(r'\b(export\s+\w+)=\S+'), r'\1=…'),
    (re.compile(r'(--?(?:password|passwd|token|secret|key|api-key|apikey|auth)[=\s]+)\S+', re.IGNORECASE), r'\1…'),
    # Bounded and anchored to the mysql word so long commands cannot backtrack quadratically.
    (re.compile(r'(\b(?:mysql|mariadb|mysqldump)\b[^|;&]{0,200}?\s-p)(?=\S)\S+'), r'\1…'),
    (re.compile(r'(authorization:?\s*)(bearer|basic|token)?\s*[^\s\'"]+', re.IGNORECASE), r'\1…'),
    (re.compile(r'\bbearer\s+[^\s\'"]+', re.IGNORECASE), 'Bearer …'),
    (re.compile(r'\b(ghp|gho|ghs|github_pat|glpat|xox[abp]|sk|AKIA)[-_][\w-]{8,}|\bAKIA[0-9A-Z]{16}\b'), '…'),
    (re.compile(r'(https?://[^\s?\'"]+)\?[^\s\'"]*'), r'\1?…'),
    (re.compile(r'(\s(?:-m|--message|--body|-b|--title|--notes?|-F)\s+)("[^"]*"|\'[^\']*\'|\S+)'), r'\1"…"'),
    (re.compile(r'://[^/\s:@]+:[^/\s@]+@'), '://…@'),
)
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


def redact(text):
    """Remove values that may be credentials or content before a label is stored."""
    text = text[:SIGNATURE_LIMIT * 4]
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')


def unquoted(text):
    """The command with quoted arguments blanked, so `grep "a > b"` is not read as a redirect."""
    return QUOTED.sub('""', text)


def shell_script(command):
    """The script a shell call runs, without wrappers or leading directory changes."""
    if isinstance(command, list):
        words = [str(part) for part in command]
        # Codex wraps commands as ["bash", "-lc", "<script>"].
        if len(words) >= 3 and Path(words[0]).name in ('bash', 'sh', 'zsh', 'pwsh', 'powershell') and words[1] in ('-lc', '-c', '-Command'):
            command = words[2]
        else:
            command = ' '.join(words)
    if not isinstance(command, str):
        return ''
    # A leading "cd folder &&" says where, not what.
    return re.sub(r'^\s*(cd\s+\S+\s*(&&|;)\s*)+', '', command.strip())


def shell_label(command):
    """First meaningful line of a shell command, redacted, for display."""
    script = shell_script(command)
    lines = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith('#')]
    text = ' '.join(lines[0].split()) if lines else ''
    text = re.sub(r'^(cd\s+\S+\s*(&&|;)\s*)+', '', text)
    return redact(text)[:SIGNATURE_LIMIT]


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
        if not label:
            return 'other'
        if re.match(r'(apply_patch|cat\s*>|tee)\b', label) or WRITE_REDIRECT.search(unquoted(label)):
            return 'work'
        if ADMIN_COMMAND.search(label) and not re.match(r'(rg|grep|ag|cat|less|head|tail)\b', label):
            return 'admin'
        if WAIT_COMMAND.search(label):
            return 'wait'
        if EXPLORE_COMMAND.search(label):
            return 'explore'
        return 'work'
    return 'other'


def fingerprint(value):
    """Stable hash input for arguments, bounded so huge payloads stay cheap."""
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= 20000 else text[:10000] + digest(text, 32)


def call_metadata(tool, arguments):
    """Reduce a tool call to its name, class, a redacted label and a signature of its full arguments.

    The signature distinguishes calls that differ anywhere in their arguments, so
    editing a file twice or reading two ranges are different steps; only the
    hash is kept, never the arguments themselves.
    """
    raw = arguments
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            # Codex custom tools (apply_patch) pass raw text.
            files = patch_files(arguments)
            arguments = {'files': files, 'input': raw} if files else {'input': raw}
    arguments = arguments if isinstance(arguments, dict) else {}
    tool = str(tool)
    if tool in SHELL_TOOLS:
        script = shell_script(arguments.get('command') or arguments.get('cmd') or '')
        label, basis = shell_label(script), script
    elif tool == 'apply_patch':
        files = arguments.get('files') or patch_files(arguments.get('input') or arguments.get('patch') or '')
        label, basis = ', '.join(files), fingerprint(arguments)
    elif tool in ('Grep', 'Glob', 'WebSearch') or arguments.get('pattern') or arguments.get('query'):
        # Search text can be sensitive, so it is shown only as a short hash.
        text = str(arguments.get('pattern') or arguments.get('query') or '')
        where = str(arguments.get('path') or arguments.get('glob') or '')
        label = ('#' + digest(text, 8) if text else '') + (' in ' + where if where else '')
        basis = fingerprint(arguments)
    elif arguments.get('file_path') or arguments.get('notebook_path') or arguments.get('path'):
        label = str(arguments.get('file_path') or arguments.get('notebook_path') or arguments.get('path'))
        basis = fingerprint(arguments)
    elif arguments.get('url'):
        label, basis = str(arguments['url']).split('?')[0], fingerprint(arguments)
    else:
        label, basis = '', fingerprint(arguments)
    label = redact(' '.join(label.split()))[:SIGNATURE_LIMIT]
    return {'tool': tool[:80], 'cls': classify(tool, label), 'label': label, 'sig': digest(tool + '\0' + basis),
            **({'exempt': True} if tool in LOOP_EXEMPT else {})}


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


BUCKET_SECONDS = 300
REPLY_MINIMUM = 20
# Claude Code records these as user rows, but they are not prompts.
NON_PROMPTS = ('<command-name>', '<command-message>', '<local-command-stdout>', '<local-command-stderr>',
               '<user-memory-input>', 'Caveat: The messages below were generated')


def new_turn(session, turn_id, at, from_studio=False):
    return {'id': turn_id, 'session': session, 'startedAt': at, 'endedAt': None, 'lastAt': at, 'status': 'open',
            'fromStudio': from_studio, 'tokens': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            'buckets': {}, 'calls': [], 'callCount': 0, 'classes': {name: 0 for name in CLASSES}, 'errors': 0,
            'replies': []}


def copy_turn(turn):
    """Copy of a turn; its values are flat apart from these containers, so deepcopy is not needed."""
    return {**turn, 'tokens': dict(turn['tokens']), 'buckets': dict(turn['buckets']), 'classes': dict(turn['classes']),
            'calls': [dict(call) for call in turn['calls']], 'replies': list(turn['replies'])}


def add_tokens(turn, delta, at):
    """Add token counts to a turn and to its five-minute bucket, so long turns are spread over time."""
    for key, value in delta.items():
        turn['tokens'][key] += value
    fresh = sum(value for key, value in delta.items() if key != 'cacheRead')
    if fresh and at:
        bucket = str(int(at // BUCKET_SECONDS * BUCKET_SECONDS))
        turn['buckets'][bucket] = turn['buckets'].get(bucket, 0) + fresh
        if len(turn['buckets']) > 600:
            turn['buckets'].pop(min(turn['buckets'], key=int))


class TurnParser:
    """Incremental state machine that turns transcript records into turn summaries."""

    def __init__(self, session, backend):
        self.session, self.backend = session, backend
        self.current = None
        self.changed = {}
        self.pending_calls = {}
        self.usage_by_message = {}
        self.message_turns = {}
        self.codex_total = None
        self.task_events = False
        self.dirty = {}

    def touch(self, turn, at):
        if at:
            turn['lastAt'] = max(turn['lastAt'] or at, at)
        self.changed[turn['id']] = turn

    def start(self, turn_id, at, from_studio=False, previous='aborted'):
        if self.current and self.current['status'] == 'open':
            # A new prompt while the previous turn was still open usually means it was superseded.
            self.finish(previous, at)
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
        if turn['callCount'] <= CALL_LIMIT:
            call = {**meta, 'seq': turn['callCount'], 'at': at, 'error': False, 'open': True}
            turn['calls'] = (turn['calls'] + [call])[-TAIL_CALLS:]
            turn['tail'] = True
            self.dirty.setdefault(turn['id'], {})[call['seq']] = call
            if call_id:
                self.pending_calls[call_id] = call
                if len(self.pending_calls) > PENDING_LIMIT:
                    self.pending_calls.pop(next(iter(self.pending_calls)))
            self.touch(turn, at)
            return call
        self.touch(turn, at)
        return None

    def mark_result(self, call_id, failed, at):
        call = self.pending_calls.pop(call_id, None)
        if call is not None:
            call['open'] = False
        if call is not None and failed and not call['error']:
            call['error'] = True
            self.current['errors'] += 1
        if call is not None and self.current:
            self.dirty.setdefault(self.current['id'], {})[call['seq']] = call
        if self.current:
            self.touch(self.current, at)

    def add_reply(self, text, at):
        normalized = ' '.join(text.lower().split())
        # Short stock replies ("Done.") repeat legitimately and say nothing about loops.
        if len(normalized) < REPLY_MINIMUM:
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
                if prompt.startswith(NON_PROMPTS) or row.get('isCompactSummary'):
                    return
                self.start(row.get('uuid') or digest(self.session + str(at) + prompt), at, studio_prompt(prompt))
                if prompt.startswith(CHECK_MARKER):
                    self.current['studioCheck'] = True
            return
        usage = message.get('usage') if isinstance(message.get('usage'), dict) else {}
        mid = message.get('id') or row.get('requestId') or row.get('uuid')
        # Later rows of a message that already ended its turn belong to that turn, not a new one.
        turn = self.message_turns.get(mid) if mid else None
        if turn == 'ended':
            return  # A late row of a message whose turn ended before a restart.
        if turn is None or turn is not self.current and self.current and self.current['status'] == 'open':
            turn = self.ensure(at)
        if mid:
            self.message_turns[mid] = turn
            if len(self.message_turns) > 50:
                self.message_turns.pop(next(iter(self.message_turns)))
        fields = (('input_tokens', 'input'), ('output_tokens', 'output'),
                  ('cache_read_input_tokens', 'cacheRead'), ('cache_creation_input_tokens', 'cacheWrite'))
        # Streaming repeats a message's usage; only increases are counted.
        old = self.usage_by_message.get(mid, {})
        new, delta = {}, {}
        for source, target in fields:
            value = usage.get(source)
            if type(value) is int and value >= 0:
                new[target] = max(value, old.get(target, 0))
                delta[target] = new[target] - old.get(target, 0)
        add_tokens(turn, delta, at)
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
                call = self.add_call(call_metadata(block.get('name') or 'tool', block.get('input')), block.get('id'), at)
                if block.get('id') and call:
                    call['callId'] = block['id']
            elif block.get('type') == 'text' and isinstance(block.get('text'), str) and turn is self.current:
                self.add_reply(block['text'], at)
        self.touch(turn, at)
        if message.get('stop_reason') == 'end_turn' and turn is self.current:
            self.finish('completed', at)

    def feed_codex(self, row):
        at = epoch(row.get('timestamp'))
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        kind, item = row.get('type'), payload.get('type')
        if kind == 'event_msg':
            if item == 'task_started':
                self.task_events = True
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
                if payload['message'].lstrip().startswith(CHECK_MARKER):
                    turn['studioCheck'] = True
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
        elif item == 'message' and payload.get('role') == 'user' and not self.task_events:
            parts = payload.get('content') if isinstance(payload.get('content'), list) else []
            text = '\n'.join(c.get('text', '') for c in parts if isinstance(c, dict) and isinstance(c.get('text'), str)).lstrip()
            # Older rollouts have no task events; each user prompt closes the previous turn and opens the next.
            if text and not text.startswith(('<environment_context>', '<user_instructions>')):
                self.start(digest(self.session + str(at) + text[:200]), at, studio_prompt(text), previous='completed')

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
        if previous is None or any(values[key] < previous[key] for key in values):
            # The first total after a tail seek includes earlier turns, and a total that went
            # down means the session restarted; either way count only the last call.
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
        add_tokens(turn, delta, at)
        self.touch(turn, at)

    def state(self):
        """Serializable parser state so a restart resumes instead of re-reading the tail."""
        current = deepcopy(self.current)
        return {'current': current, 'pending': [c['seq'] for c in (self.current or {}).get('calls', []) if c.get('open')],
                'pendingIds': {key: call['seq'] for key, call in self.pending_calls.items()},
                'usage': dict(list(self.usage_by_message.items())[-200:]), 'codexTotal': self.codex_total,
                'taskEvents': self.task_events,
                # Message IDs only: those of the current turn reattach to it, the rest are ignored.
                'messages': {mid: (turn['id'] if isinstance(turn, dict) else turn) for mid, turn in self.message_turns.items()}}

    def restore(self, saved):
        self.current = saved.get('current')
        self.usage_by_message = saved.get('usage') or {}
        self.codex_total = saved.get('codexTotal')
        self.task_events = saved.get('taskEvents', False)
        calls = {c['seq']: c for c in (self.current or {}).get('calls', [])}
        self.pending_calls = {key: calls[seq] for key, seq in (saved.get('pendingIds') or {}).items() if seq in calls}
        current_id = (self.current or {}).get('id')
        self.message_turns = {mid: self.current if turn_id == current_id else 'ended' for mid, turn_id in (saved.get('messages') or {}).items()}

    def drain(self):
        """Changed turns, each with `newCalls`: the calls added or updated since the last drain."""
        changed = []
        for turn in self.changed.values():
            copy = copy_turn(turn)
            copy['newCalls'] = [dict(call) for call in sorted(self.dirty.pop(turn['id'], {}).values(), key=lambda call: call['seq'])]
            for call in copy['calls'] + copy['newCalls']:
                call.pop('callId', None)
            changed.append(copy)
        self.changed, self.dirty = {}, {}
        return changed


def total_tokens(turn):
    return sum(turn['tokens'].values())


class TranscriptFollower:
    """Follows one transcript file, reading only appended complete records."""

    def __init__(self, path, session, backend, saved=None):
        self.path, self.offset = Path(path), None
        self.parser = TurnParser(session, backend)
        if saved and saved.get('path') == str(path):
            self.offset = saved['offset']
            self.parser.restore(saved['parser'])

    def state(self):
        return {'path': str(self.path), 'offset': self.offset, 'parser': self.parser.state()}

    def read(self):
        size = self.path.stat().st_size
        carried = []
        if self.offset is None or size < self.offset:
            old = self.parser
            if self.offset is not None and old.current and old.current['status'] == 'open':
                # The file was rewritten; its open turn will never finish, so close it.
                old.finish('aborted', old.current['lastAt'])
                carried = old.drain()
            self.offset = max(0, size - INITIAL_TAIL)
            self.parser = TurnParser(old.session, old.backend)
            skip_first = self.offset > 0
        else:
            skip_first = False
        if size == self.offset:
            return carried
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
        return carried + self.parser.drain()


class Telemetry:
    """Stores turn summaries per agent in the progress database."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.followers = {}
        self.errors = {}
        self.saved_at = {}
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS turns (
                    session TEXT NOT NULL, id TEXT NOT NULL, fleet TEXT NOT NULL, agent TEXT NOT NULL,
                    started REAL, last REAL, status TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(session, id));
                CREATE INDEX IF NOT EXISTS turns_agent ON turns(fleet, agent, last);
                CREATE TABLE IF NOT EXISTS telemetry_offsets (follower TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turn_calls (
                    session TEXT NOT NULL, turn TEXT NOT NULL, seq INTEGER NOT NULL, fleet TEXT NOT NULL,
                    at REAL, data TEXT NOT NULL, PRIMARY KEY(session, turn, seq));
                -- Pruning is by age; a narrow index keeps each call row small.
                CREATE INDEX IF NOT EXISTS turn_calls_at ON turn_calls(at);
            ''')
            db.execute('CREATE INDEX IF NOT EXISTS turns_fleet ON turns(fleet, last)')

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
        seen, batch = set(), []
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
                        follower = self.followers[key] = TranscriptFollower(path, key[2], agent.get('backend', 'codex'), self.saved(key))
                    before = follower.offset
                    turns = follower.read()
                    batch.append((fleet['id'], agent['id'], turns, key if self.save_due(key, follower, before, turns) else None, follower, key))
                    self.errors.pop(key, None)
                except (OSError, sqlite3.Error) as exc:
                    self.errors[key] = str(exc)
        try:
            self.store_all([entry[:5] for entry in batch])
        except sqlite3.Error as exc:
            # Followers have moved past what was stored; start them again from their saved positions.
            for entry in batch:
                self.followers.pop(entry[5], None)
                self.saved_at.pop(entry[5], None)
                self.errors[entry[5]] = str(exc)
        for key in set(self.followers) - seen:
            self.followers.pop(key)
            self.saved_at.pop(key, None)

    def save_due(self, key, follower, before, turns):
        """Save the read position at most every 30 s, and whenever a turn ends.

        Re-reading records after a restart is harmless: the saved parser state matches
        the saved offset, and turns and calls are keyed, so replays overwrite in place.
        """
        if follower.offset == before:
            return False
        now = time.monotonic()
        if any(turn['status'] != 'open' for turn in turns) or now - self.saved_at.get(key, 0) >= OFFSET_SAVE_SECONDS:
            self.saved_at[key] = now
            return True
        return False

    def saved(self, key):
        with self.lock, closing(self.connect()) as db:
            row = db.execute('SELECT data FROM telemetry_offsets WHERE follower=?', (json.dumps(key),)).fetchone()
        return json.loads(row['data']) if row else None

    def covered(self, fleet_id, agent):
        """Whether Studio is reading this agent's transcript."""
        return (fleet_id, agent['id'], session_key(agent)) in self.followers

    def store(self, fleet_id, agent_id, turns, key=None, follower=None):
        self.store_all([(fleet_id, agent_id, turns, key, follower)])

    def store_all(self, batch):
        """Write every agent's changes in one transaction: a commit per agent per tick dominates the cost."""
        batch = [entry for entry in batch if entry[2] or entry[3] is not None]
        if not batch:
            return
        with self.lock, closing(self.connect()) as db, db:
            for fleet_id, agent_id, turns, key, follower in batch:
                self.write(db, fleet_id, agent_id, turns, key, follower)

    @staticmethod
    def write(db, fleet_id, agent_id, turns, key, follower):
        for turn in turns:
            calls = turn.pop('newCalls', [])
            db.executemany('INSERT OR REPLACE INTO turn_calls VALUES(?,?,?,?,?,?)',
                           [(turn['session'], turn['id'], call['seq'], fleet_id, call.get('at'), json.dumps(call, separators=(',', ':')))
                            for call in calls])
            db.execute('INSERT OR REPLACE INTO turns VALUES(?,?,?,?,?,?,?,?)',
                       (turn['session'], turn['id'], fleet_id, agent_id, turn['startedAt'], turn['lastAt'],
                        turn['status'], json.dumps(turn, separators=(',', ':'))))
        # A read position is saved in the same transaction as the turns read up to it.
        if key is not None:
            db.execute('INSERT OR REPLACE INTO telemetry_offsets VALUES(?,?)',
                       (json.dumps(key), json.dumps(follower.state(), separators=(',', ':'))))

    def turns(self, fleet_id, agent_id=None, since=0, limit=200, calls=True):
        """Turns newest first, each with all of its stored calls, or only the latest few when `calls` is False."""
        query = 'SELECT session, id FROM turns WHERE fleet=? AND last>=?'
        values = [fleet_id, since]
        if agent_id:
            query += ' AND agent=?'
            values.append(agent_id)
        query += ' ORDER BY last DESC LIMIT ?'
        values.append(limit)
        with self.lock, closing(self.connect()) as db:
            keys = [(row['session'], row['id']) for row in db.execute(query, values)]
            return self.load(db, keys, calls)

    def index(self, fleet_id, since):
        """Cheap listing of turns changed since a time, so callers can reload only what changed."""
        with self.lock, closing(self.connect()) as db:
            # The row size changes with every update (an error flag, a token count), so it marks changes cheaply.
            return [(row['session'], row['id'], row['agent'], row['last'], (row['status'], row['size']))
                    for row in db.execute('SELECT session, id, agent, last, status, length(data) AS size FROM turns WHERE fleet=? AND last>=?',
                                          (fleet_id, since))]

    def load_turns(self, keys, cached=None):
        """Full turns for keys; a turn already in `cached` reads only the calls added since."""
        cached = cached or {}
        with self.lock, closing(self.connect()) as db:
            fresh = [key for key in keys if not cached.get(key, {}).get('tail')]
            output = self.load(db, fresh)
            for key in keys:
                if key in fresh:
                    continue
                row = db.execute('SELECT agent, data FROM turns WHERE session=? AND id=?', key).fetchone()
                if not row:
                    continue
                turn = {**json.loads(row['data']), 'agentId': row['agent']}
                old = cached[key].get('calls', [])
                last = old[-1]['seq'] if old else 0
                calls = {call['seq']: call for call in old}
                for call in db.execute('SELECT data FROM turn_calls WHERE session=? AND turn=? AND seq>? ORDER BY seq', (*key, last)):
                    call = json.loads(call['data'])
                    calls[call['seq']] = call
                # The row's own tail is the latest state of recent calls, such as a result that just failed.
                for call in turn['calls']:
                    calls[call['seq']] = call
                turn['calls'] = [calls[seq] for seq in sorted(calls)]
                output.append(turn)
            return output

    @staticmethod
    def load(db, keys, with_calls=True):
        output = []
        for start in range(0, len(keys), 200):
            chunk = keys[start:start + 200]
            where = ' OR '.join(['(session=? AND id=?)'] * len(chunk))
            rows = {(row['session'], row['id']): row for row in
                    db.execute('SELECT session, id, agent, data FROM turns WHERE ' + where, [v for key in chunk for v in key])}
            calls = {}
            tailed = [key for key in chunk if with_calls and key in rows and '"tail":true' in rows[key]['data']]
            if tailed:
                where = ' OR '.join(['(session=? AND turn=?)'] * len(tailed))
                for row in db.execute('SELECT session, turn, data FROM turn_calls WHERE ' + where + ' ORDER BY seq',
                                      [v for key in tailed for v in key]):
                    calls.setdefault((row['session'], row['turn']), []).append(json.loads(row['data']))
            for key in chunk:
                if key not in rows:
                    continue
                turn = {**json.loads(rows[key]['data']), 'agentId': rows[key]['agent']}
                if key in calls:
                    turn['calls'] = calls[key]
                output.append(turn)
        return output

    def prune(self, db, fleet_id, cutoff, now=None):
        # Per-call detail serves health (one day) and recent timelines; turn summaries keep the full retention.
        call_cutoff = max(cutoff, (now or time.time()) - CALL_RETENTION_DAYS * 86400)
        db.execute('DELETE FROM turn_calls WHERE fleet=? AND at<?', (fleet_id, call_cutoff))
        db.execute('DELETE FROM turns WHERE fleet=? AND last<?', (fleet_id, cutoff))
        # Read positions of agents no longer followed only cost a re-read of the tail if they return.
        with self.lock:
            active = {json.dumps(key) for key in self.followers if key[0] == fleet_id}
        for row in db.execute('SELECT follower FROM telemetry_offsets').fetchall():
            key = json.loads(row['follower'])
            if key[0] == fleet_id and row['follower'] not in active:
                db.execute('DELETE FROM telemetry_offsets WHERE follower=?', (row['follower'],))
