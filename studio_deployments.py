"""Reviewable local workflow releases and explicit agent adoption receipts."""
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid
from studio_bindings import runtime_binding, same_binding
from workflow_spec import assignment_document, apply_spec, digest, diff, export_spec, metadata, render, runtime_problems, validate

INTEGRATION_POLICY = {
    'id': 'integration-coordination', 'name': 'Coordinate changes during final integration', 'roles': [], 'sources': [],
    'text': 'Before final integration or acceptance checks, the Integrator records a bounded integration reservation in the existing ticket: responsible integration owner, candidate and base revisions, affected files and shared interfaces, required checks and the exact revisions tested, start time, next review/expiry time and release condition. This is coordination of current custody, not a global freeze or transfer of ownership. Before merging, rebasing or changing overlapping files/interfaces, Writers, the Coordinator and agents arranging externally managed work consult that record and agree sequencing with the integration owner. Non-overlapping authorized work may continue. The Coordinator brings externally managed changes into the same overlap check; an unacknowledged external reservation is a reported risk, not an assumed lock. An urgent overlapping change requires an explicit recorded decision identifying which evidence it invalidates. When the candidate or relevant main dependencies change, the Integrator records impacted checks and withdraws superseded acceptance requests; refresh affected evidence against the new submitted revision before requesting acceptance again. Keep unaffected evidence only with a documented applicability assessment. Rebase completion, passing tests at an older revision, cleared goals and workflow adoption do not establish current acceptance. Release the reservation when integration completes, is withdrawn or is deliberately rescheduled; review expired reservations with the owner rather than automatically completing, resuming or transferring work. Preserve genuine blockers, explicit stops, source/publication restrictions and serialized runtime ownership. Existing progress and blocked/unblocked reports should link the reservation evidence, owner, dependency and bounded next action.'}


def brief_changes(old, new):
    """Headings of brief sections whose text differs."""
    def sections(text):
        result, heading = {}, "Header"
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
            elif not line.startswith("Brief "):
                result.setdefault(heading, []).append(line)
        return result
    before, after = sections(old), sections(new)
    return [name for name in list(after) + [n for n in before if n not in after] if before.get(name) != after.get(name)]


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
                agent["enabled"] = bool(agent["enabled"] and local and local["enabled"] and local["sessionId"] == agent["sessionId"] and local["directory"] == agent["directory"]
                                        and runtime_binding(local) == runtime_binding(agent))
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
            if not agent or not binding or not agent['enabled'] or not same_binding(agent, binding, ('sessionId', 'directory', 'roleIds', 'notes')) or aid not in acknowledged:
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
            entry = next((e for e in entry_points if agent["id"] in e["agentIds"] and not runtime_binding(agent) and agent["directory"] and Path(e["path"]).parent == Path(agent["directory"]).resolve()), None)
            binding = deployed.get(agent["id"])
            matched = same_binding(binding, agent, ("directory", "sessionId", "roleIds", "notes"))
            acknowledgement = matched and any(a["agentId"] == agent["id"] for a in acks)
            receipt = next((t["status"] for c in reversed(notifications) for t in c["targets"] if t["agentId"] == agent["id"]), "not-notified")
            # Acknowledgement is optional: a brief counts as delivered once it is installed, queued or started.
            # An entry point edited or removed outside Studio means the agent may read the wrong text.
            delivered = entry["status"] == "installed" if entry else bool(acknowledgement or receipt in ("queued", "started"))
            adoption.append({"agentId": agent["id"], "name": agent["name"], "required": bool(agent["enabled"] and agent["sessionId"] and agent["directory"]),
                             "entryPoint": entry["status"] if entry else "not-installed", "bindingMatches": matched,
                             "notification": receipt, "acknowledged": bool(acknowledgement), "briefDelivered": matched and delivered})
        required = [a for a in adoption if a["required"]]
        return {"spec": spec, "digest": digest(spec), "entryPoints": entry_points,
                "adoption": adoption, "migrationComplete": bool(active and required and all(a["briefDelivered"] for a in required)),
                "active": ({k: active[k] for k in ("id", "digest", "createdAt", "version")} if active else None),
                "draftChanged": bool(active and (active["digest"] != digest(spec) or self.binding_digest(fleet) != self.binding_digest(active["fleet"]))),
                "paused": self.data["paused"].get(fleet["id"], False),
                "history": [r for r in self.data["history"] if r["fleetId"] == fleet["id"]][-20:][::-1],
                "acks": acks, "notifications": notifications,
                "enabledTriggers": sum(t["enabled"] for t in self.runtime_fleet(fleet).get("triggers", []))}

    def draft_status(self, fleet):
        """Cheap check of whether the draft differs from what agents are working from."""
        rid = self.data["active"].get(fleet["id"])
        if not rid:
            return {"active": False, "draftChanged": bool(fleet.get("roles")), "paused": False, "staleAgents": []}
        release = self.release(rid)
        try:
            changed = release["digest"] != digest(export_spec(fleet)) or self.binding_digest(fleet) != self.binding_digest(release["fleet"])
        except ValueError:
            changed = True
        deployed = {a["id"]: a for a in release["fleet"]["agents"]}
        stale = [a["id"] for a in fleet["agents"] if a["id"] not in deployed
                 or not same_binding(deployed[a["id"]], a, ("directory", "sessionId", "roleIds", "notes"))]
        return {"active": True, "draftChanged": changed, "paused": self.data["paused"].get(fleet["id"], False), "staleAgents": stale}

    @staticmethod
    def binding_digest(fleet):
        # Runtime defaults keep digests of releases made before backend bindings unchanged.
        return digest({"agents": [{**{k: a.get(k) for k in ("id", "name", "sessionId", "directory", "enabled", "roleIds", "notes")},
                                   **runtime_binding(a)} for a in fleet["agents"]],
                       "triggers": [{"id": t["id"], "enabled": t["enabled"]} for t in fleet.get("triggers", [])]})

    def preview(self, payload):
        with self.app.store.lock:
            state = self.app.store.snapshot()
            if payload.get("revision") != state["revision"]:
                raise ValueError("Fleet changed. Reload before reviewing the workflow.")
            fleet = self.app.store.fleet(payload.get("fleetId"))
            before = export_spec(fleet)
            kind = payload.get("kind", "import")
            extra = {}
            if kind == "profile":
                # Retool the draft to a saved profile and deploy it in one review, so agents never see a half-switched fleet.
                from fleet_studio import validate_fleets
                from studio_profiles import capture, previous_slug, retool
                slug = payload.get("profile")
                profile = self.app.profiles.read(slug)
                retooled, remap = retool(fleet, profile)
                fleet = validate_fleets([retooled], {h["id"] for h in state.get("hosts", [])})[0]
                extra = {"profile": {"slug": slug, "name": profile.get("name") or slug, "remap": remap, "digest": digest(profile)},
                         "previousProfile": {"slug": previous_slug(fleet["id"]),
                                             "document": capture(self.app.store.fleet(fleet["id"]), "Before switching to " + (profile.get("name") or slug),
                                                                 "Saved automatically before the switch, so you can switch back.")}}
                before = export_spec(fleet)
                kind = "deploy-profile"
            if kind == "import":
                if payload.get("template") == "sample":
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
            elif kind in ("deploy", "deploy-profile", "reporting", "bounded-goals", "coordination"):
                spec = deepcopy(before)
                if kind in ('bounded-goals', 'coordination'):
                    from studio_goals import POLICY
                    policy = POLICY if kind == 'bounded-goals' else INTEGRATION_POLICY
                    active_id = self.data['active'].get(fleet['id'])
                    if not active_id or digest(before) != self.release(active_id)['digest'] or self.binding_digest(fleet) != self.binding_digest(self.release(active_id)['fleet']):
                        raise ValueError('Deploy or discard unrelated draft changes before adding workflow instructions.')
                    if any(p['id'] == policy['id'] for p in spec['policies']):
                        raise ValueError(policy['name'] + ' instructions are already deployed.')
                    if not any(p['id'] == 'progress-reporting' for p in spec['policies']):
                        raise ValueError('Set up progress reporting before bounded goals.')
                    spec['policies'].append(deepcopy(policy))
                    version = spec['version'].split('.')
                    if len(version) == 3 and all(v.isdigit() for v in version):
                        spec['version'] = '.'.join(version[:2] + [str(int(version[2]) + 1)])
                    spec = validate(spec)
                if kind == 'reporting':
                    active_id = self.data['active'].get(fleet['id'])
                    if not active_id or digest(before) != self.release(active_id)['digest'] or self.binding_digest(fleet) != self.binding_digest(self.release(active_id)['fleet']):
                        raise ValueError('Deploy or discard unrelated draft changes before adding reporting instructions.')
                    if any(p['id'] == 'progress-reporting' for p in spec['policies']):
                        raise ValueError('Progress reporting is already deployed.')
                    spec['policies'].append({'id':'progress-reporting','name':'Meaningful progress and receipt reports','roles':[],'sources':[],
                        'text':'Studio observes sessions, so agents report only done, blocked and hand-off with workflow_client.py report. Studio infers starts, progress and receipts from session activity. Do not post repeated or no-change updates.'})
                    version = spec['version'].split('.')
                    if len(version) == 3 and all(v.isdigit() for v in version):
                        spec['version'] = '.'.join(version[:2] + [str(int(version[2]) + 1)])
                    spec = validate(spec)
                from studio_workflow import contract_problem
                problems = [contract_problem(fleet, t) for t in fleet.get("triggers", []) if t["enabled"] and contract_problem(fleet, t)]
                problems += [g["summary"] for g in spec["gaps"] if g["blocking"] and g["resolution"] == "open"]
                problems += runtime_problems(fleet)
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
                       "previousSpec": baseline if kind in ("deploy", "deploy-profile", "reporting", "bounded-goals", "coordination") else before,
                       "pendingHandoffs": self.app.progress.pending_handoffs(fleet["id"]) if hasattr(self.app, "progress") else [],
                       "bindingChanges": {"before": self.release(self.data["active"][fleet["id"]])["fleet"]["agents"] if fleet["id"] in self.data["active"] else [], "after": updated["agents"]},
                       "activeAtPreview": self.data["active"].get(fleet["id"]),
                       "agents": [{"id": a["id"], "name": a["name"], "roles": a.get("roleIds", []), "linked": bool(a["sessionId"])} for a in updated["agents"]],
                       "enabledTriggers": [t["name"] for t in updated.get("triggers", []) if t["enabled"]],
                       "document": render(spec), "gaps": spec["gaps"], **extra}
            self.previews = {k: v for k, v in self.previews.items() if v["createdAt"] > time.time() - 900}
            self.previews[preview["id"]] = preview
            return {k: deepcopy(v) for k, v in preview.items() if k not in ("fleet", "previousProfile")}

    def apply(self, preview_id):
        from fleet_studio import atomic_json
        with self.app.store.lock:
            preview = self.previews.get(preview_id)
            if not preview or preview["createdAt"] < time.time() - 900:
                raise ValueError("Workflow preview expired. Review it again.")
            state = self.app.store.snapshot()
            if preview["revision"] != state["revision"] or preview["activeAtPreview"] != self.data["active"].get(preview["fleetId"]):
                raise ValueError("Fleet or deployment changed. Review it again.")
            if preview.get("profile"):
                # The file reviewed is the file deployed; a profile edited since the review needs a new review.
                if digest(self.app.profiles.read(preview["profile"]["slug"])) != preview["profile"]["digest"]:
                    raise ValueError("That profile changed since the review. Review it again.")
                self.app.profiles.write(preview["previousProfile"]["slug"], preview["previousProfile"]["document"])
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
                entry = ("# Fleet Studio workflow release " + rid + "\n\nYour brief is in agents/<your agent ID>.md. "
                         "[WORKFLOW.md](WORKFLOW.md) is the full workflow for reference; you do not need to read it to do your work.\n")
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
            if agent["id"] in ids and (agent["id"] not in bindings or not same_binding(agent, bindings[agent["id"]])):
                raise ValueError("Agent bindings changed. Deploy again before notifying them.")
        acknowledged = {a["agentId"] for a in self.data["acks"] if a["releaseId"] == rid}
        reserved = {t["agentId"] for c in self.app.controls.snapshot(full=True) if c.get("deploymentId") == rid and c.get("purpose") == "workflow-adoption"
                    for t in c["targets"] if t["status"] in ("waiting", "queued", "started", "unknown")}
        ids = [aid for aid in ids if aid not in acknowledged | reserved]
        # Agents whose brief is word-for-word the same as in the previous release are not woken again.
        if payload.get("changedOnly", True):
            ids = [aid for aid in ids if self.brief_changed(fleet["id"], rid, aid)]
        if not ids:
            raise ValueError("No selected agent needs notifying: their briefs are unchanged, acknowledged or already queued.")
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
                target["text"] = self.brief_notice(fleet["id"], rid, agent, client)
            return deepcopy(stored)

    def brief_changed(self, fleet_id, release_id, agent_id):
        previous = [r["id"] for r in self.data["history"] if r["fleetId"] == fleet_id and r["id"] != release_id]
        if not previous:
            return True
        def text(rid):
            try:
                body = (self.root / rid / "agents" / (agent_id + ".md")).read_text(encoding="utf-8")
            except OSError:
                return None
            # The footer names the release, which always differs.
            return "\n".join(line for line in body.splitlines() if not line.startswith("Brief "))
        old, new = text(previous[-1]), text(release_id)
        return old is None or old != new

    def brief_notice(self, fleet_id, release_id, agent, client):
        """A short notice naming the brief sections that changed since the agent's previous release."""
        command = "python " + client + " instructions --fleet " + fleet_id + " --agent " + agent["id"]
        previous = [r["id"] for r in self.data["history"] if r["fleetId"] == fleet_id and r["id"] != release_id]
        changed = []
        if previous:
            try:
                old = (self.root / previous[-1] / "agents" / (agent["id"] + ".md")).read_text(encoding="utf-8")
                new = (self.root / release_id / "agents" / (agent["id"] + ".md")).read_text(encoding="utf-8")
                changed = brief_changes(old, new)
            except OSError:
                changed = []
        summary = ("Changed: " + ", ".join(changed) + "." if changed else
                   "Nothing in your brief changed." if previous else "This is your first brief.")
        header = "[Fleet Studio] Your brief was updated (release " + release_id[:8] + "). " + summary
        try:
            brief = (self.root / release_id / "agents" / (agent["id"] + ".md")).read_text(encoding="utf-8")
        except OSError:
            brief = ""
        # Agents without a folder entry point (Claude Code, remote) cannot run the read command, so
        # they get the brief itself; so does anyone whose brief is short enough to read in place.
        inline = agent.get("backend", "codex") != "codex" or agent.get("hostId", "local") != "local" or len(brief.split()) <= 400
        if brief and inline and len(header) + len(brief) < 15000:
            return header + " Read it at a natural stopping point, then carry on:\n\n" + brief
        return header + "\n\nRead it when you reach a natural stopping point, then carry on with your current work:\n" + command

    def acknowledge(self, payload):
        with self.app.store.lock:
            fleet = self.app.store.fleet(payload.get("fleetId"))
            rid = payload.get("releaseId")
            if rid != self.data["active"].get(fleet["id"]):
                raise ValueError("That release is no longer active.")
            agent = next((a for a in self.release(rid)["fleet"]["agents"] if a["id"] == payload.get("agentId")), None)
            local = next((a for a in fleet["agents"] if a["id"] == payload.get("agentId")), None)
            if not agent or not local or not same_binding(local, agent):
                raise ValueError("Agent is no longer bound to this release.")
            receipt = {"agentId": agent["id"], "name": agent["name"], "releaseId": rid, "time": time.time(), "status": "agent-reported"}
            self.data["acks"] = [a for a in self.data["acks"] if (a["agentId"], a["releaseId"]) != (agent["id"], rid)] + [receipt]
            self.persist()
            return receipt
