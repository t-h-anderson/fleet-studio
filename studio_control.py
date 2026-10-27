"""Explicit operator controls using each agent backend's supported commands, never native DB writes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid
from studio_bindings import runtime_binding, same_binding, host_config
from studio_agents import NotReady

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AutomationHeld(ValueError):
    """A final availability check deferred delivery before any native effect."""


def rename_session(codex, session_id, name):
    """Set persisted metadata through app-server; never load/resume a thread."""
    process = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
    responses = queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                try:
                    responses.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            responses.put(None)
    threading.Thread(target=reader, daemon=True).start()
    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
    def response(request_id):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            value = responses.get(timeout=max(.1, deadline - time.monotonic()))
            if value is None:
                raise RuntimeError("Codex app-server closed before acknowledging the rename.")
            if value.get("id") == request_id:
                if "error" in value:
                    raise RuntimeError(value["error"].get("message", "Codex rejected the rename."))
                return value.get("result")
        raise TimeoutError("Codex did not acknowledge the rename.")
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "fleet_studio", "title": "Fleet Studio", "version": "2.0.0"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "thread/name/set", "params": {"threadId": session_id, "name": name}})
        response(2)
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()


def assignment_text(fleet, agent):
    roles = [role for role in fleet.get("roles", []) if role["id"] in agent.get("roleIds", [])]
    if not roles:
        raise ValueError(agent["name"] + " has no assigned shared role.")
    lines = ["Fleet Studio assignment for " + agent["name"] + ".", "",
             "Apply this assignment at the next safe task boundary. Preserve explicit stops, current file custody, source permissions and project policy. A role assignment does not transfer another agent's checkout or runtime. Record task ownership and handoffs through the project's normal ticket/MR workflow.", ""]
    source = fleet.get("roleSource")
    if source:
        lines += ["Committed role-policy reference: " + source["url"], ""]
    if source:
        from studio_roles import git, parse_roles, POLICY_PATH
        originals = {r["name"]: r for r in parse_roles(git(source["repoDirectory"], "show", source["commit"] + ":" + POLICY_PATH))}
    for role in roles:
        baseline = role.get("baseline")
        if source and (not baseline or baseline != originals.get(baseline["name"])):
            raise ValueError(role["name"] + " is not linked to the committed policy. Prepare/publish the policy proposal before applying it.")
        if baseline and any(role[k] != baseline[k] for k in ("name", "responsibilities", "authority")):
            raise ValueError(role["name"] + " has unpublished policy edits. Prepare/reconcile the policy proposal before applying this role.")
        lines += ["Role: " + role["name"], "Responsibilities: " + role["responsibilities"],
                  "Authority and handoff: " + role["authority"], ""]
    # The checks above keep committed policy authoritative; the message itself is the agent's brief.
    from workflow_spec import brief_document, export_spec
    return "[Fleet Studio] Your brief (draft, not yet deployed):\n\n" + brief_document(fleet, export_spec(fleet), agent, "draft", Path(__file__).resolve().parent)


class Controls:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.previews = {}
        self.receipt_path = app.root / ".runtime" / "controls.json"
        self.receipts = {}
        if self.receipt_path.exists():
            self.receipts = json.loads(self.receipt_path.read_text(encoding="utf-8"))
            for receipt in self.receipts.values():
                if receipt["status"] == "sending":
                    receipt["status"] = "unknown"
                    receipt["detail"] = "App restarted during dispatch. Check the session before sending again."
    def snapshot(self, full=False):
        with self.lock:
            rows = list(self.receipts.values())
            return deepcopy(rows if full else rows[-30:])
    def existing_preview(self, preview_id):
        revision = self.app.store.snapshot()["revision"]
        with self.lock:
            preview = self.previews.get(preview_id)
            if preview and preview["createdAt"] > time.time() - 600 and preview["revision"] == revision:
                return deepcopy(preview)
            if preview:
                self.previews.pop(preview_id, None)
            return None
    def persist(self):
        from fleet_studio import atomic_json
        self.receipt_path.parent.mkdir(exist_ok=True)
        atomic_json(self.receipt_path, self.receipts)
    def preview(self, payload):
        state = self.app.store.snapshot()
        self.app.runtimes.configure(state.get('hosts', []))
        fleet = next((f for f in state["fleets"] if f["id"] == payload.get("fleetId")), None)
        if fleet is None:
            raise ValueError("Fleet not found.")
        action = payload.get("action", "assignment")
        if action not in ("assignment", "message", "rename"):
            raise ValueError("Unknown control action.")
        ids = payload.get("agentIds", [])
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(v, str) for v in ids):
            raise ValueError("Choose agents for this action.")
        agents = [agent for agent in fleet["agents"] if agent["id"] in ids]
        if len(agents) != len(set(ids)):
            raise ValueError("An agent no longer exists.")
        message = payload.get("message", "")
        if action == "message" and (not isinstance(message, str) or not message.strip() or len(message) > 16000):
            raise ValueError("Enter a message of at most 16000 characters.")
        if action == "message" and message.lstrip().startswith(("/", "!")):
            raise ValueError("Use plain instructions here, not slash or shell commands.")
        if action == "assignment" and fleet.get("workflow"):
            return self.app.deployments.notification({"fleetId": fleet["id"], "agentIds": ids})
        targets = []
        for agent in agents:
            if not agent["sessionId"]:
                raise ValueError(agent["name"] + " needs a linked session.")
            if action == "rename" and not self.app.runtimes.for_agent(agent).capabilities["rename"]:
                raise ValueError(agent["name"] + " uses a backend whose sessions cannot be renamed from Studio.")
            body = assignment_text(fleet, agent) if action == "assignment" else agent["name"] if action == "rename" else message.strip()
            # Windows has a finite command-line length; queue is an argument-based CLI.
            if len(body) > 16000:
                raise ValueError("This assignment is too long to send. Shorten notes or send roles separately.")
            targets.append({"agentId": agent["id"], "name": agent["name"], "sessionId": agent["sessionId"], "text": body,
                            "directory": agent['directory'], **runtime_binding(agent),
                            "hostConfig": host_config(agent, state.get('hosts', [])),
                            "effect": 'Starts a headless Claude turn after the session is closed; this is execution, not queued delivery.' if agent.get('backend') == 'claude-code' else 'Queues a message for the existing Codex session.'})
        preview = {"id": uuid.uuid4().hex, "fleetId": fleet["id"], "action": action,
                   "revision": state["revision"], "createdAt": time.time(), "targets": targets,
                   **({"deploymentId": payload["deploymentId"], "automation": payload.get("automation", False), "sourceAgentId": payload.get("sourceAgentId"), "idleOnly": payload.get("idleOnly", False), "purpose": payload.get("purpose", "")} if payload.get("deploymentId") else
                      {"automation": True, "purpose": payload.get("purpose", "")} if payload.get("automation") is True else {})}
        if 'assignmentContract' in payload:
            from studio_contracts import validate, delivery_hold
            contract = validate(payload['assignmentContract'], self.app, fleet, check_base=True, revalidated=True)
            if action != 'message' or ids != [contract['agentId']]:
                raise ValueError('Contract delivery must target its single named recipient.')
            reason = delivery_hold(self.app, fleet['id'], contract)
            if reason:
                raise ValueError(reason)
            preview['assignmentContract'] = contract
        if payload.get('handoffId'):
            from studio_recovery import replay_hold
            reason = replay_hold(self.app, fleet['id'], payload['handoffId'])
            if reason:
                raise ValueError(reason)
            preview['handoffId'] = payload['handoffId']
        if payload.get('continuationOf'):
            from studio_continuations import hold
            if not preview.get('assignmentContract') or not preview.get('automation'):
                raise ValueError('A continuation requires its authorized automatic assignment contract.')
            reason = hold(self.app, fleet['id'], preview['assignmentContract'], payload['continuationOf'])
            if reason:
                raise ValueError(reason)
            preview['continuationOf'] = payload['continuationOf']
        if payload.get('taskUrl'):
            from studio_progress import plain
            preview['taskUrl'] = plain(payload['taskUrl'], 'taskUrl', 1000)
            if not preview.get('assignmentContract'):
                from studio_contracts import untyped_hold
                for target in targets:
                    reason = untyped_hold(self.app, fleet['id'], target['agentId'], preview['taskUrl'])
                    if reason:
                        raise ValueError(reason)
        with self.lock:
            self.previews = {key: value for key, value in self.previews.items() if value["createdAt"] > time.time() - 600}
            self.previews[preview["id"]] = preview
        return deepcopy(preview)
    def send(self, preview_id):
        if not self.app.launch_enabled:
            raise ValueError("Agent controls are disabled in this test instance.")
        with self.app.store.lock, self.lock:
            current_revision = self.app.store.snapshot()["revision"]
            if preview_id in self.receipts:
                return deepcopy(self.receipts[preview_id])
            preview = self.previews.get(preview_id)
            if not preview or preview["createdAt"] < time.time() - 600:
                raise ValueError("Preview expired. Review this action again.")
            if preview["revision"] != current_revision:
                raise ValueError("Fleet configuration changed. Preview the action again.")
            if preview.get("deploymentId") and preview["deploymentId"] != self.app.deployments.data["active"].get(preview["fleetId"]):
                raise ValueError("The workflow deployment changed. Review this notification again.")
            reason = self.automatic_hold(preview)
            if reason:
                raise AutomationHeld(reason)
            if preview.get("purpose") == "workflow-adoption":
                rid = preview["deploymentId"]
                reserved = {t["agentId"] for c in self.receipts.values() if c.get("deploymentId") == rid and c.get("purpose") == "workflow-adoption"
                            for t in c["targets"] if t["status"] in ("waiting", "queued", "started", "unknown")}
                reserved |= {a["agentId"] for a in self.app.deployments.data["acks"] if a["releaseId"] == rid}
                preview = deepcopy(preview)
                preview["targets"] = [t for t in preview["targets"] if t["agentId"] not in reserved]
                if not preview["targets"]:
                    raise ValueError("These agents were already notified or acknowledged this release. Check existing receipts.")
            receipt = {**{k: preview[k] for k in ("deploymentId", "automation", "purpose") if k in preview}, "id": preview_id, "fleetId": preview["fleetId"], "action": preview["action"],
                       "status": "sending", "createdAt": time.time(),
                       "targets": [{"agentId": t["agentId"], "sessionId": t["sessionId"], "name": t["name"], **runtime_binding(t), "status": "waiting"} for t in preview["targets"]]}
            self.receipts[preview_id] = receipt
            self.persist()
            threading.Thread(target=self._dispatch, args=(deepcopy(preview),), daemon=True).start()
            return deepcopy(receipt)

    def automatic_hold(self, preview, target=None):
        if preview.get('handoffId'):
            from studio_recovery import replay_hold
            reason = replay_hold(self.app, preview['fleetId'], preview['handoffId'])
            if reason:
                return reason
        if preview.get('continuationOf'):
            from studio_continuations import hold
            reason = hold(self.app, preview['fleetId'], preview['assignmentContract'], preview['continuationOf'])
            if reason:
                return reason
        if preview.get('assignmentContract'):
            from studio_contracts import delivery_hold
            reason = delivery_hold(self.app, preview['fleetId'], preview['assignmentContract'])
            if reason:
                return reason
        elif preview.get('taskUrl'):
            from studio_contracts import untyped_hold
            for recipient in ([target] if target else preview['targets']):
                reason = untyped_hold(self.app, preview['fleetId'], recipient['agentId'], preview['taskUrl'])
                if reason:
                    return reason
        if not preview.get("automation"):
            return ""
        if self.app.deployments.data["paused"].get(preview["fleetId"]):
            return "Workflow automation is paused."
        fleet = self.app.runtime_fleet(self.app.store.fleet(preview["fleetId"]))
        targets = [target] if target else preview["targets"]
        agents = []
        for selected in targets:
            agent = next((a for a in fleet["agents"] if a["id"] == selected["agentId"]), None)
            if not agent or not agent["enabled"] or not same_binding(agent, selected):
                return "Workflow recipient was excluded or rebound."
            agents.append(agent)
        # Do not call app.status(): that acquires the trigger lock while a
        # trigger may be waiting for this control's lock. Only observations
        # are needed here, and our own waiting receipt is excluded.
        with self.app.lock:
            monitor = deepcopy(self.app.monitor)
        # Adoption acknowledgements no longer gate automation: agents read their brief when it changes.
        # Workflow deliveries and new-work automation sends are throttled; recovery sends are not.
        new_work = preview.get("purpose") == "automation-new-work" or bool(preview.get("deploymentId")) and not preview.get("purpose")
        return (self.app.triggers.hold_reason(agents, monitor, preview.get("idleOnly", False), preview["id"])
                or self.app.automation.hold(fleet["id"], agents, monitor, throttle=new_work))

    def _dispatch(self, preview):
        for index, target in enumerate(preview["targets"]):
            status, detail = "failed", "Not dispatched."
            try:
                if preview.get("deploymentId"):
                    if preview["deploymentId"] != self.app.deployments.data["active"].get(preview["fleetId"]):
                        raise ValueError("Workflow deployment changed before dispatch.")
                    if preview.get("automation") and self.app.deployments.data["paused"].get(preview["fleetId"]):
                        raise AutomationHeld("Workflow automation was paused before dispatch.")
                if preview.get("automation") or preview.get("purpose") == "workflow-adoption":
                    live = self.app.runtime_fleet(self.app.store.fleet(preview["fleetId"]))
                    recipient = next((a for a in live["agents"] if a["id"] == target["agentId"]), None)
                    if not recipient or not recipient["enabled"] or recipient["sessionId"] != target["sessionId"]:
                        raise ValueError("Workflow recipient was excluded or rebound before dispatch.")
                with self.app.store.lock, self.lock:
                    if preview.get('deploymentId') and preview['deploymentId'] != self.app.deployments.data['active'].get(preview['fleetId']):
                        raise ValueError('Workflow deployment changed before dispatch.')
                    fleet = self.app.store.fleet(preview['fleetId'])
                    agent = next((a for a in fleet['agents'] if a['id'] == target['agentId']), None)
                    if not agent or not agent['enabled'] or not same_binding(agent, target) or host_config(agent, self.app.store.snapshot().get('hosts', [])) != target.get('hostConfig'):
                        raise ValueError('Agent or host binding changed before dispatch. Review again.')
                    reason = self.automatic_hold(preview, target)
                    if reason:
                        raise AutomationHeld(reason)
                    # Freeze the reviewed destination through the effect; never reroute a queued control.
                    from studio_agents import RemoteBackend, RemoteHost
                    backend = RemoteBackend(RemoteHost(target['hostConfig']), target.get('backend', 'codex')) if target.get('hostConfig') else self.app.runtimes.for_agent(target)
                    if preview['action'] == 'rename':
                        status, detail = backend.rename(target['sessionId'], target['text'])
                    else:
                        status, detail = backend.send(target['sessionId'], target['text'], target['directory'])
            except (AutomationHeld, NotReady) as exc:
                status, detail = "held", str(exc)
            except (subprocess.TimeoutExpired, TimeoutError, queue.Empty):
                status, detail = "unknown", "No acknowledgement received. Check the session before retrying; the message may have been accepted."
            except Exception as exc:
                status, detail = "failed", str(exc)
            with self.lock:
                self.receipts[preview["id"]]["targets"][index].update(status=status, detail=detail, acknowledgedAt=time.time())
                self.persist()
        with self.lock:
            receipt = self.receipts[preview["id"]]
            receipt["status"] = "completed" if all(t["status"] in ("queued", "renamed", "started") for t in receipt["targets"]) else "needs-attention"
            receipt["finishedAt"] = time.time()
            self.persist()
        self.app.event("Agent control " + receipt["status"], preview["action"] + " · " + str(len(preview["targets"])) + " agent(s)", preview["fleetId"])
