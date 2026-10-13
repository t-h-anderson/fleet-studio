"""Read-only Codex metadata and Windows process observations."""
import ctypes
from contextlib import closing
from ctypes import wintypes as w
import os
from pathlib import Path
import sqlite3
import time


class UniqueProcess(ctypes.Structure):
    _fields_ = [("pid", w.DWORD), ("start", w.FILETIME)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", UniqueProcess), ("app", w.WCHAR * 256),
        ("service", w.WCHAR * 64), ("app_type", w.DWORD),
        ("app_status", w.ULONG), ("session", w.DWORD), ("restartable", w.BOOL),
    ]


def writer_owners(lock_path):
    """Query existing lock owners. Never modify locks or native Codex state."""
    if not lock_path.is_file():
        return []
    if os.name != "nt":
        raise OSError("Live process detection requires Windows.")
    rm = ctypes.WinDLL("Rstrtmgr")
    handle = w.DWORD()
    key = ctypes.create_unicode_buffer(33)
    code = rm.RmStartSession(ctypes.byref(handle), 0, key)
    if code:
        raise OSError(code, "RmStartSession")
    try:
        paths = (w.LPCWSTR * 1)(str(lock_path))
        code = rm.RmRegisterResources(handle, 1, paths, 0, None, 0, None)
        if code:
            raise OSError(code, "RmRegisterResources")
        needed, count, reasons = w.UINT(), w.UINT(), w.DWORD()
        code = rm.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                            None, ctypes.byref(reasons))
        if code == 0:
            return []
        for _ in range(3):
            if code != 234:
                raise OSError(code, "RmGetList")
            rows = (ProcessInfo * needed.value)()
            count.value = needed.value
            code = rm.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                                rows, ctypes.byref(reasons))
            if code == 0:
                return [int(row.process.pid) for row in rows[:count.value]]
        raise OSError("Process owners changed during observation.")
    finally:
        rm.RmEndSession(handle)


def normalize_path(value):
    value = str(value or "")
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def connect(root, filename):
    db = sqlite3.connect((root / filename).as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    return db


def sessions(root):
    with closing(connect(root, "state_5.sqlite")) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
        fields = [name for name in ("id", "name", "title", "cwd", "updated_at",
                                   "model", "reasoning_effort", "model_provider") if name in columns]
        rows = db.execute("SELECT " + ",".join(fields) +
                          " FROM threads WHERE source='cli' AND archived=0 ORDER BY updated_at DESC LIMIT 500").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        result.append({
            "id": item["id"], "name": item.get("name") or item.get("title") or "Untitled session",
            "directory": normalize_path(item.get("cwd")), "updatedAt": item.get("updated_at"),
            "model": item.get("model") or "", "reasoning": item.get("reasoning_effort") or "",
            "provider": item.get("model_provider") or "",
        })
    return result


def observe(root, session_ids):
    output = {}
    errors = []
    try:
        recent = sessions(root)
    except (OSError, sqlite3.Error) as exc:
        recent = []
        errors.append("Session catalogue: " + str(exc))
    known = {row["id"]: row for row in recent}
    usage = session_usage(root, session_ids)
    history = goals = queued = None
    try:
        try:
            history = connect(root, "thread_history_1.sqlite")
        except (OSError, sqlite3.Error) as exc:
            errors.append("Turn status: " + str(exc))
        try:
            goals = connect(root, "goals_1.sqlite")
        except (OSError, sqlite3.Error):
            pass  # Goals are optional and are not present in every CLI version.
        try:
            queued = connect(root, "queue_1.sqlite")
        except (OSError, sqlite3.Error):
            pass
        for session_id in sorted(set(session_ids)):
            item = {
                "sessionId": session_id, "status": "unknown", "pids": [],
                "observedAt": time.time(), "session": known.get(session_id),
                "turnStatus": None, "goalStatus": None, "queueCount": None, "goalKnown": False,
                "usage": usage.get(session_id, {}),
            }
            try:
                item["pids"] = writer_owners(root / "thread-writer-locks" / (session_id + ".lock"))
                turn = history.execute(
                    "SELECT turn_id,status,started_at,completed_at FROM thread_turns WHERE thread_id=? "
                    "ORDER BY rollout_ordinal DESC LIMIT 1", (session_id,)).fetchone() if history else None
                if turn:
                    item["turnId"] = turn["turn_id"]
                    item["turnStatus"] = turn["status"]
                    item["turnStartedAt"] = turn["started_at"]
                    item["turnCompletedAt"] = turn["completed_at"]
                goal = goals.execute("SELECT * FROM thread_goals WHERE thread_id=?",
                                     (session_id,)).fetchone() if goals else None
                if goals:
                    item["goalKnown"] = True
                if goal:
                    row = dict(goal)
                    item["goalStatus"] = normalize_goal_status(row['status'])
                    item['goal'] = {target: row.get(source) for source, target in (
                        ('goal_id', 'id'), ('objective', 'objective'), ('token_budget', 'tokenBudget'),
                        ('tokens_used', 'tokensUsed'), ('time_used_seconds', 'timeUsedSeconds'))}
                    item['goal'].update(status=item['goalStatus'], createdAt=(row.get('created_at_ms') or 0) / 1000,
                                        updatedAt=(row.get('updated_at_ms') or 0) / 1000)
                if queued:
                    item["queueCount"] = queued.execute("SELECT count(*) FROM queued_items WHERE thread_id=?", (session_id,)).fetchone()[0]
                if not item["pids"]:
                    item["status"] = "closed"
                elif item["turnStatus"] == "inProgress":
                    item["status"] = "working"
                elif item["turnStatus"] in ("failed", "error"):
                    item["status"] = "attention"
                else:
                    item["status"] = "idle"
            except (OSError, sqlite3.Error) as exc:
                item["error"] = str(exc)
                item["status"] = "unknown"
            output[session_id] = item
    finally:
        if history:
            history.close()
        if goals:
            goals.close()
        if queued:
            queued.close()
    return {"observedAt": time.time(), "sessions": output, "catalogue": recent, "errors": errors}


def normalize_goal_status(status):
    return {'usageLimited': 'usage_limited', 'budgetLimited': 'budget_limited'}.get(status, status)


def session_usage(root, session_ids):
    """Read numeric native metadata for the exact linked sessions, including archived ones."""
    result = {sid: {} for sid in set(session_ids)}
    try:
        with closing(connect(root, 'state_5.sqlite')) as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            fields = [key for key in ('tokens_used', 'model') if key in columns]
            if fields:
                for sid in result:
                    row = db.execute('SELECT ' + ','.join(fields) + ' FROM threads WHERE id=?', (sid,)).fetchone()
                    if row:
                        data = dict(row)
                        result[sid].update(tokensUsed=data.get('tokens_used'), model=data.get('model'))
    except (OSError, sqlite3.Error):
        pass
    try:
        with closing(connect(root, 'goals_1.sqlite')) as db:
            for sid in result:
                row = db.execute('SELECT tokens_used,token_budget FROM thread_goals WHERE thread_id=?', (sid,)).fetchone()
                if row:
                    result[sid].update(goalTokens=row['tokens_used'], goalBudget=row['token_budget'])
    except (OSError, sqlite3.Error):
        pass
    return result
