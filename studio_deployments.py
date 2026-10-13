"""Reviewable local workflow releases and explicit agent adoption receipts."""
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid
from workflow_spec import assignment_document, apply_spec, digest, diff, export_spec, metadata, render, validate


class Deployments:
    def __init__(self, app):
        self.app = app
        self.root = app.root / "workflow-releases"
        self.path = app.root / ".runtime" / "workflows.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"active": {}, "paused": {}, "history": [], "acks": []}
        self.previews = {}
        self.cache = {}

    def persist(self):
        from fleet_studio import atomic_json
        self.path.parent.mkdir(exist_ok=True)
        atomic_json(self.path, self.data)

    def release(self, release_id):
        if not isinstance(release_id, str) or len(release_id) != 32 or any(c not in "0123456789abcdef" for c in release_id):
            raise ValueError("Invalid release identifier.")
        if release_id not in self.cache:
            value = json.loads((self.root / release_id / "release.json").read_text(encoding="utf-8"))
            if value["id"] != release_id or digest(value["spec"]) != value["digest"]:
                raise ValueError("Workflow release integrity check failed.")
            self.cache[release_id] = value
        return deepcopy(self.cache[release_id])

    def runtime_fleet(self, fleet):
        release_id = self.data["active"].get(fleet["id"])
        if not fleet.get("workflow") and not release_id:
            return fleet
        if release_id:
            result = self.release(release_id)["fleet"]
            current = {a["id"]: a for a in fleet["agents"]}
            for agent in result["agents"]:
                local = current.get(agent["id"])
                # A release must never send to a newly rebound conversation.
                agent["enabled"] = bool(agent["enabled"] and local and local["enabled"] and local["sessionId"] == agent["sessionId"] and local["directory"] == agent["directory"])
        else:
            result = deepcopy(fleet)
        for rule in result.get("triggers", []):
            rule["deploymentId"] = release_id or "draft"
        if not release_id or self.data["paused"].get(fleet["id"]):
            for rule in result.get("triggers", []):
                rule["enabled"] = False
        return result

    def adoption_hold(self, fleet_id, agent_ids):
        rid = self.data['active'].get(fleet_id)
        if not rid:
            return ''
        current = self.app.store.fleet(fleet_id)
        bindings = {a['id']: a for a in self.release(rid)['fleet']['agents']}
        acknowledged = {a['agentId'] for a in self.data['acks'] if a['releaseId'] == rid}
        for aid in agent_ids:
            agent = next((a for a in current['agents'] if a['id'] == aid), None)
            binding = bindings.get(aid)
            if not agent or not binding or not agent['enabled'] or any(agent.get(k) != binding.get(k) for k in ('sessionId', 'directory', 'roleIds', 'notes')) or aid not in acknowledged:
                return (agent['name'] if agent else 'Source agent') + ' has not adopted the active workflow binding.'
        return ''

    def overview(self, fleet):
        spec = export_spec(fleet)
        rid = self.data["active"].get(fleet["id"])
        active = self.release(rid) if rid else None
        entry_points = self.app.entrypoints.status(fleet)
        acks = [a for a in self.data["acks"] if a["releaseId"] == rid]
        notifications = [c for c in self.app.controls.snapshot(full=True) if c.get("deploymentId") == rid and c.get("purpose") == "workflow-adoption"] if rid else []
        adoption = []
        deployed = {a["id"]: a for a in active["fleet"]["agents"]} if active else {}
        for agent in fleet["agents"]:
            entry = next((e for e in entry_points if agent["id"] in e["agentIds"] and agent["directory"] and Path(e["path"]).parent == Path(agent["directory"]).resolve()), None)
            binding = deployed.get(agent["id"])
            matched = bool(binding and all(binding[k] == agent[k] for k in ("directory", "sessionId", "roleIds", "notes")))
            acknowledgement = matched and any(a["agentId"] == agent["id"] for a in acks)
            receipt = next((t["status"] for c in reversed(notifications) for t in c["targets"] if t["agentId"] == agent["id"]), "not-notified")
            adoption.append({"agentId": agent["id"], "name": agent["name"], "required": bool(agent["enabled"] and agent["sessionId"] and agent["directory"]),
                             "entryPoint": entry["status"] if entry else "not-installed", "bindingMatches": matched,
                             "notification": receipt, "acknowledged": bool(acknowledgement)})
        required = [a for a in adoption if a["required"]]
        return {"spec": spec, "digest": digest(spec), "entryPoints": entry_points,
                "adoption": adoption, "migrationComplete": bool(active and required and all(a["bindingMatches"] and a["entryPoint"] == "installed" and a["acknowledged"] for a in required)),
                "active": ({k: active[k] for k in ("id", "digest", "createdAt", "version")} if active else None),
                "draftChanged": bool(active and (active["digest"] != digest(spec) or self.binding_digest(fleet) != self.binding_digest(active["fleet"]))),
                "paused": self.data["paused"].get(fleet["id"], False),
                "history": [r for r in self.data["history"] if r["fleetId"] == fleet["id"]][-20:][::-1],
                "acks": acks, "notifications": notifications,
                "enabledTriggers": sum(t["enabled"] for t in self.runtime_fleet(fleet).get("triggers", []))}

    @staticmethod
    def binding_digest(fleet):
        return digest({"agents": [{k: a.get(k) for k in ("id", "name", "sessionId", "directory", "enabled", "roleIds", "notes")} for a in fleet["agents"]],
                       "triggers": [{"id": t["id"], "enabled": t["enabled"]} for t in fleet.get("triggers", [])]})

    def preview(self, payload):
        with self.app.store.lock:
            state = self.app.store.snapshot()
            if payload.get("revision") != state["revision"]:
                raise ValueError("Fleet changed. Reload before reviewing the workflow.")
            fleet = self.app.store.fleet(payload.get("fleetId"))
            before = export_spec(fleet)
            kind = payload.get("kind", "import")
            if kind == "import":
                if payload.get("template") in ("sample", "eds"):
                    spec = json.loads((Path(__file__).parent / "workflows" / "sample.workflow.json").read_text(encoding="utf-8"))
                elif payload.get("releaseId"):
                    release = self.release(payload["releaseId"])
                    if release["fleet"]["id"] != fleet["id"]:
                        raise ValueError("Choose a release from this fleet.")
                    spec = release["spec"]
                else:
                    spec = payload.get("spec")
                spec = validate(spec)
                updated = apply_spec(fleet, spec)
                changes = diff(before, spec)
            elif kind in ("deploy", "reporting"):
                spec = deepcopy(before)
                if kind == 'reporting':
                    active_id = self.data['active'].get(fleet['id'])
                    if not active_id or digest(before) != self.release(active_id)['digest'] or self.binding_digest(fleet) != self.binding_digest(self.release(active_id)['fleet']):
                        raise ValueError('Deploy or discard unrelated draft changes before adding reporting instructions.')
                    if any(p['id'] == 'progress-reporting' for p in spec['policies']):
                        raise ValueError('Progress reporting is already deployed.')
                    spec['policies'].append({'id':'progress-reporting','name':'Meaningful progress and receipt reports','roles':[],'sources':[],
                        'text':'After recording substantive evidence in the existing tracker, report task start, meaningful progress, blocker changes, handoff receipt and work completion through fleet_progress.py. Record one brief handoff-received receipt after reading the exact handoff and its evidence; receiving work is separate from technical acceptance, execution and issue closure. Report existing local test results with suite, exact commit and evidence. Reuse identical event IDs for retries. Do not repeat technical review, tests, unchanged updates or historical deliveries to populate metrics. Preserve actual blocker reasons, explicit paused/blocked/limited goals, task and file custody, source/publication restrictions and serialized runtime ownership. Adoption and receipts do not resume stopped work.'})
                    version = spec['version'].split('.')
                    if len(version) == 3 and all(v.isdigit() for v in version):
                        spec['version'] = '.'.join(version[:2] + [str(int(version[2]) + 1)])
                    spec = validate(spec)
                from studio_workflow import contract_problem
                problems = [contract_problem(fleet, t) for t in fleet.get("triggers", []) if t["enabled"] and contract_problem(fleet, t)]
                problems += [g["summary"] for g in spec["gaps"] if g["blocking"] and g["resolution"] == "open"]
                if problems:
                    raise ValueError("Resolve before deployment: " + "; ".join(problems))
                updated = deepcopy(fleet)
                updated["workflow"] = metadata(spec)
                active_id = self.data["active"].get(fleet["id"])
                baseline = self.release(active_id)["spec"] if active_id else {**spec, **{k: [] for k in ("roles", "handoffs", "states", "transitions", "policies", "triggers", "sources", "gaps")}}
                changes = diff(baseline, spec)
            else:
                raise ValueError("Unknown workflow preview action.")
            preview = {"id": uuid.uuid4().hex, "fleetId": fleet["id"], "kind": kind, "revision": state["revision"],
                       "createdAt": time.time(), "spec": spec, "fleet": updated, "changes": changes,
                       "previousSpec": baseline if kind in ("deploy", "reporting") else before,
                       "pendingHandoffs": self.app.progress.pending_handoffs(fleet["id"]) if hasattr(self.app, "progress") else [],
                       "bindingChanges": {"before": self.release(self.data["active"][fleet["id"]])["fleet"]["agents"] if fleet["id"] in self.data["active"] else [], "after": updated["agents"]},
                       "activeAtPreview": self.data["active"].get(fleet["id"]),
                       "agents": [{"id": a["id"], "name": a["name"], "roles": a.get("roleIds", []), "linked": bool(a["sessionId"])} for a in updated["agents"]],
                       "enabledTriggers": [t["name"] for t in updated.get("triggers", []) if t["enabled"]],
                       "document": render(spec), "gaps": spec["gaps"]}
            self.previews = {k: v for k, v in self.previews.items() if v["createdAt"] > time.time() - 900}
            self.previews[preview["id"]] = preview
            return {k: deepcopy(v) for k, v in preview.items() if k != "fleet"}

    def apply(self, preview_id):
        from fleet_studio import atomic_json
        with self.app.store.lock:
            preview = self.previews.get(preview_id)
            if not preview or preview["createdAt"] < time.time() - 900:
                raise ValueError("Workflow preview expired. Review it again.")
            state = self.app.store.snapshot()
            if preview["revision"] != state["revision"] or preview["activeAtPreview"] != self.data["active"].get(preview["fleetId"]):
                raise ValueError("Fleet or deployment changed. Review it again.")
            fleet = deepcopy(preview["fleet"])
            state["fleets"] = [fleet if f["id"] == fleet["id"] else f for f in state["fleets"]]
            if preview["kind"] == "import":
                result = {"state": self.app.store.save(state)}
            else:
                spec = preview["spec"]
                rid = uuid.uuid4().hex
                directory = self.root / rid
                directory.mkdir(parents=True)
                release = {"id": rid, "fleetId": fleet["id"], "version": spec["version"], "createdAt": time.time(),
                           "digest": digest(spec), "spec": spec, "fleet": fleet}
                atomic_json(directory / "workflow.json", spec)
                atomic_json(directory / "release.json", release)
                (directory / "WORKFLOW.md").write_text(render(spec), encoding="utf-8")
                (directory / "agents").mkdir()
                for agent in fleet["agents"]:
                    instruction = assignment_document(fleet, spec, agent, rid, Path(__file__).parent)
                    (directory / "agents" / (agent["id"] + ".md")).write_text(instruction, encoding="utf-8")
                entry = ("# Fleet Studio workflow entry point\n\nWorkflow release " + rid + ".\n\nRead [WORKFLOW.md](WORKFLOW.md) and your assigned file under agents/ before continuing. "
                         "This release defines workflow responsibilities, handoffs and acceptance. Existing ticket scopes, explicit stops, source/publication restrictions, "
                         "host recovery and engineering standards remain applicable. Report conflicts instead of silently choosing a weaker rule.\n\n"
                         "Studio draft edits take effect after a new deployment. Keep technical work and evidence in the existing tickets/MRs.\n")
                (directory / "AGENTS.md").write_text(entry, encoding="utf-8")
                # Publishing a release is local. Agent notification is a separate explicit control.
                saved = self.app.store.save(state)
                previous = deepcopy(self.data)
                self.data["active"][fleet["id"]] = rid
                self.data["paused"].setdefault(fleet["id"], False)
                self.data["history"].append({k: release[k] for k in ("id", "fleetId", "version", "createdAt", "digest")})
                try:
                    self.persist()
                except Exception:
                    self.data = previous
                    raise
                self.cache[rid] = release
                result = {"state": saved, "release": {"id": rid, "directory": str(directory), "digest": release["digest"]}}
            del self.previews[preview_id]
            return result

    def pause(self, payload):
        with self.app.store.lock:
            fleet = self.app.store.fleet(payload.get("fleetId"))
            if fleet["id"] not in self.data["active"] or type(payload.get("paused")) is not bool:
                raise ValueError("Choose a deployed workflow and pause state.")
            self.data["paused"][fleet["id"]] = payload["paused"]
            self.persist()
            return self.overview(fleet)

    def notification(self, payload):
        fleet = self.app.store.fleet(payload.get("fleetId"))
        rid = self.data["active"].get(fleet["id"])
        if not rid:
            raise ValueError("Deploy the workflow before notifying agents.")
        release = self.release(rid)
        bindings = {a["id"]: a for a in release["fleet"]["agents"]}
        ids = payload.get("agentIds", [a["id"] for a in fleet["agents"] if a["sessionId"]])
        for agent in fleet["agents"]:
            if agent["id"] in ids and (agent["id"] not in bindings or (agent["sessionId"], agent["directory"]) != (bindings[agent["id"]]["sessionId"], bindings[agent["id"]]["directory"])):
                raise ValueError("Agent bindings changed. Deploy again before notifying them.")
        acknowledged = {a["agentId"] for a in self.data["acks"] if a["releaseId"] == rid}
        reserved = {t["agentId"] for c in self.app.controls.snapshot(full=True) if c.get("deploymentId") == rid and c.get("purpose") == "workflow-adoption"
                    for t in c["targets"] if t["status"] in ("waiting", "queued", "unknown")}
        ids = [aid for aid in ids if aid not in acknowledged | reserved]
        if not ids:
            raise ValueError("All selected agents have acknowledged or already have queued/unconfirmed notifications. Check their receipts before retrying.")
        with self.app.controls.lock:
            previews = list(self.app.controls.previews.values())
        for preview in previews:
            if preview.get("deploymentId") == rid and preview.get("purpose") == "workflow-adoption" and {t["agentId"] for t in preview["targets"]} == set(ids):
                existing = self.app.controls.existing_preview(preview["id"])
                if existing:
                    return existing
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        client = quote(Path(__file__).parent / "workflow_client.py")
        with self.app.store.lock, self.app.controls.lock:
            preview = self.app.controls.preview({"fleetId": fleet["id"], "agentIds": ids, "action": "message",
                                                 "message": "Adopt Fleet Studio workflow release " + rid,
                                                 "deploymentId": rid, "purpose": "workflow-adoption"})
            stored = self.app.controls.previews[preview["id"]]
            for target in stored["targets"]:
                agent = bindings[target["agentId"]]
                target["text"] = (
                    "Adopt Fleet Studio workflow release " + rid + " at your next safe boundary.\n\n"
                    "Your Fleet Studio identity: " + agent["name"] + " (" + agent["id"] + ").\n"
                    "Assigned folder: " + agent["directory"] + ".\n"
                    "Read " + str(self.root / rid / "agents" / (agent["id"] + ".md")) +
                    ". This contains your assignment and the applicable deployed policy. "
                    "Use your folder entry point to read the active release on future safe boundaries. "
                    "These workflow definitions replace the historical workflow documents; continue using project engineering standards and ticket records.\n\n"
                    "This is a policy-adoption instruction, not a new work assignment. Preserve explicit stops, current bounded tasks, claims, file custody, "
                    "owned runtime and source/publication restrictions. Read the latest current ticket and host/session record; do not revive a historical task "
                    "from a source citation or begin stopped work. Report conflicts before adopting. Do not launch MATLAB just to acknowledge.\n\n"
                    "After reading and accepting your assignment, acknowledge only for yourself:\n"
                    "python " + client + " acknowledge --fleet " + fleet["id"] + " --agent " + agent["id"] + " --release " + rid)
            return deepcopy(stored)

    def acknowledge(self, payload):
        with self.app.store.lock:
            fleet = self.app.store.fleet(payload.get("fleetId"))
            rid = payload.get("releaseId")
            if rid != self.data["active"].get(fleet["id"]):
                raise ValueError("That release is no longer active.")
            agent = next((a for a in self.release(rid)["fleet"]["agents"] if a["id"] == payload.get("agentId")), None)
            local = next((a for a in fleet["agents"] if a["id"] == payload.get("agentId")), None)
            if not agent or not local or (local["sessionId"], local["directory"]) != (agent["sessionId"], agent["directory"]):
                raise ValueError("Agent is no longer bound to this release.")
            receipt = {"agentId": agent["id"], "name": agent["name"], "releaseId": rid, "time": time.time(), "status": "agent-reported"}
            self.data["acks"] = [a for a in self.data["acks"] if (a["agentId"], a["releaseId"]) != (agent["id"], rid)] + [receipt]
            self.persist()
            return receipt
