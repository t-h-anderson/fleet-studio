"""Fleet Studio: a loopback-only browser app with no third-party runtime dependencies."""
import argparse
from collections import deque
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen
import uuid
import webbrowser

from studio_entrypoints import EntryPoints
from studio_deployments import Deployments
from workflow_spec import export_spec, metadata, validate_node, ID, REFS
from studio_roles import enrich_state, clean_design, refresh_roles, policy_proposal
from studio_control import Controls
from studio_triggers import Triggers, validate_triggers
from studio_workflow import populate_roles, populate_triggers, contract_problem
from studio_backlog import Backlog, clean_config
from studio_activity import activity
from studio_progress import Progress, clean_settings
from studio_telemetry import Telemetry
from studio_health import Health, clean_health_settings
from studio_automation import Automation, backtest, clean_alerts, clean_automations, clean_throttles
from studio_reconciliation import Reconciliation
from studio_goals import Goals
from studio_agents import Runtimes, clean_binding, clean_hosts, tmux_launch, LOCAL_HOST

from studio_bindings import session_key, same_binding, host_config

APP_ID = "codex-fleet-studio"
ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "web"
ROLE_DEFAULTS = {
    "Coordinator": ("Coordination", 480, 40),
    "Agent 1": ("Writer", 160, 240), "Agent 2": ("Writer", 480, 240),
    "Agent 3": ("Writer", 800, 240), "Integration prep": ("Preparation", 160, 450),
    "Integrator": ("Integration", 480, 450), "Audit": ("Review", 800, 450),
    "Material data": ("Data", 160, 660), "Repo cleanup": ("Operations", 800, 660),
}


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    pass


def identifier(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("A valid UUID is required.") from None


def text(value, label, limit=200, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError(label + " is missing or too long.")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise ValidationError(label + " contains invalid characters.")
    return value.strip()


def validate_fleets(value, host_ids=()):
    if not isinstance(value, list) or not 1 <= len(value) <= 30:
        raise ValidationError("Keep between 1 and 30 fleets.")
    output, fleet_ids = [], set()
    for raw in value:
        fid = identifier(raw.get("id"))
        if fid in fleet_ids:
            raise ValidationError("Fleet IDs must be unique.")
        fleet_ids.add(fid)
        window_name = text(raw.get("windowName"), "Terminal window name", 80)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", window_name):
            raise ValidationError("Terminal window names use letters, numbers, hyphens and underscores.")
        agents_raw = raw.get("agents", [])
        if not isinstance(agents_raw, list) or len(agents_raw) > 100:
            raise ValidationError("A fleet can contain at most 100 agents.")
        agents, agent_ids, names, session_ids = [], set(), set(), set()
        for agent in agents_raw:
            aid = identifier(agent.get("id"))
            name = text(agent.get("name"), "Agent name", 80)
            if any(ch in name for ch in ';"\n\t') or name.casefold() in names:
                raise ValidationError("Agent names must be unique and cannot contain quotes or semicolons.")
            if aid in agent_ids:
                raise ValidationError("Agent IDs must be unique within a fleet.")
            names.add(name.casefold())
            agent_ids.add(aid)
            session_id = identifier(agent["sessionId"]) if agent.get("sessionId") else ""
            try:
                backend, host = clean_binding(agent, host_ids)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            key = session_key({'sessionId': session_id, 'backend': backend, 'hostId': host})
            if session_id and key in session_ids:
                raise ValidationError("A session can only appear once in a fleet on the same backend and host.")
            if session_id:
                session_ids.add(key)
            directory = text(agent.get("directory", ""), "Folder", 1000, required=False)
            # A remote folder is checked by its own node, not on this computer.
            if host == LOCAL_HOST and directory and not Path(directory).is_dir():
                raise ValidationError("Folder does not exist: " + directory)
            if host == LOCAL_HOST and directory:
                directory = str(Path(directory).resolve())
            position = agent.get("position", {})
            coordinates = {}
            for axis in ("x", "y"):
                number = position.get(axis, 60)
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                    raise ValidationError("Board positions must be finite numbers.")
                coordinates[axis] = max(0, min(4000, round(number)))
            if not isinstance(agent.get("enabled", True), bool):
                raise ValidationError("Enabled must be true or false.")
            agents.append({
                "id": aid, "name": name, "sessionId": session_id, "directory": directory,
                "enabled": agent.get("enabled", True), "backend": backend, "hostId": host,
                "role": text(agent.get("role", "Agent"), "Role", 60),
                "notes": text(agent.get("notes", ""), "Notes", 4000, required=False),
                "position": coordinates,
            })
        links, seen_links, link_ids = [], set(), set()
        links_raw = raw.get("links", [])
        if not isinstance(links_raw, list) or len(links_raw) > 300:
            raise ValidationError("A fleet can contain at most 300 handoff links.")
        for link in links_raw:
            link_id = identifier(link.get("id"))
            if link_id in link_ids:
                raise ValidationError("Handoff IDs must be unique within a fleet.")
            link_ids.add(link_id)
            source, target = link.get("source"), link.get("target")
            if source not in agent_ids or target not in agent_ids or source == target:
                raise ValidationError("Handoff links need two different agents from this fleet.")
            pair = (source, target)
            if pair in seen_links:
                raise ValidationError("This handoff link already exists.")
            seen_links.add(pair)
            links.append({"id": link_id, "source": source, "target": target,
                          "label": text(link.get("label", ""), "Link label", 60, required=False)})
        try:
            design = clean_design(raw, agents)
            triggers = validate_triggers(raw.get("triggers", []), {r["id"] for r in design["roles"]}, agent_ids)
            backlog = clean_config(raw.get("backlog"), agent_ids, design.get("roleSource"))
            health = clean_health_settings(raw.get("health"))
            role_ids = {r["id"] for r in design["roles"]}
            automations = clean_automations(raw.get("automations"), role_ids)
            throttles = clean_throttles(raw.get("throttles"), role_ids)
            alerts = clean_alerts(raw.get("alerts"))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValidationError(str(exc)) from None
        candidate = {
            **design, "triggers": triggers, "backlog": backlog, "progress": clean_settings(raw.get("progress")),
            "health": health, "automations": automations, "throttles": throttles, "alerts": alerts,
            "id": fid, "name": text(raw.get("name"), "Fleet name", 80),
            "description": text(raw.get("description", ""), "Description", 600, required=False),
            "windowName": window_name, "agents": agents, "links": links,
        }
        for row in design["roles"] + design["roleLinks"] + triggers:
            if "specId" in row:
                validate_node(row["specId"], ID)
            if "sources" in row:
                validate_node(row["sources"], REFS)
        if raw.get("workflow") is not None:
            candidate["workflow"] = raw["workflow"]
            candidate["workflow"] = metadata(export_spec(candidate))
        output.append(candidate)
    return output


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # Windows scanners/readers can briefly deny replacement of a closed
        # file. Keep the old version intact and retry only transient sharing
        # failures, before reporting a persistent write failure to the caller.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4:
                    raise
                time.sleep(.025 * (2 ** attempt))
    finally:
        if temporary.exists():
            temporary.unlink()


def seed_state(legacy):
    fleet_id = str(uuid.uuid4())
    agents = []
    for index, old in enumerate(legacy["agents"]):
        role, x, y = ROLE_DEFAULTS.get(old["name"], ("Agent", 80 + (index % 3) * 320, 80 + (index // 3) * 210))
        agents.append({
            "id": str(uuid.uuid4()), "name": old["name"], "role": role, "notes": "",
            "directory": old["directory"], "sessionId": old["sessionId"],
            "enabled": old.get("enabled", True), "position": {"x": x, "y": y},
        })
    fleet = {"id": fleet_id, "name": "My fleet", "description": "Local development fleet",
             "windowName": legacy["windowName"], "agents": agents, "links": []}
    # Start with a spatial role layout. Handoff links are created explicitly in the UI.
    return {"version": 1, "revision": 1, "defaultFleetId": fleet_id, "fleets": [fleet]}


def windows_codex(agent):
    """The PowerShell launcher and fleet.json only cover local Codex agents."""
    return agent.get("backend", "codex") == "codex" and agent.get("hostId", LOCAL_HOST) == LOCAL_HOST


class Store:
    def __init__(self, root):
        self.root = root
        self.path = root / "fleet-studio.json"
        self.lock = threading.RLock()
        self.legacy = json.loads((root / "fleet.json").read_text(encoding="utf-8-sig")) if (root / "fleet.json").exists() else {"version": 1, "windowName": "fleet-studio", "codexPath": shutil.which("codex") or "codex", "agents": []}
        self.state = json.loads(self.path.read_text(encoding="utf-8-sig")) if self.path.exists() else seed_state(self.legacy)
        upgraded = enrich_state(self.state)
        if upgraded and self.path.exists():
            self.state["revision"] += 1
        if upgraded or not self.path.exists():
            atomic_json(self.path, self.state)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def save(self, payload):
        try:
            # Older clients omit hosts; keep the saved remote nodes rather than dropping them.
            hosts = clean_hosts(payload["hosts"] if "hosts" in payload else self.state.get("hosts", []))
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        fleets = validate_fleets(payload.get("fleets"), {h["id"] for h in hosts})
        default = identifier(payload.get("defaultFleetId"))
        if default not in {fleet["id"] for fleet in fleets}:
            raise ValidationError("Choose an existing default fleet.")
        with self.lock:
            if payload.get("revision") != self.state["revision"]:
                raise ConflictError("The fleet changed in another window. Reload before saving your changes.")
            state = {"version": 1, "revision": self.state["revision"] + 1,
                     "defaultFleetId": default, "fleets": fleets, "hosts": hosts}
            # Keep the existing one-click launcher aligned with the default fleet.
            default_fleet = next(fleet for fleet in fleets if fleet["id"] == default)
            compatible = {
                "version": 1, "windowName": default_fleet["windowName"],
                "codexPath": self.legacy["codexPath"],
                "agents": [{key: agent[key] for key in ("name", "directory", "sessionId", "enabled")}
                           for agent in default_fleet["agents"] if agent["sessionId"] and agent["directory"] and windows_codex(agent)],
            }
            previous_legacy = self.legacy
            atomic_json(self.root / "fleet.json", compatible)
            try:
                atomic_json(self.path, state)
            except OSError:
                atomic_json(self.root / "fleet.json", previous_legacy)
                raise
            self.legacy = compatible
            self.state = state
            return deepcopy(state)

    def fleet(self, fleet_id):
        with self.lock:
            match = next((fleet for fleet in self.state["fleets"] if fleet["id"] == fleet_id), None)
            if match is None:
                raise ValidationError("Fleet not found.")
            return deepcopy(match)

    def launch_config(self, fleet, agent_ids):
        selected = [agent for agent in fleet["agents"] if agent["id"] in agent_ids]
        if len(selected) != len(set(agent_ids)):
            raise ValidationError("One or more selected agents do not exist.")
        if not selected:
            raise ValidationError("Select at least one agent.")
        for agent in selected:
            if not agent["enabled"] or not agent["sessionId"] or not agent["directory"]:
                raise ValidationError(agent["name"] + " needs an enabled session and folder before launch.")
            if agent.get("hostId", LOCAL_HOST) == LOCAL_HOST and not Path(agent["directory"]).is_dir():
                raise ValidationError("Folder no longer exists: " + agent["directory"])
        return {
            "version": 1, "windowName": fleet["windowName"], "codexPath": self.legacy["codexPath"],
            "agents": [{key: agent[key] for key in ("name", "directory", "sessionId", "enabled")} for agent in selected],
            "bindings": [{**{key: agent.get(key, default) for key, default in (("id", ""), ("name", ""), ("directory", ""), ("sessionId", ""),
                          ("backend", "codex"), ("hostId", LOCAL_HOST))}, "hostConfig": host_config(agent, self.state.get('hosts', []))} for agent in selected],
        }


class Application:
    def __init__(self, root, launch_enabled=True, backlog_fixture=None, activity_fixture=None):
        self.root = root
        self.store = Store(root)
        self.token = secrets.token_urlsafe(32)
        self.instance_id = uuid.uuid4().hex
        self.root_id = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        self.codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.windows = os.name == "nt"
        self.runtimes = Runtimes(root, self.store.legacy["codexPath"], self.codex_root)
        self.runtimes.configure(self.store.state.get("hosts", []))
        self.remote_catalogue_at = 0
        self.remote_catalogue_thread = None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.events = deque(maxlen=120)
        self.jobs = {}
        self.launch_enabled = launch_enabled
        self.monitor = {"observedAt": None, "sessions": {}, "catalogue": [], "errors": []}
        self.deployments = Deployments(self)
        self.entrypoints = EntryPoints(self)
        self.controls = Controls(self)
        self.triggers = Triggers(self)
        self.progress = Progress(self)
        self.telemetry = Telemetry(self)
        self.health = Health(self)
        self.automation = Automation(self)
        self.goals = Goals(self)
        self.reconciliation = Reconciliation(self)
        from studio_context import CurrentContext
        self.context = CurrentContext(self)
        self.backlog = Backlog(self, backlog_fixture)
        self.activity_fixture = activity_fixture
        self.event("Fleet Studio opened", "Your saved fleets are ready.", None)

    def runtime_fleet(self, fleet):
        return self.deployments.runtime_fleet(fleet)

    def event(self, title, detail, fleet_id):
        with self.lock:
            self.events.appendleft({"id": uuid.uuid4().hex, "time": time.time(),
                                    "title": title, "detail": detail, "fleetId": fleet_id})

    def monitor_loop(self):
        while not self.stop.is_set():
            state = self.store.snapshot()
            try:
                self.runtimes.configure(state.get("hosts", []))
                if self.runtimes.hosts and time.time() - self.remote_catalogue_at > 60 and not (self.remote_catalogue_thread and self.remote_catalogue_thread.is_alive()):
                    # Remote catalogues only help link sessions, so refresh them less often.
                    self.remote_catalogue_at = time.time()
                    self.remote_catalogue_thread = threading.Thread(target=self.runtimes.refresh_remote_catalogue, daemon=True)
                    self.remote_catalogue_thread.start()
                latest = self.runtimes.observe([agent for fleet in state["fleets"] for agent in fleet["agents"]])
                with self.lock:
                    previous = self.monitor["sessions"]
                    self.monitor = latest
                self.telemetry.tick(state)
                self.health.tick(state, latest)
                self.automation.tick(state, latest)
                self.triggers.tick(state, self.status())
                self.progress.tick(state, self.status())
                for fleet in state["fleets"]:
                    for agent in fleet["agents"]:
                        sid = session_key(agent)
                        before, after = previous.get(sid), latest["sessions"].get(sid)
                        if before and after and before["status"] != after["status"]:
                            self.event(agent["name"] + " · " + after["status"], fleet["name"], fleet["id"])
            except Exception as exc:
                logging.exception("Monitor refresh failed")
                with self.lock:
                    self.monitor["errors"] = ["Status refresh failed: " + str(exc)]
            self.stop.wait(5)

    def status(self):
        with self.lock:
            result = deepcopy({"observedAt": self.monitor["observedAt"],
                               "sessions": self.monitor["sessions"], "errors": self.monitor["errors"],
                               "events": list(self.events), "jobs": list(self.jobs.values())[-15:]})
        result["controls"] = self.controls.snapshot()
        result["automation"] = self.triggers.snapshot()
        result["workflowProblems"] = {r["id"]: contract_problem(f, r) for f in self.store.snapshot()["fleets"] for r in f.get("triggers", []) if r.get("workflowLinkId")}
        return result

    def catalogue(self):
        with self.lock:
            return deepcopy(self.monitor["catalogue"])

    def launch(self, payload):
        # Serialize scheduling with reviewed goal controls, including the job reservation.
        with self.store.lock:
            return self._launch(payload)

    def _launch(self, payload):
        if not self.launch_enabled:
            raise ValidationError("Launching is disabled in this test instance.")
        fleet = self.store.fleet(payload.get("fleetId"))
        ids = payload.get("agentIds")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValidationError("Select agents to launch.")
        config = self.store.launch_config(fleet, ids)
        with self.lock:
            if any(job["status"] == "running" for job in self.jobs.values()):
                raise ConflictError("A launch is already in progress. Wait for it to finish.")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "status": "running", "fleetId": fleet["id"],
                                 "count": len(ids), "startedAt": time.time(), "message": "Opening agent terminals..."}
        threading.Thread(target=self._run_launch, args=(job_id, fleet, config), daemon=True).start()
        return deepcopy(self.jobs[job_id])

    def _run_launch(self, job_id, fleet, config):
        try:
            bindings = config.get("bindings", [])
            legacy = [b for b in bindings if self.windows and windows_codex(b)]
            opened, failures = 0, []
            if legacy:
                names = {b["name"] for b in legacy}
                opened, failure = self._launch_powershell(job_id, {**{k: v for k, v in config.items() if k != "bindings"},
                    "agents": [a for a in config["agents"] if a["name"] in names]})
                failures += [failure] if failure else []
            for binding in bindings:
                if binding in legacy:
                    continue
                try:
                    opened += int(self._launch_agent(fleet, binding))
                except Exception as exc:
                    failures.append(binding["name"] + ": " + str(exc)[-600:])
            if failures:
                status, message = "failed", "Launch failed. Check that the agent CLI and terminal are installed."
                detail = "\n".join(failures)[-1800:]
            else:
                status = "completed"
                message = str(opened) + " tab(s) opened. " + str(len(bindings) - opened) + " already open."
                detail = message
            with self.lock:
                self.jobs[job_id].update(status=status, message=message, detail=detail, finishedAt=time.time())
            self.event("Fleet launch " + status, fleet["name"] + " · " + message, fleet["id"])
        except Exception as exc:
            logging.exception("Launch failed")
            with self.lock:
                self.jobs[job_id].update(status="failed", message=str(exc), finishedAt=time.time())
            self.event("Launch needs attention", str(exc), fleet["id"])

    def _launch_powershell(self, job_id, config):
        """Existing Windows Terminal launcher for local Codex agents, with its mutex and lock checks."""
        request_root = self.root / ".launches"
        request_path = request_root / (job_id + ".json")
        request_root.mkdir(exist_ok=True)
        atomic_json(request_path, config)
        shell = str(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        command = [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(ROOT / "Start-CodexFleet.ps1"), "-ConfigPath", str(request_path)]
        # The scripts ship with Studio; only the request snapshot lives in the data root.
        run = subprocess.run(command, capture_output=True, text=True, timeout=180,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=ROOT)
        (request_root / (job_id + ".log")).write_text(run.stdout + run.stderr, encoding="utf-8")
        match = re.search(r"Opened (\d+) tab", run.stdout)
        failure = (run.stderr or run.stdout).strip()[-1800:] if run.returncode else ""
        return (int(match.group(1)) if match else 0), failure

    def _launch_agent(self, fleet, binding):
        """Open one agent interactively. Returns False when it is already open."""
        with self.store.lock:
            current = self.store.fleet(fleet['id'])
            agent = next((a for a in current['agents'] if a['id'] == binding['id']), None)
            if not agent or not agent['enabled'] or not same_binding(agent, binding) or host_config(agent, self.store.state.get('hosts', [])) != binding.get('hostConfig'):
                raise ValueError('Agent or host binding changed before launch. Review and open it again.')
            return self._open_bound_agent(fleet, binding)

    def _open_bound_agent(self, fleet, binding):
        from studio_agents import RemoteBackend, RemoteHost
        backend = RemoteBackend(RemoteHost(binding['hostConfig']), binding['backend']) if binding.get('hostConfig') else self.runtimes.for_agent(binding)
        if binding["hostId"] != LOCAL_HOST:
            result = backend.host.call("/node/launch", {"backend": binding["backend"], "sessionId": binding["sessionId"],
                                                        "directory": binding["directory"], "name": binding["name"],
                                                        "window": fleet["windowName"]})
            return bool(result.get("opened"))
        if not backend.ready_to_launch(binding['sessionId']):
            return False
        command = backend.resume_command(binding["sessionId"], binding["directory"])
        if self.windows:
            subprocess.run(["wt.exe", "-w", fleet["windowName"], "new-tab", "--title", binding["name"],
                            "-d", binding["directory"], *command], check=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        return tmux_launch(fleet["windowName"], binding, command)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application):
        self.application = application
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info("%s " + fmt, self.address_string(), *args)

    def same_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in ("127.0.0.1:" + str(port), "localhost:" + str(port))

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self.same_host():
            return self.json(403, {"error": "Local access only."})
        app = self.server.application
        path = urlsplit(self.path).path
        try:
            if path == "/api/health":
                return self.json(200, {"appId": APP_ID, "rootId": app.root_id, "instanceId": app.instance_id})
            if path == "/api/bootstrap":
                return self.json(200, {"token": app.token, "state": app.store.snapshot(),
                                      "status": app.status(), "sessions": app.catalogue(),
                                      "runtimes": app.runtimes.describe()})
            if path == "/api/status":
                return self.json(200, app.status())
            if path == "/api/activity":
                query = parse_qs(urlsplit(self.path).query)
                fleet = app.store.fleet(query.get("fleetId", [None])[0])
                agent = next((a for a in fleet["agents"] if a["id"] == query.get("agentId", [None])[0]), None)
                if not agent:
                    return self.json(404, {"error": "Agent not found in this fleet."})
                if not agent["sessionId"]:
                    return self.json(200, {"messages": [], "error": "Link an agent session to see this agent's activity."})
                if app.activity_fixture:
                    return self.json(200, activity(app.codex_root, agent["sessionId"], app.activity_fixture))
                try:
                    return self.json(200, app.runtimes.for_agent(agent).activity(agent["sessionId"]))
                except (RuntimeError, TimeoutError) as exc:
                    return self.json(200, {"messages": [], "error": str(exc)})
            if path in ("/api/progress", "/api/progress/history"):
                query = parse_qs(urlsplit(self.path).query)
                fid = query.get("fleetId", [None])[0]
                if path.endswith("history"):
                    return self.json(200, app.progress.history(fid, query.get("taskUrl", [None])[0], query.get("agentId", [None])[0], query.get("before", [None])[0]))
                return self.json(200, app.progress.snapshot(fid))
            if path == "/api/telemetry/turns":
                query = parse_qs(urlsplit(self.path).query)
                fleet = app.store.fleet(query.get("fleetId", [None])[0])
                agent_id = query.get("agentId", [None])[0]
                return self.json(200, {"turns": app.telemetry.turns(fleet["id"], agent_id, limit=100)})
            if path == "/api/health/agents":
                fleet_id = parse_qs(urlsplit(self.path).query).get("fleetId", [None])[0]
                return self.json(200, app.health.snapshot(fleet_id))
            if path == "/api/automation":
                fleet_id = parse_qs(urlsplit(self.path).query).get("fleetId", [None])[0]
                app.store.fleet(fleet_id)
                return self.json(200, app.automation.snapshot(fleet_id))
            if path == "/api/backlog":
                fleet_id = parse_qs(urlsplit(self.path).query).get("fleetId", [None])[0]
                return self.json(200, app.backlog.snapshot(fleet_id))
            if path == "/api/sessions":
                return self.json(200, {"sessions": app.catalogue()})
            if path in ("/", "/app.js", "/roles.js", "/layout.js", "/backlog.js", "/activity.js", "/workflow.js", "/progress.js", "/metrics.js", "/reconciliation.js", "/goals.js", "/app.css", "/icon.svg"):
                name = "index.html" if path == "/" else path[1:]
                mime = {".html": "text/html", ".js": "application/javascript",
                        ".css": "text/css", ".svg": "image/svg+xml"}[Path(name).suffix]
                return self.send_bytes(200, (PUBLIC / name).read_bytes(), mime + "; charset=utf-8")
            return self.json(404, {"error": "Not found."})
        except ValidationError as exc:
            return self.json(400, {"error": str(exc)})
        except (OSError, ValueError):
            logging.exception("Read failed")
            return self.json(500, {"error": "Unable to read application data. See the local log."})

    def do_POST(self):
        app = self.server.application
        if not self.same_host():
            return self.json(403, {"error": "Local access only."})
        origin = self.headers.get("Origin")
        allowed = ("http://127.0.0.1:" + str(self.server.server_port),
                   "http://localhost:" + str(self.server.server_port))
        if origin and origin not in allowed:
            return self.json(403, {"error": "Origin rejected."})
        if not secrets.compare_digest(self.headers.get("X-Fleet-Token", ""), app.token):
            return self.json(403, {"error": "Refresh Fleet Studio before making changes."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValidationError("Invalid request size.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValidationError("Expected a JSON object.")
            path = urlsplit(self.path).path
            if path == '/api/context':
                return self.json(200, app.context.page(payload))
            if path == '/api/context/handoff':
                return self.json(200, app.context.handoff(payload))
            if path == '/api/context/retire/preview':
                return self.json(200, app.context.preview_retirement(payload))
            if path == '/api/context/retire/apply':
                return self.json(200, app.context.retire(payload.get('previewId')))
            if path == "/api/automation/acknowledge":
                return self.json(200, app.automation.acknowledge(payload.get("fleetId"), payload.get("escalationId")))
            if path == "/api/automation/resume":
                fleet = app.store.fleet(payload.get("fleetId"))
                app.automation.resume(fleet["id"], payload.get("agentId"))
                return self.json(200, app.automation.snapshot(fleet["id"]))
            if path == "/api/automation/backtest":
                fleet = app.store.fleet(payload.get("fleetId"))
                role_ids = {r["id"] for r in fleet.get("roles", [])}
                try:
                    [rule] = clean_automations([payload.get("rule")], role_ids)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise ValidationError(str(exc)) from None
                hours = payload.get("hours", 24)
                if type(hours) is not int or not 1 <= hours <= 720:
                    raise ValidationError("Backtest window must be 1 to 720 hours.")
                agents = [a["id"] for a in fleet["agents"] if not rule["roleId"] or rule["roleId"] in a.get("roleIds", [])]
                history = [h for h in app.health.history(fleet["id"], time.time() - hours * 3600 - 86400) if h["agentId"] in agents]
                return self.json(200, backtest(rule, history, time.time(), hours))
            if path == "/api/progress/report":
                app.progress.observe_handoffs(app.triggers.snapshot().get("actions", []))
                return self.json(200, app.progress.report(payload))
            if path == '/api/goals/link':
                return self.json(200, app.goals.link_metadata(payload))
            if path == '/api/goals/preview':
                return self.json(200, app.goals.preview(payload))
            if path == '/api/goals/apply':
                return self.json(200, app.goals.apply(payload.get('previewId')))
            if path == "/api/workflow/entrypoints/preview":
                return self.json(200, app.entrypoints.preview(payload))
            if path == "/api/workflow/entrypoints/apply":
                return self.json(200, app.entrypoints.apply(payload.get("previewId")))
            if path == "/api/workflow/inspect":
                return self.json(200, app.deployments.overview(app.store.fleet(payload.get("fleetId"))))
            if path == "/api/workflow/preview":
                return self.json(200, app.deployments.preview(payload))
            if path == "/api/workflow/apply":
                return self.json(200, app.deployments.apply(payload.get("previewId")))
            if path == "/api/workflow/pause":
                return self.json(200, app.deployments.pause(payload))
            if path == "/api/workflow/notify":
                return self.json(200, app.deployments.notification(payload))
            if path == "/api/workflow/acknowledge":
                return self.json(200, app.deployments.acknowledge(payload))
            if path == "/api/state":
                state = app.store.save(payload)
                app.runtimes.configure(state.get("hosts", []))
                app.event("Fleet configuration saved", str(len(state["fleets"])) + " fleet(s)", None)
                return self.json(200, {"state": state})
            if path in ("/api/workflow/populate", "/api/workflow/roles"):
                state = app.store.snapshot()
                if payload.get("revision") != state["revision"]:
                    raise ConflictError("Fleet changed. Reload before populating the workflow.")
                fleet = next((f for f in state["fleets"] if f["id"] == payload.get("fleetId")), None)
                if not fleet:
                    raise ValidationError("Fleet not found.")
                report = populate_triggers(fleet) if path.endswith("populate") else {"roles": populate_roles(fleet)}
                return self.json(200, {"state": app.store.save(state), "report": report})
            if path == "/api/backlog/refresh":
                return self.json(200, app.backlog.snapshot(payload.get("fleetId"), force=True))
            if path == "/api/backlog/history":
                return self.json(200, app.backlog.history(payload.get("fleetId"), payload.get("iid")))
            if path == "/api/control/preview":
                return self.json(200, self.server.application.controls.preview(payload))
            if path == "/api/control/send":
                return self.json(202, self.server.application.controls.send(payload.get("previewId")))
            if path == "/api/roles/refresh":
                state = app.store.snapshot()
                if payload.get("revision") != state["revision"]:
                    raise ConflictError("Fleet changed. Reload before refreshing roles.")
                fleet = next((f for f in state["fleets"] if f["id"] == payload.get("fleetId")), None)
                if not fleet:
                    raise ValidationError("Fleet not found.")
                if fleet.get("workflow"):
                    raise ValidationError("This workflow is managed in Studio. Review a workflow import to update its source policy.")
                refresh_roles(fleet)
                return self.json(200, {"state": app.store.save(state)})
            if path == "/api/roles/proposal":
                return self.json(200, policy_proposal(app.store.fleet(payload.get("fleetId"))))
            if path == "/api/events":
                return self.json(202, app.triggers.report(payload))
            if path == '/api/events/preview':
                return self.json(200, app.triggers.report(payload, preview=True))
            if path == "/api/triggers/review":
                return self.json(200, app.triggers.review(payload.get("actionId")))
            if path == "/api/triggers/dismiss":
                return self.json(200, app.triggers.dismiss(payload.get("actionId")))
            if path == "/api/handoffs/reconcile/preview":
                return self.json(200, app.reconciliation.preview(payload))
            if path == "/api/handoffs/reconcile/apply":
                return self.json(200, app.reconciliation.apply(payload.get("previewId")))
            if path == "/api/launch":
                return self.json(202, {"job": app.launch(payload)})
            if path == "/api/shutdown":
                self.json(200, {"ok": True})
                app.stop.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.json(404, {"error": "Not found."})
        except ConflictError as exc:
            return self.json(409, {"error": str(exc)})
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return self.json(400, {"error": str(exc)})
        except Exception:
            logging.exception("Write failed")
            return self.json(500, {"error": "The operation failed. See the local log."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", type=Path, default=ROOT)
    parser.add_argument("--no-launch", action="store_true", help="Disable real agent launches for UI tests.")
    parser.add_argument("--backlog-fixture", type=Path, help="Use synthetic issue data for isolated browser tests.")
    parser.add_argument("--activity-fixture", type=Path, help="Use synthetic transcript messages for browser tests.")
    args = parser.parse_args()
    args.data_root.mkdir(parents=True, exist_ok=True)
    runtime = args.data_root / ".runtime"
    runtime.mkdir(exist_ok=True)
    logging.basicConfig(filename=runtime / "studio.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    root_id = hashlib.sha256(str(args.data_root.resolve()).encode()).hexdigest()[:16]
    for port in range(args.port, args.port + 10):
        url = "http://127.0.0.1:" + str(port)
        try:
            with urlopen(url + "/api/health", timeout=0.5) as response:
                health = json.load(response)
            if health.get("appId") == APP_ID and health.get("rootId") == root_id:
                if args.open:
                    webbrowser.open(url)
                return
        except (OSError, ValueError):
            pass
        app = Application(args.data_root.resolve(), launch_enabled=not args.no_launch, backlog_fixture=args.backlog_fixture, activity_fixture=args.activity_fixture)
        try:
            server = Server(("127.0.0.1", port), app)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("No free Fleet Studio port found.")
    atomic_json(runtime / "server.json", {"url": url, "pid": os.getpid(), "instanceId": app.instance_id})
    threading.Thread(target=app.monitor_loop, daemon=True).start()
    if args.open:
        webbrowser.open(url)
    logging.info("Fleet Studio serving %s", url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        app.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
