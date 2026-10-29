"""Read-only native descendant observations; never opens or controls a child."""
from contextlib import ExitStack, closing
import sqlite3
import time

MAX_CHILDREN = 500
TOOL_ITEMS = ('commandExecution', 'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall', 'collabToolCall')


def observe_children(root, session_ids):
    from studio_runtime import connect
    result = {sid: {'known': False, 'observedAt': time.time(), 'children': [],
                    'active': 0, 'uncertain': 0, 'tokensUsed': None} for sid in session_ids}
    def unsupported(error):
        for report in result.values():
            report.update(supported=False, error=error)
        return result
    try:
        if not (root / 'state_5.sqlite').is_file():
            # No Codex state database means no relationship metadata to read, not uncertain children.
            return unsupported('No Codex state database records subagent relationships here.')
        with ExitStack() as stack:
            state = stack.enter_context(closing(connect(root, 'state_5.sqlite')))
            # Capability detection is intentional: missing schemas are unknown,
            # rather than proof that this parent has no children. A Codex that never
            # records spawn edges is marked unsupported, so only actions that need
            # known children are held there, not the whole fleet's supervision.
            # The probe runs before the history database opens, so a Codex without
            # either is unsupported, while one with edges but no history fails closed.
            try:
                state.execute('SELECT parent_thread_id,child_thread_id,status FROM thread_spawn_edges LIMIT 0')
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                return unsupported('This Codex version records no subagent relationships.')
            history = stack.enter_context(closing(connect(root, 'thread_history_1.sqlite')))
            columns = {r[1] for r in state.execute('PRAGMA table_info(threads)')}
            fields = [k for k in ('id', 'agent_nickname', 'agent_path', 'model', 'tokens_used', 'updated_at', 'updated_at_ms') if k in columns]
            item_columns = {r[1] for r in history.execute('PRAGMA table_info(thread_items)')}
            unsettled_only = ' AND completed_at_ms IS NULL' if 'completed_at_ms' in item_columns else ''
            cache = {}
            for parent, report in result.items():
                seen, frontier, children = {parent}, [parent], []
                depths = {parent: 0}
                complete = True
                while frontier:
                    placeholders = ','.join('?' for _ in frontier)
                    edges = state.execute('SELECT parent_thread_id,child_thread_id,status FROM thread_spawn_edges WHERE parent_thread_id IN (' + placeholders + ') LIMIT ?',
                                          (*frontier, MAX_CHILDREN + 1)).fetchall()
                    frontier = []
                    for edge in edges:
                        sid = edge['child_thread_id']
                        if sid in seen or len(children) >= MAX_CHILDREN:
                            complete = False
                            continue
                        seen.add(sid)
                        depths[sid] = depths[edge['parent_thread_id']] + 1
                        frontier.append(sid)
                        if sid not in cache:
                            row = state.execute('SELECT ' + ','.join(fields) + ' FROM threads WHERE id=?', (sid,)).fetchone()
                            turn = history.execute('SELECT turn_id,status,started_at,completed_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1', (sid,)).fetchone()
                            native = dict(row) if row else {}
                            status = 'unknown'
                            if turn:
                                if turn['status'] == 'inProgress':
                                    status = 'working'
                                elif turn['status'] == 'completed' and turn['completed_at']:
                                    status = 'idle'
                                elif turn['status'] in ('failed', 'error', 'aborted', 'interrupted'):
                                    status = 'attention'
                                # A finished child turn must not hide unsettled tool items.
                                if status != 'working':
                                    try:
                                        statuses = history.execute('SELECT CASE WHEN json_valid(item_json) THEN json_extract(item_json, \'$.status\') ELSE NULL END AS status FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type IN (' + ','.join('?' for _ in TOOL_ITEMS) + ')' + unsettled_only, (sid, turn['turn_id'], *TOOL_ITEMS))
                                        if any(r['status'] not in ('completed', 'failed', 'declined') for r in statuses):
                                            status = 'unknown'
                                    except (sqlite3.Error, ValueError, AttributeError):
                                        status = 'unknown'
                            cache[sid] = {'sessionId': sid, 'name': native.get('agent_nickname') or 'Subagent',
                                          'path': native.get('agent_path') or '', 'model': native.get('model') or '',
                                          'tokensUsed': native.get('tokens_used'), 'state': status,
                                          'turnStatus': turn['status'] if turn else None,
                                          'turnId': turn['turn_id'] if turn else None,
                                          'lastActivityAt': max(native.get('updated_at') or 0, (native.get('updated_at_ms') or 0) / 1000, (turn['completed_at'] or turn['started_at'] or 0) if turn else 0) or None}
                        child = {**cache[sid], 'parentSessionId': edge['parent_thread_id'], 'depth': depths[sid]}
                        if edge['status'] == 'closed' and child['state'] in ('idle', 'attention'):
                            child['state'] = 'closed'
                        elif edge['status'] not in ('open', 'closed'):
                            child['state'] = 'unknown'
                        children.append(child)
                report.update(known=complete, children=children,
                              active=sum(c['state'] == 'working' for c in children),
                              uncertain=sum(c['state'] in ('unknown', 'attention') for c in children))
                tokens = [c['tokensUsed'] for c in children]
                if all(type(n) is int and n >= 0 for n in tokens):
                    report['tokensUsed'] = sum(tokens)
    except (OSError, sqlite3.Error, ValueError) as exc:
        for report in result.values():
            report.update(known=False, error='Native subagent observations unavailable: ' + str(exc))
    return result


def hold_reason(session, now=None, require_known=False, risky=False):
    """Legacy/remote observations without this capability remain compatible.

    Native goal recovery explicitly requires a known, fresh descendant graph.
    Local observations always supply the field, including capability failures.
    A local Codex that records no spawn edges is unsupported: ordinary supervision
    continues there, and only risky actions (automatic stops) or callers that
    require known children are held.
    """
    report = session.get('subagents')
    if report is None and not require_known:
        return ''
    if report and report.get('supported') is False:
        return 'Subagent tracking is unavailable on this Codex version; review before intervening.' if require_known or risky else ''
    now = time.time() if now is None else now
    if not report or not report.get('known') or not 0 <= now - (report.get('observedAt') or 0) <= 20:
        return 'Subagent activity is unavailable or uncertain; review before intervening.'
    if report.get('uncertain'):
        return 'A subagent has failed or has an unsettled tool outcome; parent review is required.'
    if report.get('active'):
        return 'Subagents are working; let the parent coordinate them.'
    return ''
