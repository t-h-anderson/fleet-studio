"""Reviewed installation of stable workflow entry points in assigned agent folders."""
from studio_bindings import same_binding
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import uuid
from workflow_spec import digest
from studio_trackers import tracker_label


class EntryPoints:
    def __init__(self, app):
        self.app = app
        self.path = app.root / ".runtime" / "workflow-entrypoints.json"
        self.records = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.previews = {}

    def content(self, fleet, agents):
        if isinstance(agents, dict):
            agents = [agents]
        client = Path(__file__).parent / "workflow_client.py"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        folder = Path(agents[0]["directory"]).resolve()
        commands = "\n".join(
            "### " + agent["name"] + "\n\nFleet agent ID: " + agent["id"] + "\n\n```powershell\npython " + quote(client) +
            " instructions --fleet " + fleet["id"] + " --agent " + agent["id"] + " --directory (Get-Location).Path\n```\n"
            for agent in agents)
        return ("# Fleet Studio managed workflow\n\n"
                "## Folder and agent scope\n\nThis entry point applies only to the named Fleet Studio agents working directly in `" + str(folder) + "`. "
                "If inherited by a different or nested checkout, do not adopt these assignments; use that checkout's own entry point and assigned workflow. "
                "Match your existing fleet name and agent ID below. Read only your own assignment; sharing a folder does not combine roles or transfer custody. "
                "If you cannot identify your assignment, report the ambiguity before adopting.\n\n"
                "Workflow roles, assignments, handoffs, acceptance and coordination are managed by the active Fleet Studio release. "
                "Read your current deployed instructions at each safe task boundary and after a workflow notification:\n\n" + commands + "\n"
                "This command reads the locally deployed release and works while the browser/server is closed. "
                "If no release is available, stop workflow work and report the missing deployment; do not silently revert to an older process.\n\n"
                "The deployed workflow replaces workflow instructions in the older roles, agent backlog and handoff documents. "
                "Continue to read repository AGENTS.md and applicable engineering guides for coding/testing requirements, source/publication restrictions and safety. "
                "Preserve explicit task stops, current ticket/file custody, claims and host recovery rules. A workflow deployment does not grant new source permissions or resume stopped work. "
                "Report conflicts or missing assignments at a safe boundary. " + tracker_label(fleet) + " remains the live task, claim and evidence record.\n")

    @staticmethod
    def target(directory):
        folder = Path(directory).resolve()
        target = folder / "AGENTS.override.md"
        if not folder.is_dir() or target.is_symlink() or target.resolve().parent != folder or target.exists() and not target.is_file():
            raise ValueError("The agent entry point must be a regular file inside its assigned folder.")
        return target

    def status(self, fleet):
        result = []
        for path, record in self.records.items():
            if record["fleetId"] != fleet["id"]:
                continue
            status = "missing"
            try:
                target = self.target(Path(path).parent)
                if target.exists():
                    status = "installed" if digest(target.read_text(encoding="utf-8")) == record["installedDigest"] else "changed"
            except (OSError, UnicodeError, ValueError):
                status = "changed"
            result.append({"path": path, "agentId": record["agentId"], "agentIds": record.get("agentIds", [record["agentId"]]), "status": status})
        return result

    def preview(self, payload):
        state = self.app.store.snapshot()
        if payload.get("revision") != state["revision"]:
            raise ValueError("Fleet changed. Review the entry points again.")
        fleet = self.app.store.fleet(payload.get("fleetId"))
        rid = self.app.deployments.data["active"].get(fleet["id"])
        if not rid:
            raise ValueError("Deploy a workflow before installing entry points.")
        ids = payload.get("agentIds", [a["id"] for a in fleet["agents"] if a["directory"]])
        agents = [a for a in fleet["agents"] if a["id"] in ids]
        if not agents or len(agents) != len(set(ids)):
            raise ValueError("Choose existing agents with assigned folders.")
        deployed = {a["id"]: a for a in self.app.deployments.release(rid)["fleet"]["agents"]}
        changes, groups = [], {}
        for agent in agents:
            if agent.get("hostId", "local") != "local":
                raise ValueError("Remote entry-point installation is not supported; remote paths must never be opened locally.")
            if agent.get('backend', 'codex') != 'codex':
                raise ValueError('Automatic entry-point installation supports Codex only. Load Claude assignments explicitly with workflow_client.py instructions.')
            if agent["id"] not in deployed or not same_binding(deployed[agent["id"]], agent):
                raise ValueError("Agent bindings changed. Deploy again before installing entry points.")
            if not agent["directory"]:
                raise ValueError("Assign an agent folder first.")
            target = self.target(agent["directory"])
            key = str(target).casefold()
            groups.setdefault(key, {"target": target, "agents": []})["agents"].append(agent)
        for group in groups.values():
            target, members = group["target"], group["agents"]
            peers = [a for a in fleet["agents"] if a.get("hostId", "local") == "local" and a["directory"] and Path(a["directory"]).resolve() == target.parent]
            if any(a["id"] not in ids for a in peers):
                raise ValueError("Include all agents sharing a folder when reviewing its entry point.")
            before = target.read_bytes() if target.exists() else None
            if before is not None and len(before) > 100000:
                raise ValueError("Existing entry point is too large to review here.")
            after = self.content(fleet, members)
            changes.append({"name": ", ".join(a["name"] for a in members), "agentId": members[0]["id"], "agentIds": [a["id"] for a in members], "path": str(target), "before": before.decode("utf-8-sig") if before is not None else None,
                            "beforeBytes": base64.b64encode(before).decode() if before is not None else None, "after": after})
        result = {"id": uuid.uuid4().hex, "fleetId": fleet["id"], "releaseId": rid, "revision": state["revision"], "createdAt": time.time(), "changes": changes}
        self.previews = {k:v for k,v in self.previews.items() if v["createdAt"] > time.time()-900}
        self.previews[result["id"]] = result
        return {**result, "changes": [{k:v for k,v in c.items() if k != "beforeBytes"} for c in changes]}

    @staticmethod
    def write(path, body):
        temporary = path.with_name(path.name + ".fleet-" + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(body)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def apply(self, preview_id):
        if not self.app.launch_enabled:
            raise ValueError("Entry-point installation is disabled in this test instance.")
        from fleet_studio import atomic_json
        with self.app.store.lock:
            preview = self.previews.get(preview_id)
            state = self.app.store.snapshot()
            if not preview or preview["createdAt"] < time.time()-900:
                raise ValueError("Entry-point preview expired.")
            if preview["revision"] != state["revision"] or preview["releaseId"] != self.app.deployments.data["active"].get(preview["fleetId"]):
                raise ValueError("Fleet or release changed. Review entry points again.")
            for c in preview["changes"]:
                path = self.target(Path(c["path"]).parent)
                before = base64.b64decode(c["beforeBytes"]) if c["beforeBytes"] is not None else None
                if (path.read_bytes() if path.exists() else None) != before:
                    raise ValueError("Entry point changed since preview: " + c["path"])
            # Persist recovery bytes before touching any assigned folder.
            records = deepcopy(self.records)
            for c in preview["changes"]:
                records.setdefault(c["path"], {"originalBytes": c["beforeBytes"], "fleetId": preview["fleetId"], "agentId": c["agentId"]})
                records[c["path"]].update(fleetId=preview["fleetId"], agentId=c["agentId"], agentIds=c["agentIds"], installedDigest=digest(c["after"]), installedAt=time.time())
            self.path.parent.mkdir(exist_ok=True)
            atomic_json(self.path, records)
            completed = []
            try:
                for c in preview["changes"]:
                    self.write(Path(c["path"]), c["after"].encode("utf-8"));completed.append(c)
            except Exception:
                for c in reversed(completed):
                    path=Path(c["path"])
                    if path.read_bytes() == c["after"].encode("utf-8"):
                        if c["beforeBytes"] is None: path.unlink()
                        else: self.write(path,base64.b64decode(c["beforeBytes"]))
                raise
            self.records = records
            del self.previews[preview_id]
            return {"installed": [c["path"] for c in completed], "backup": str(self.path), "state": self.app.store.save(state)}
