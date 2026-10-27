"""Agent runtime backends. Each backend observes and messages one kind of agent CLI.

A backend exposes the same small surface whether its sessions run on this machine
or behind a remote fleet node, so Studio's monitoring, triggers and controls stay
agnostic of the agent vendor and its location. Backends never write native agent
databases; they read metadata and use each CLI's supported commands.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from studio_bindings import runtime_binding, session_key
import studio_runtime
from studio_activity import activity as codex_activity, rollout_path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL_HOST = "local"
BACKENDS = ("codex", "claude-code")


class BackendUnsupported(ValueError):
    """The selected backend does not offer this operation."""


class NotReady(RuntimeError):
    """The session cannot take a message yet; nothing was sent and delivery may be retried."""


class AgentBackend:
    """Common contract. Subclasses declare capabilities so callers can explain gaps."""
    name = ""
    label = ""
    capabilities = {"queue": False, "rename": False, "goals": False, "activity": False, "tokens": False}

    def catalogue(self):
        return []

    def observe(self, session_ids):
        raise NotImplementedError

    def send(self, session_id, text, directory=""):
        raise BackendUnsupported(self.label + " cannot deliver messages.")

    def rename(self, session_id, name):
        raise BackendUnsupported(self.label + " sessions cannot be renamed from Studio.")

    def activity(self, session_id, fixture=None):
        return {"messages": [], "error": self.label + " does not expose a readable transcript."}

    def resume_command(self, session_id, directory):
        """Argument vector that reopens the session interactively in its folder."""
        raise NotImplementedError

    def ready_to_launch(self, session_id):
        """Recheck native ownership on the execution host immediately before launch."""
        observed = self.observe([session_id]).get('sessions', {}).get(session_id, {})
        if observed.get('pids'):
            return False
        if observed.get('error') or observed.get('status') != 'closed':
            raise RuntimeError('Session is not confirmed closed; check its native client before opening it.')
        return True


class CodexBackend(AgentBackend):
    name, label = "codex", "Codex CLI"
    capabilities = {"queue": True, "rename": True, "goals": True, "activity": True, "tokens": True}

    def __init__(self, executable="", root=None):
        self.executable = executable or shutil.which("codex") or "codex"
        self.root = Path(root or os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))

    def catalogue(self):
        return [{**row, "backend": self.name} for row in studio_runtime.sessions(self.root)]

    def observe(self, session_ids):
        result = studio_runtime.observe(self.root, session_ids)
        for row in result["catalogue"]:
            row["backend"] = self.name
        return result

    def send(self, session_id, text, directory=""):
        # Codex queues the message for the session's next turn; delivery is not adoption.
        run = subprocess.run([self.executable, "queue", "--thread", session_id, "--message", text],
                             capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                             creationflags=CREATE_NO_WINDOW)
        if run.returncode:
            raise RuntimeError((run.stderr or run.stdout or "Codex rejected the message.")[-1500:])
        return "queued", "Accepted by Codex queue; agent adoption is not yet confirmed."

    def rename(self, session_id, name):
        from studio_control import rename_session
        rename_session(self.executable, session_id, name)
        return "renamed", "Codex acknowledged the conversation name."

    def activity(self, session_id, fixture=None):
        return codex_activity(self.root, session_id, fixture)

    def transcript_path(self, session_id):
        return rollout_path(self.root, session_id)

    def resume_command(self, session_id, directory):
        return [self.executable, "resume", session_id, "--cd", directory]


def claude_text(content):
    """Public text blocks only. Thinking, tool inputs and tool results are never copied here;
    studio_telemetry keeps tool-call metadata (name, short signature, error flag) separately."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))


class ClaudeTranscript:
    """Incremental reader so each five-second observation parses only appended records."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.usage_by_message = {}
        self.offset = 0
        self.state = {"title": "", "cwd": "", "model": "", "tokensUsed": 0, "lastType": "",
                      "stopReason": None, "lastAt": "", "messages": []}

    def refresh(self):
        with self.lock:
            return deepcopy(self._refresh())

    def _refresh(self):
        size = self.path.stat().st_size
        if size < self.offset:
            # A rewritten transcript invalidates accumulated counters.
            fresh = ClaudeTranscript(self.path)
            self.state, self.offset, self.usage_by_message = fresh.state, 0, {}
        if size == self.offset:
            return self.state
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(size - self.offset)
        # Leave an incomplete trailing record for the next refresh.
        complete = data.rfind(b"\n") + 1
        self.offset += complete
        for line in data[:complete].splitlines():
            try:
                self.record(json.loads(line))
            except (ValueError, UnicodeError, TypeError, AttributeError):
                continue
        return self.state

    def record(self, row):
        state = self.state
        kind = row.get("type")
        # A user-chosen title outranks generated titles and summaries.
        if kind == "custom-title" and isinstance(row.get("customTitle"), str):
            state["title"], state["customTitle"] = row["customTitle"], True
        elif kind == "ai-title" and isinstance(row.get("aiTitle"), str) and not state.get("customTitle"):
            state["title"] = row["aiTitle"]
        elif kind == "summary" and isinstance(row.get("summary"), str) and not state["title"]:
            state["title"] = row["summary"]
        if kind not in ("user", "assistant") or row.get("isSidechain") or row.get("isMeta"):
            return
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        state["cwd"] = row.get("cwd") or state["cwd"]
        state["lastType"] = kind
        state["lastAt"] = row.get("timestamp") or state["lastAt"]
        if kind == "assistant":
            state["model"] = message.get("model") or state["model"]
            state["stopReason"] = message.get("stop_reason")
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            mid = message.get('id') or row.get('requestId') or row.get('uuid')
            counters = {key: value for key, value in usage.items()
                        if key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
                        and type(value) is int and value >= 0}
            old = self.usage_by_message.get(mid, {}) if mid else {}
            updated = {k: max(v, old.get(k, 0)) for k, v in counters.items()}
            state['tokensUsed'] += sum(updated.values()) - sum(old.get(k, 0) for k in updated)
            if mid:
                self.usage_by_message[mid] = {**old, **updated}
        text = claude_text(message.get("content"))
        if not text.strip():
            return
        identity = row.get("uuid") or hashlib.sha256((state["lastAt"] + text).encode()).hexdigest()[:24]
        finished = kind == "assistant" and message.get("stop_reason") == "end_turn"
        state["messages"].append({"id": identity, "kind": "input" if kind == "user" else "summary" if finished else "progress",
                                  "text": text[:16000], "truncated": len(text) > 16000, "timestamp": state["lastAt"]})
        del state["messages"][:-100]


def claude_processes(session_id, directory=""):
    """Claude processes that may hold this session. None when the OS cannot be inspected.

    Sessions started without --resume carry no ID on the command line, so a Claude
    process in the session's folder counts unless it resumes another session. A
    false match only holds delivery, which is the safe direction.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    owners = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except PermissionError:
            try:
                if entry.stat().st_uid == os.getuid():
                    return None
            except FileNotFoundError:
                pass
            continue
        except FileNotFoundError:
            continue
        words = [part.decode("utf-8", "replace") for part in argv if part]
        if not words or not any(Path(w).name in ("claude", "claude.exe") or w.endswith("/claude/cli.js") for w in words[:2]):
            continue
        if session_id in words:
            owners.append(int(entry.name))
            continue
        resumed = [words[i + 1] for i, w in enumerate(words[:-1]) if w in ("--resume", "-r", "--session-id")]
        try:
            folder = os.readlink(entry / "cwd")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if directory and not resumed and Path(folder).resolve() == Path(directory).resolve():
            owners.append(int(entry.name))
    return owners


class ClaudeCodeBackend(AgentBackend):
    name, label = "claude-code", "Claude Code"
    capabilities = {"queue": False, "rename": False, "goals": False, "activity": True, "tokens": True}
    # A transcript written this recently is treated as an active turn.
    active_seconds = 90

    def __init__(self, executable="", root=None, runtime=None):
        self.executable = executable or shutil.which("claude") or "claude"
        self.root = Path(root or os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        self.runtime = Path(runtime) if runtime else None
        self.lock = threading.Lock()
        self.readers = {}
        self.send_lock = threading.Lock()
        self.processes = {}
        # Sessions whose Studio turn was stopped mid-tool, by transcript mtime at the stop.
        self.interrupted = {}

    def transcripts(self):
        projects = self.root / "projects"
        if not projects.is_dir():
            return {}
        return {path.stem: path for path in projects.glob("*/*.jsonl")}

    def reader(self, path):
        with self.lock:
            item = self.readers.get(path)
            if item is None:
                item = self.readers[path] = ClaudeTranscript(path)
            return item

    def catalogue(self):
        rows = []
        files = sorted(self.transcripts().items(), key=lambda item: item[1].stat().st_mtime, reverse=True)[:500]
        for session_id, path in files:
            try:
                state = self.reader(path).refresh()
            except OSError:
                continue
            rows.append({"id": session_id, "name": state["title"] or "Untitled session", "directory": state["cwd"],
                         "updatedAt": path.stat().st_mtime, "model": state["model"], "reasoning": "",
                         "provider": "anthropic", "backend": self.name})
        return rows

    def observe(self, session_ids):
        files = self.transcripts()
        output, errors, now = {}, [], time.time()
        for session_id in sorted(set(session_ids)):
            item = {"sessionId": session_id, "status": "unknown", "pids": [], "observedAt": now, "session": None,
                    "turnStatus": None, "goalStatus": None, "queueCount": None, "goalKnown": False, "usage": {},
                    "backend": self.name}
            path = files.get(session_id)
            if not path:
                item["error"] = "No Claude Code transcript found for this session."
                output[session_id] = item
                continue
            try:
                state = self.reader(path).refresh()
                modified = path.stat().st_mtime
                # Tool use and unanswered prompts leave a turn open; end_turn closes it.
                open_turn = state["lastType"] == "user" or state["stopReason"] in ("tool_use", None)
                item["turnStatus"] = "inProgress" if open_turn else "completed"
                item["usage"] = {"tokensUsed": state["tokensUsed"], "model": state["model"]}
                item["session"] = {"id": session_id, "name": state["title"] or "Untitled session",
                                   "directory": state["cwd"], "updatedAt": modified, "model": state["model"]}
                owners = claude_processes(session_id, state["cwd"])
                item["pids"] = owners or []
                item["livenessKnown"] = owners is not None
                stopped_at = self.interrupted.get(session_id)
                if stopped_at is not None and modified <= stopped_at + 1 and not owners:
                    # A stopped turn leaves its last record open; nothing has written since.
                    item["turnStatus"] = "interrupted"
                if item["turnStatus"] == "inProgress":
                    item["status"] = "working"
                elif owners is None:
                    item['status'] = 'unknown'
                elif owners:
                    item["status"] = "idle"
                else:
                    item["status"] = "closed"
            except OSError as exc:
                item["error"] = str(exc)
            output[session_id] = item
        return {"observedAt": now, "sessions": output, "catalogue": [], "errors": errors}

    def send(self, session_id, text, directory=""):
        with self.send_lock:
            previous = self.processes.get(session_id)
            if previous is not None and previous.poll() is None:
                raise RuntimeError('A Studio-started Claude turn is already running for this session.')
            return self._send(session_id, text, directory)

    def _send(self, session_id, text, directory):
        # Claude Code has no message queue. A headless resume runs one turn in the
        # same session, so refuse while an interactive client holds it.
        path = self.transcripts().get(session_id)
        if not path:
            raise RuntimeError('No saved Claude transcript found; refusing to resume an unidentified session.')
        state = self.reader(path).refresh()
        if not directory or not state['cwd'] or Path(directory).resolve() != Path(state['cwd']).resolve():
            raise RuntimeError('The assigned folder differs from the saved Claude session.')
        if time.time() - path.stat().st_mtime < self.active_seconds:
            raise NotReady("This Claude Code session wrote to its transcript recently. Deliver after its turn settles.")
        owners = claude_processes(session_id, directory)
        if owners is None:
            raise RuntimeError('Claude process ownership is unavailable on this host; no headless turn was started.')
        if owners:
            raise NotReady("A Claude Code client may have this session open (PID "
                           + ",".join(map(str, owners)) + "). Deliver at a safe boundary or close it first.")
        log = None
        if self.runtime:
            folder = self.runtime / "claude-turns"
            folder.mkdir(parents=True, exist_ok=True)
            log = (folder / (session_id + "-" + str(int(time.time())) + ".log")).open("ab")
        try:
            process = subprocess.Popen([self.executable, "-p", "--resume", session_id, "--output-format", "json", '--', text],
                             cwd=directory or None, stdin=subprocess.DEVNULL, stdout=log or subprocess.DEVNULL,
                             stderr=subprocess.STDOUT if log else subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                             start_new_session=os.name != "nt")
            self.processes[session_id] = process
        finally:
            if log:
                log.close()
        return 'started', 'Headless Claude Code process started; turn completion and agent adoption are not yet confirmed.'

    def transcript_path(self, session_id):
        return self.transcripts().get(session_id)

    def interrupt(self, session_id):
        """Stop a Studio-started headless turn. Native clients are never signalled."""
        with self.send_lock:
            process = self.processes.get(session_id)
            if process is None or process.poll() is not None:
                raise RuntimeError('No Studio-started Claude turn is running for this session.')
            # The turn runs in its own process group; stop its tools too.
            stop = (lambda sig: os.killpg(process.pid, sig)) if os.name != 'nt' else (lambda sig: process.terminate())
            try:
                stop(signal.SIGTERM)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stop(signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)
            except ProcessLookupError:
                pass
            path = self.transcripts().get(session_id)
            self.interrupted[session_id] = path.stat().st_mtime if path else time.time()
        return 'Stopped the Studio-started Claude turn.'

    def activity(self, session_id, fixture=None):
        path = self.transcripts().get(session_id)
        if not path:
            return {"messages": [], "error": "No transcript is available for this session."}
        try:
            state = self.reader(path).refresh()
        except OSError:
            return {"messages": [], "error": "The saved session transcript could not be read. Retry after the next update."}
        return {"messages": deepcopy(state["messages"]), "observedAt": time.time(), "limited": False}

    def resume_command(self, session_id, directory):
        return [self.executable, "--resume", session_id]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Remote node redirects are refused.', headers, fp)


class RemoteHost:
    """A fleet node on another machine, reached with a bearer token held outside Studio's files."""

    def __init__(self, config, timeout=8):
        self.id = config["id"]
        self.url = config["url"].rstrip("/")
        self.token_env = config.get("tokenEnv", "")
        self.timeout = timeout

    def call(self, path, payload):
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        if not token:
            raise ValueError("Set the " + (self.token_env or "host token") + " environment variable for remote host " + self.id + ".")
        request = Request(self.url + path, data=json.dumps(payload).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "")
            except ValueError:
                detail = ""
            if path in ('/node/send', '/node/rename', '/node/launch') and exc.code >= 500:
                raise TimeoutError('Remote host ' + self.id + ' could not confirm the operation; inspect native state before retrying.') from None
            raise RuntimeError("Remote host " + self.id + " refused the request: " + (detail or str(exc.code))) from None
        except (ValueError, UnicodeError):
            raise TimeoutError('Remote host ' + self.id + ' returned an unreadable acknowledgement.') from None
        except (URLError, OSError, TimeoutError) as exc:
            raise TimeoutError("Remote host " + self.id + " is unreachable: " + str(exc)) from None


class RemoteBackend(AgentBackend):
    """Proxy for a backend that runs on a remote fleet node."""

    def __init__(self, host, backend):
        self.host, self.name = host, backend
        self.label = backend + " on " + host.id
        self.capabilities = dict(BACKEND_TYPES[backend].capabilities, goals=False)

    def request(self, action, **payload):
        return self.host.call("/node/" + action, {"backend": self.name, **payload})

    def catalogue(self):
        return [{**row, "backend": self.name, "hostId": self.host.id} for row in self.request("catalogue").get("sessions", [])]

    def observe(self, session_ids):
        return self.request("observe", sessionIds=list(session_ids))

    def send(self, session_id, text, directory=""):
        result = self.request("send", sessionId=session_id, text=text, directory=directory)
        return result["status"], result["detail"] + " (via " + self.host.id + ")"

    def rename(self, session_id, name):
        result = self.request("rename", sessionId=session_id, name=name)
        return result["status"], result["detail"] + " (via " + self.host.id + ")"

    def activity(self, session_id, fixture=None):
        return self.request("activity", sessionId=session_id)


BACKEND_TYPES = {"codex": CodexBackend, "claude-code": ClaudeCodeBackend}


def clean_hosts(value):
    """Remote fleet nodes. Tokens are named environment variables, never stored values."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("Keep at most 50 remote hosts.")
    output, ids = [], {LOCAL_HOST}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Remote hosts must be objects.")
        host_id, url, token_env = raw.get("id"), raw.get("url"), raw.get("tokenEnv", "")
        if not isinstance(host_id, str) or not host_id or len(host_id) > 40 or not host_id.replace("-", "").isalnum() or host_id in ids:
            raise ValueError("Remote host IDs are unique letters, numbers and hyphens, and cannot be 'local'.")
        if not isinstance(url, str) or len(url) > 500:
            raise ValueError("Remote host " + host_id + " needs a URL.")
        parts = urlsplit(url)
        loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
        # Plain HTTP is accepted only for loopback, i.e. an SSH or similar tunnel.
        if not parts.hostname or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not loopback) or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Remote host " + host_id + " needs an HTTPS URL, or a loopback HTTP tunnel, without credentials.")
        try:
            parts.port
        except ValueError:
            raise ValueError('Remote host has an invalid port.') from None
        if not isinstance(token_env, str) or not token_env.replace("_", "").isalnum() or len(token_env) > 100:
            raise ValueError("Remote host " + host_id + " needs the name of the environment variable holding its token.")
        name = raw.get("name", host_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("Remote host " + host_id + " needs a name of at most 80 characters.")
        ids.add(host_id)
        output.append({"id": host_id, "name": name.strip(), "url": url.rstrip("/"), "tokenEnv": token_env})
    return output


def clean_binding(agent, host_ids):
    backend = agent.get("backend", "codex")
    host = agent.get("hostId", LOCAL_HOST)
    if backend not in BACKENDS:
        raise ValueError("Unknown agent backend: " + str(backend))
    if host != LOCAL_HOST and host not in host_ids:
        raise ValueError("Unknown agent host: " + str(host))
    return backend, host


class Runtimes:
    """Routes agent operations to the backend for each agent's (host, backend) binding."""

    def __init__(self, data_root, codex_path="", codex_root=None):
        self.data_root = Path(data_root)
        self.local = {"codex": CodexBackend(codex_path, codex_root),
                      "claude-code": ClaudeCodeBackend(root=None, runtime=self.data_root / ".runtime")}
        self.hosts = {}
        self.remote_catalogue = {}

    def configure(self, hosts):
        self.hosts = {host["id"]: RemoteHost(host) for host in hosts}

    def backend(self, backend="codex", host=LOCAL_HOST):
        if host in (LOCAL_HOST, "", None):
            return self.local[backend]
        if host not in self.hosts:
            raise ValueError("Remote host " + str(host) + " is not configured.")
        return RemoteBackend(self.hosts[host], backend)

    def for_agent(self, agent):
        return self.backend(agent.get("backend", "codex"), agent.get("hostId", LOCAL_HOST))

    def observe(self, agents):
        """Merge observations from every binding in use into the monitor's existing shape."""
        groups = {}
        for agent in agents:
            if agent.get("sessionId"):
                key = (agent.get("hostId", LOCAL_HOST), agent.get("backend", "codex"))
                groups.setdefault(key, set()).add(agent["sessionId"])
        # Observe an installed Codex even without bound agents so its sessions can be linked.
        if self.local["codex"].root.is_dir():
            groups.setdefault((LOCAL_HOST, "codex"), set())
        merged = {"observedAt": time.time(), "sessions": {}, "catalogue": [], "errors": []}
        def read(group):
            (host, backend), ids = group
            try:
                return self.backend(backend, host).observe(sorted(ids))
            except Exception as exc:
                return exc
        grouped = sorted(groups.items())
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(grouped)))) as pool:
            results = list(pool.map(read, grouped))
        for ((host, backend), ids), result in zip(grouped, results):
            if isinstance(result, Exception):
                exc = result
                merged["errors"].append(("" if host == LOCAL_HOST else host + " · ") + backend + ": " + str(exc))
                for sid in ids:
                    merged["sessions"][session_key({"sessionId": sid, "backend": backend, "hostId": host})] = {"sessionId": sid, "status": "unknown", "pids": [], "observedAt": time.time(),
                                               "error": str(exc), "backend": backend, "hostId": host, "usage": {}}
                continue
            for sid, item in result.get("sessions", {}).items():
                if sid not in ids:
                    continue
                merged["sessions"][session_key({"sessionId": sid, "backend": backend, "hostId": host})] = {**item, "backend": backend, "hostId": host}
            merged["catalogue"] += [{**row, "backend": backend, "hostId": host} for row in result.get("catalogue", [])]
            merged["errors"] += result.get("errors", [])
        if self.local["claude-code"].root.joinpath("projects").is_dir():
            try:
                merged["catalogue"] += [{**row, "hostId": LOCAL_HOST} for row in self.local["claude-code"].catalogue()]
            except OSError as exc:
                merged["errors"].append("Claude Code catalogue: " + str(exc))
        merged["catalogue"] += [row for rows in list(self.remote_catalogue.values()) for row in rows]
        merged['observedAt'] = time.time()
        return merged

    def refresh_remote_catalogue(self):
        for host_id in list(self.hosts):
            rows = []
            for backend in BACKENDS:
                try:
                    rows += self.backend(backend, host_id).catalogue()
                except Exception:
                    continue
            self.remote_catalogue[host_id] = rows
        for host_id in set(self.remote_catalogue) - set(self.hosts):
            self.remote_catalogue.pop(host_id)

    def describe(self):
        return {"backends": [{"id": key, "label": BACKEND_TYPES[key].label, "capabilities": dict(BACKEND_TYPES[key].capabilities)}
                             for key in BACKENDS],
                "hosts": [{"id": LOCAL_HOST, "name": "This computer"}] + [{"id": h.id, "url": h.url} for h in self.hosts.values()]}


def tmux_launch(window, agent, command):
    """Open an agent in a named tmux session window; the POSIX counterpart of Windows Terminal tabs."""
    tmux = shutil.which("tmux")
    if not tmux:
        raise RuntimeError("tmux is required to open agents on this platform.")
    shell_command = " ".join(shlex.quote(part) for part in command)
    if subprocess.run([tmux, "has-session", "-t", window], capture_output=True).returncode:
        subprocess.run([tmux, "new-session", "-d", "-s", window, "-n", agent["name"], "-c", agent["directory"], shell_command], check=True)
        return True
    windows = subprocess.run([tmux, "list-windows", "-t", window, "-F", "#{window_name}"],
                             capture_output=True, text=True).stdout.split("\n")
    if agent["name"] in windows:
        return False
    subprocess.run([tmux, "new-window", "-t", window, "-n", agent["name"], "-c", agent["directory"], shell_command], check=True)
    return True
