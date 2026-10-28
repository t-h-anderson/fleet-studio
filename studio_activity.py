"""Read public transcript messages and supplied reasoning summaries, without tools.

Tool-call metadata is recorded separately by studio_telemetry."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from studio_runtime import connect, normalize_path


def public_messages(rows):
    messages, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get('type') != 'response_item':
            continue
        item = row.get('payload', {})
        if not isinstance(item, dict):
            continue
        kind, text = None, ''
        if item.get('type') == 'message':
            role, phase = item.get('role'), item.get('phase') or item.get('channel')
            if role not in ('user', 'assistant') or phase in ('analysis', 'justify', 'confidence') or item.get('channel') == 'analysis' or item.get('recipient') not in (None, 'all'):
                continue
            if role == 'assistant' and phase not in (None, 'commentary', 'final', 'final_answer'):
                continue
            kind = 'input' if role == 'user' else 'progress' if phase == 'commentary' else 'summary'
            text = '\n'.join(c['text'] for c in (item.get('content') if isinstance(item.get('content'), list) else []) if isinstance(c, dict) and c.get('type') in ('input_text', 'output_text', 'text') and isinstance(c.get('text'), str))
        elif item.get('type') == 'reasoning':
            # Only the explicit display summary is public. Never read raw
            # reasoning content, encrypted_content or event raw_content.
            kind = 'thinking'
            text = '\n'.join(c['text'] for c in (item.get('summary') if isinstance(item.get('summary'), list) else []) if isinstance(c, dict) and c.get('type') == 'summary_text' and isinstance(c.get('text'), str))[:800]
        if not kind or not text.strip():
            continue
        stamp = row.get('timestamp') if isinstance(row.get('timestamp'), str) else ''
        identity = (item.get('id') if isinstance(item.get('id'), str) else None) or hashlib.sha256((stamp + kind + text).encode()).hexdigest()[:24]
        if identity in seen:
            continue
        seen.add(identity)
        messages.append({'id': identity, 'kind': kind, 'text': text[:16000], 'truncated': len(text) > 16000,
                         'timestamp': stamp})
    return messages[-100:]


def rollout_path(root, session_id):
    """The session's rollout file when it lies inside the Codex session folder."""
    with closing(connect(root, 'state_5.sqlite')) as db:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (session_id,)).fetchone()
    if not row or not row['rollout_path']:
        return None
    path = Path(normalize_path(row['rollout_path'])).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix != '.jsonl':
        return None
    return path


def activity(root, session_id, fixture=None):
    if fixture:
        rows = json.loads(Path(fixture).read_text(encoding='utf-8'))
        return {'messages': public_messages(rows), 'observedAt': time.time(), 'limited': False}
    try:
        with closing(connect(root, 'state_5.sqlite')) as db:
            row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (session_id,)).fetchone()
        if not row or not row['rollout_path']:
            return {'messages': [], 'error': 'No transcript is available for this session.'}
        path = Path(normalize_path(row['rollout_path'])).resolve()
        # The browser chooses a fleet agent, never a filesystem path.
        if not path.is_relative_to(root.resolve()) or path.suffix != '.jsonl':
            return {'messages': [], 'error': 'This session transcript is outside the Codex session folder.'}
        size = path.stat().st_size
        with path.open('rb') as stream:
            start = max(0, size - 4_000_000)
            stream.seek(start)
            data = stream.read(4_000_000)
        lines = data.splitlines()
        if start:
            lines = lines[1:]
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue  # An in-progress trailing record is picked up next refresh.
        return {'messages': public_messages(rows), 'observedAt': time.time(), 'limited': start > 0}
    except (OSError, sqlite3.Error):
        return {'messages': [], 'error': 'The saved session transcript could not be read. Retry after the next update.'}
