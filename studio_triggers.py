"""Explicitly enabled workflow triggers with persisted deduplication and delivery receipts."""
from studio_bindings import runtime_binding, session_key
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
import re
from studio_workflow import contract_problem


def validate_triggers(raw, role_ids, agent_ids):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("A fleet can have at most 100 triggers.")
    output, seen = [], set()
    for row in raw:
        tid = str(uuid.UUID(str(row.get("id"))))
        if tid in seen:
            raise ValueError("Trigger IDs must be unique.")
        seen.add(tid)
        name = row.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Give the trigger a name.")
        kind = row.get("kind")
        if kind not in ("event", "condition", "timer", "idle"):
            raise ValueError("Choose an upstream event, condition or timer.")
        source = row.get("sourceRoleId", "")
        target = row.get("targetRoleId", "")
        if target not in role_ids or (kind not in ("timer", "idle") and source not in role_ids):
            raise ValueError("Trigger roles must belong to this fleet.")
        event_name = row.get("eventName", "packet-ready")
        if not isinstance(event_name, str) or not event_name or len(event_name) > 100:
            raise ValueError("Give the upstream event a name.")
        condition = row.get("condition", "turn-completed")
        if condition not in ("turn-completed", "idle", "attention", "closed"):
            raise ValueError("Unknown observed condition.")
        minutes = row.get("minutes", 30)
        if type(minutes) is not int or not 1 <= minutes <= 10080:
            raise ValueError("Timer interval must be between 1 and 10080 minutes.")
        message = row.get("message", "")
        if not isinstance(message, str) or not message.strip() or len(message) > 12000 or message.lstrip().startswith(("/", "!")):
            raise ValueError("Use a plain instruction of at most 12000 characters.")
        if type(row.get("enabled", False)) is not bool or row.get("mode", "automatic") not in ("automatic", "review"):
            raise ValueError("Invalid trigger delivery settings.")
        if row.get("recipientMode", "single") not in ("single", "all"):
            raise ValueError("Choose a single recipient or an explicit role broadcast.")
        workflow_id = row.get("workflowLinkId", "")
        workflow_signature = row.get("workflowSignature", "")
        if not isinstance(workflow_id, str) or not isinstance(workflow_signature, str) or (workflow_signature and not workflow_id):
            raise ValueError("Invalid source workflow contract.")
        if workflow_id:
            workflow_id = str(uuid.UUID(str(workflow_id)))
            if not isinstance(workflow_signature, str) or not re.fullmatch(r"[0-9a-f]{64}", workflow_signature):
                raise ValueError("Invalid source workflow contract.")
        output.append({**{k: row[k] for k in ("specId", "specContract", "recipientMode") if k in row}, **({"workflowLinkId": workflow_id, "workflowSignature": workflow_signature} if workflow_id else {}), "id": tid, "name": name.strip(), "kind": kind, "sourceRoleId": source,
                       "targetRoleId": target, "eventName": event_name.strip(), "condition": condition,
                       "minutes": minutes, "message": message.strip(), "enabled": row.get("enabled", False),
                       "mode": row.get("mode", "automatic")})
    return output


def rule_signature(rule):
    return hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()


class Triggers:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.path = app.root / ".runtime" / "triggers.json"
        self.data = {"states": {}, "actions": [], "events": [], "seenEvents": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        seen = self.data.setdefault("seenEvents", {})
        self.data.setdefault('contractIds', {})
        self.data.setdefault('currentContracts', {})
        self.data.setdefault('continuations', {})
        for event in self.data["events"]:
            seen.setdefault(event["key"], {"digest": None})
        # Historical turn transitions are never replayed after restart.
        # Recurring idle schedules and unsent deliveries are durable.
        self.observed = {}
        self.idle_since = {}

    def persist(self):
        from fleet_studio import atomic_json
        from studio_continuations import identity
        for action in self.data['actions']:
            if action.get('continuationOf'):
                key = identity(action['fleetId'], action['assignmentContract'])
                record = self.data['continuations'].get(key)
                if record:
                    record['state'] = {'pending': 'unsent', 'held': 'held', 'sending': 'sending', 'queued': 'delivered',
                                       'needs-attention': 'uncertain', 'reconciled': 'delivered', 'dismissed': 'invalidated',
                                       'invalidated': 'invalidated'}.get(action['status'], 'uncertain')
        self.path.parent.mkdir(exist_ok=True)
        atomic_json(self.path, self.data)
        # Telemetry cannot undo a committed delivery intent. Persist every
        # handoff before bounded UI history can discard a queued action.
        if hasattr(self.app, 'progress'):
            try:
                self.app.progress.observe_handoffs(self.data['actions'])
            except Exception as exc:
                import logging
                logging.exception('Progress handoff observation failed')
                self.app.progress.observation_error = 'Handoff observations are delayed: ' + type(exc).__name__

    def snapshot(self):
        with self.lock:
            waiting = [a for a in self.data["actions"] if a["status"] in ("pending", "held", "sending", "needs-attention")]
            recent = [a for a in self.data["actions"] if a not in waiting][-30:]
            return deepcopy({"actions": sorted(waiting + recent, key=lambda a: a["createdAt"], reverse=True),
                             "events": self.data["events"][-15:][::-1], "states": self.data["states"]})

    def targets(self, fleet, rule, target_id=None):
        agents = [a for a in fleet["agents"] if rule["targetRoleId"] in a.get("roleIds", [])]
        if target_id:
            agents = [a for a in agents if a["id"] == target_id]
            if not agents:
                raise ValueError("The recipient must belong to this handoff's target role in the deployed workflow.")
        elif rule["kind"] == "event" and rule.get("recipientMode", "single") == "single" and len(agents) != 1:
            raise ValueError("This handoff requires one named recipient. Supply --to with an agent assigned to the target role.")
        if not agents:
            raise ValueError("No agents are assigned to the target role.")
        return agents

    def hold_reason(self, agents, monitor, idle_only=False, ignore_control_id=None):
        if not agents:
            return "No agents are assigned to the target role."
        if not monitor.get("observedAt") or time.time() - monitor["observedAt"] > 20:
            return "Live session observations are unavailable or stale."
        controls = self.app.controls.snapshot(full=True)
        for agent in agents:
            item = monitor.get("sessions", {}).get(session_key(agent), {})
            reserved = any(session_key(t) == session_key(agent) and
                           (t["status"] in ("waiting", "unknown") or
                            (t["status"] == "queued" and t.get("acknowledgedAt", c["createdAt"]) >= item.get("observedAt", monitor["observedAt"])))
                           for c in controls if c["id"] != ignore_control_id for t in c["targets"])
            if reserved:
                return agent["name"] + " has a dispatch awaiting a fresh queue observation or outcome review."
            if not item.get("observedAt") or time.time() - item["observedAt"] > 20:
                return agent["name"] + " has stale session observations."
            if not agent["enabled"]:
                return agent["name"] + " is excluded from automatic delivery."
            if agent.get("backend", "codex") == "claude-code":
                # Claude Code has no queue: Studio resumes a closed session for one headless turn.
                if item.get("status") == "working":
                    return agent["name"] + " is mid-turn; waiting for it to finish."
                if item.get("status") == "idle" or item.get("pids"):
                    return agent["name"] + " is open in a Claude Code client; Studio cannot start a turn alongside it."
                if item.get("status") != "closed" or not item.get("livenessKnown"):
                    return agent["name"] + " has unknown process ownership on this host."
                if idle_only and item.get("status") not in ("idle", "closed"):
                    return agent["name"] + " is working; waiting for idle."
                continue
            if item.get("status") not in ("idle", "working") or not item.get("pids"):
                return agent["name"] + " is closed or needs attention; open/review the session first."
            if agent.get("backend", "codex") != "codex":
                return agent["name"] + " has no safe message queue; use an explicit reviewed turn after closing the session."
            if not item.get("goalKnown"):
                return agent["name"] + " has unavailable goal-state observations."
            if item.get("goalStatus") in ("paused", "blocked", "budget_limited", "usage_limited"):
                return agent["name"] + " has a paused, blocked or limited goal."
            if item.get("queueCount") is None:
                return agent["name"] + " has unavailable queue observations."
            if item["queueCount"] > 0:
                return agent["name"] + " already has queued work."
            if idle_only and item.get("status") != "idle":
                return agent["name"] + " is working; waiting for idle."
        return ""

    @staticmethod
    def message(rule, cause, event=None):
        text = "[Fleet Studio] " + rule["name"] + " (" + cause + ")\n\n" + rule["message"]
        if event:
            text += "\n\nReported handoff (verify against the current ticket):\nFrom: " + event["agent"]
            for key in ("eventId", "ticket", "artifact", "context"):
                if event.get(key):
                    text += "\n" + key + ": " + event[key]
            if event.get('assignmentContract'):
                from studio_contracts import describe
                text += '\n\n' + describe(event['assignmentContract'])
        if len(text) > 16000:
            raise ValueError("The trigger instruction and handoff details exceed the 16000 character delivery limit.")
        return text

    def fire(self, fleet, rule, cause, monitor, target_id=None, event=None, dispatch=True):
        # All callers hold the trigger lock. Each target owns its delivery so
        # a busy peer cannot block or duplicate another recipient's message.
        latest = self.app.runtime_fleet(self.app.store.fleet(fleet["id"]))
        if latest != fleet or not rule["enabled"] or rule not in latest.get("triggers", []):
            return []
        message = self.message(rule, cause, event)
        signature = rule_signature(rule)
        actions = []
        try:
            agents = self.targets(fleet, rule, target_id)
        except ValueError:
            if event:
                raise
            agents = [{"id": "", "name": "Unassigned", "sessionId": "", "directory": ""}]
        for agent in agents:
            if not event and any(a["triggerId"] == rule["id"] and a.get("ruleSignature") == signature and
                                 a.get("targetAgentId") == agent["id"] and a["status"] in ("pending", "held", "sending", "needs-attention")
                                 for a in self.data["actions"]):
                continue
            action = {"id": uuid.uuid4().hex, "fleetId": fleet["id"], "triggerId": rule["id"],
                      "name": rule["name"], "cause": cause, "createdAt": time.time(), "status": "pending",
                      "message": message, "mode": rule["mode"], "ruleSignature": signature,
                      "targetAgentId": agent["id"], "targetName": agent["name"],
                      "targetSessionId": agent["sessionId"], "targetDirectory": agent["directory"], "targetRuntime": runtime_binding(agent),
                      "sourceAgentId": event.get("agentId") if event else None, "deploymentId": rule.get("deploymentId"),
                      **({"eventId": event["eventId"], "ticket": event.get("ticket", ""), "eventKey": event["key"]} if event else {}),
                      **({'assignmentContract': deepcopy(event['assignmentContract'])} if event and event.get('assignmentContract') else {})}
            # Receipt is observed from the recipient's next turn; no report is requested.
            action["message"] += "\n\nHandoff " + action["id"][:8] + "." if event else ""
            self.data["actions"].append(action)
            actions.append(action)
        if dispatch:
            self.persist()
            for action in actions:
                self.deliver(action, monitor)
            self.persist()
        return actions

    def deliver(self, action, monitor):
        if action["status"] not in ("pending", "held") or action.get("controlId"):
            return
        if action.get('continuationOf'):
            from studio_continuations import invalidated
            if invalidated(self.app, action):
                action.update(status='invalidated', detail='The original assignment was replaced, withdrawn, submitted or changed release; this continuation will not send.')
                return
        if action["mode"] != "automatic":
            return
        try:
            fleet = self.app.runtime_fleet(self.app.store.fleet(action["fleetId"]))
        except ValueError:
            action.update(status="needs-attention", detail="The fleet was removed; this handoff has not been sent.")
            return
        rule = next((r for r in fleet.get("triggers", []) if r["id"] == action["triggerId"]), None)
        if not rule or rule_signature(rule) != action["ruleSignature"]:
            if rule and not rule["enabled"]:
                action.update(status="held", detail="Automation is paused or this trigger is disabled.")
            else:
                action.update(status="needs-attention", detail="The deployed trigger changed. Review this unsent handoff against the current workflow.")
            return
        if not rule["enabled"]:
            action.update(status="held", detail="This trigger is disabled.")
            return
        if action.get('eventId'):
            from studio_recovery import replay_hold
            reason = replay_hold(self.app, action['fleetId'], action['id'])
            if reason:
                action.update(status='held', detail=reason)
                return
        if action.get('continuationOf'):
            from studio_continuations import hold
            reason = hold(self.app, action['fleetId'], action['assignmentContract'], action['continuationOf'])
            if reason:
                action.update(status='held', detail=reason)
                return
        if action.get('assignmentContract'):
            from studio_contracts import delivery_hold
            reason = delivery_hold(self.app, action['fleetId'], action['assignmentContract'])
            if reason:
                action.update(status='held', detail=reason)
                return
        else:
            from studio_contracts import untyped_hold
            reason = untyped_hold(self.app, action['fleetId'], action.get('targetAgentId'), action.get('ticket', ''))
            if reason:
                action.update(status='held', detail=reason)
                return
        agents = [a for a in fleet["agents"] if a["id"] == action.get("targetAgentId") and rule["targetRoleId"] in a.get("roleIds", [])]
        if agents and (agents[0]["sessionId"] != action.get("targetSessionId") or agents[0]["directory"] != action.get("targetDirectory") or runtime_binding(agents[0]) != action.get("targetRuntime", {})):
            action.update(status="needs-attention", detail="Recipient session or folder changed; review the original handoff before sending.")
            return
        source_ids = [action["sourceAgentId"]] if action.get("sourceAgentId") else []
        if rule["kind"] == "event" and not source_ids and rule.get("deploymentId"):
            action.update(status="needs-attention", detail="Legacy handoff has no source identity. Reconcile existing recipient evidence or repair the original unsent event before considering delivery.")
            return
        reason = contract_problem(fleet, rule) or self.hold_reason(agents, monitor, rule["kind"] == "idle")
        if reason:
            action.update(status="held", detail=reason)
            return
        try:
            with self.app.store.lock:
                if self.app.runtime_fleet(self.app.store.fleet(fleet["id"])) != fleet:
                    action.update(status="held", detail="Workflow configuration changed before dispatch.")
                    return
                preview = self.app.controls.preview({"fleetId": fleet["id"], "agentIds": [a["id"] for a in agents],
                    "action": "message", "message": action["message"],
                    'taskUrl': action.get('ticket', ''),
                    **({'handoffId': action['id']} if action.get('eventId') else {}),
                    **({'assignmentContract': action['assignmentContract']} if action.get('assignmentContract') else {}),
                    **({'continuationOf': action['continuationOf']} if action.get('continuationOf') else {}),
                    **({"deploymentId": rule["deploymentId"], "automation": True, "sourceAgentId": action.get("sourceAgentId"), "idleOnly": rule["kind"] == "idle"} if rule.get("deploymentId") else {})})
                action.update(controlId=preview["id"], status="sending", detail="Dispatch intent recorded; waiting for Codex receipt.")
                if rule["kind"] == "idle":
                    current = self.data["states"].setdefault(rule["id"], {"signature": rule_signature(rule)})
                    current.setdefault("agents", {}).setdefault(action["targetAgentId"], {})["nextDue"] = time.time() + rule["minutes"] * 60
                self.persist()  # Cooldown and dispatch intent precede the side effect.
                self.app.controls.send(preview["id"])
        except Exception as exc:
            receipts = {c["id"] for c in self.app.controls.snapshot(full=True)}
            if action.get("controlId") in receipts:
                action.update(status="needs-attention", detail="Check the dispatch receipt before retrying: " + str(exc))
            else:
                with self.app.controls.lock:
                    self.app.controls.previews.pop(action.pop("controlId", None), None)
                action.update(status="held", detail=str(exc))
        self.persist()

    def tick(self, state, monitor, now=None):
        now = time.time() if now is None else now
        with self.lock:
            before = deepcopy(self.data)
            controls = {c["id"]: c for c in self.app.controls.snapshot(full=True)}
            for action in self.data["actions"]:
                receipt = controls.get(action.get("controlId"))
                if receipt and action["status"] not in ("dismissed", "reconciled"):
                    status = receipt["status"]
                    action["status"] = "sending" if status == "sending" else "queued" if status == "completed" else "needs-attention"
                    action["detail"] = "; ".join(t.get("detail", t["status"]) for t in receipt["targets"])
                    if status != "sending" and all(t["status"] == "held" for t in receipt["targets"]):
                        # A late availability check proves no native send was
                        # attempted. Only this explicit outcome is retryable.
                        action.pop("controlId", None)
                        action["status"] = "held"
                elif action["status"] == "sending":
                    action.update(status="needs-attention", detail="Studio restarted during dispatch. Check the session before retrying.")
                self.deliver(action, monitor)
            from studio_continuations import reconcile
            reconcile(self, state, monitor)
            for draft in state["fleets"]:
                fleet = self.app.runtime_fleet(draft)
                for rule in fleet.get("triggers", []):
                    if not rule["enabled"]:
                        continue  # Pause/resume retains cooldowns.
                    signature = rule_signature(rule)
                    current = self.data["states"].get(rule["id"])
                    if not current or current["signature"] != signature:
                        current = {"signature": signature, "nextDue": now + rule["minutes"] * 60, "agents": {}}
                        self.data["states"][rule["id"]] = current
                        self.observed.pop(rule["id"], None)
                    if rule["kind"] == "idle":
                        agents = [a for a in fleet["agents"] if rule["targetRoleId"] in a.get("roleIds", [])]
                        for agent in agents:
                            schedule = current.setdefault("agents", {}).setdefault(agent["id"], {"nextDue": now})
                            schedule["detail"] = self.hold_reason([agent], monitor, idle_only=True)
                            idle_key = (rule["id"], agent["id"])
                            if schedule["detail"]:
                                self.idle_since.pop(idle_key, None)
                            else:
                                self.idle_since.setdefault(idle_key, now)
                            if now >= schedule["nextDue"] and not schedule["detail"] and now - self.idle_since[idle_key] >= 15:
                                actions = self.fire(fleet, rule, agent["name"] + " is idle; periodic work check", monitor, agent["id"])
                                if actions:
                                    schedule["nextDue"] = now + rule["minutes"] * 60
                        current["nextDue"] = min((s["nextDue"] for s in current.get("agents", {}).values()), default=now + rule["minutes"] * 60)
                    elif rule["kind"] == "timer" and now >= current["nextDue"]:
                        current["nextDue"] = now + rule["minutes"] * 60
                        self.persist()
                        self.fire(fleet, rule, "timer elapsed", monitor)
                    elif rule["kind"] == "condition":
                        if not monitor.get("observedAt") or time.time() - monitor["observedAt"] > 20:
                            self.observed.pop(rule["id"], None)
                            continue
                        sources = [a for a in fleet["agents"] if rule["sourceRoleId"] in a.get("roleIds", [])]
                        observations = {}
                        for agent in sources:
                            item = monitor["sessions"].get(session_key(agent), {})
                            if not item.get("observedAt") or time.time() - item["observedAt"] > 20 or item.get("error") or item.get("status") == "unknown" or (rule["condition"] == "turn-completed" and (not item.get("turnId") or not item.get("turnStatus"))):
                                continue
                            value = (item.get("turnId"), item.get("turnStatus")) if rule["condition"] == "turn-completed" else item.get("status")
                            observations[agent["id"]] = value
                            previous = self.observed.get(rule["id"], {}).get(agent["id"])
                            matched = value[1] == "completed" if isinstance(value, tuple) else value == rule["condition"]
                            if previous is not None and previous != value and matched:
                                self.fire(fleet, rule, agent["name"] + " / " + rule["condition"], monitor)
                        self.observed[rule["id"]] = observations
            # Retain every unresolved delivery. Only completed history is bounded.
            from studio_contracts import current_key
            completed = [a['id'] for a in self.data['actions'] if a['status'] in ('queued', 'dismissed', 'invalidated')
                         and (not a.get('assignmentContract') or a.get('continuationOf')
                              or self.data['currentContracts'].get(current_key(a['fleetId'], a['assignmentContract'])) != a['assignmentContract']['id'])][:-100]
            self.data["actions"] = [a for a in self.data["actions"] if a["id"] not in completed]
            if self.data != before:
                self.persist()

    def report(self, payload, preview=False):
        fleet = self.app.runtime_fleet(self.app.store.fleet(payload.get("fleetId")))
        agent = next((a for a in fleet["agents"] if a["id"] == payload.get("agentId")), None)
        name, event_id = payload.get("eventName"), payload.get("eventId")
        if not agent or not agent["enabled"] or not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Identify an enabled reporting agent and event name.")
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 200:
            raise ValueError("Supply a unique event ID to avoid duplicate dispatch.")
        context = {}
        for key, limit in (("context", 1800), ("ticket", 400), ("artifact", 700), ("targetAgentId", 36)):
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
                raise ValueError(key + " must be plain text of at most " + str(limit) + " characters.")
            context[key] = value.strip()
        if 'assignmentContract' in payload:
            context['assignmentContract'] = deepcopy(payload['assignmentContract'])
        key = fleet["id"] + ":" + agent["id"] + ":" + name + ":" + event_id
        fingerprint = rule_signature(context)
        with self.lock, self.app.store.lock:
            previous = self.data["seenEvents"].get(key) or self.data["seenEvents"].get(fleet["id"] + ":" + agent["id"] + ":" + event_id)
            if previous:
                if previous.get("digest") and previous["digest"] != fingerprint:
                    raise ValueError("This event ID was already used with different recipients or handoff details.")
                if not preview:
                    self.app.progress.count(fleet["id"], "duplicateEvents")
                return {"duplicate": True, "eventId": event_id, "actionIds": previous.get("actionIds", [])}
            rules = [r for r in fleet.get("triggers", []) if r["enabled"] and r["kind"] == "event" and r["eventName"] == name and r["sourceRoleId"] in agent.get("roleIds", [])]
            if not rules:
                raise ValueError("No enabled deployed handoff matches this reporting role and event. Check the active assignment.")
            if 'assignmentContract' in context:
                from studio_contracts import validate, check_new, project_link
                from studio_progress import configuration
                contract = validate(context['assignmentContract'], self.app, self.app.store.fleet(fleet['id']), check_base=True)
                if len(rules) != 1 or context['targetAgentId'] != contract['agentId']:
                    raise ValueError('A typed assignment needs one handoff rule and its explicitly named recipient.')
                if project_link(context['ticket'], configuration(fleet)[0], True) != contract['taskUrl']:
                    raise ValueError('The event ticket must match the assignment contract task.')
                check_new(self.app, fleet, contract, key)
                context['assignmentContract'] = contract
            # Validate all routing before recording anything. No partial broadcasts.
            for rule in rules:
                problem = contract_problem(fleet, rule)
                if problem:
                    raise ValueError(problem)
                targets = self.targets(fleet, rule, context["targetAgentId"])
                if 'assignmentContract' not in context:
                    from studio_contracts import untyped_hold
                    for target in targets:
                        reason = untyped_hold(self.app, fleet['id'], target['id'], context['ticket'])
                        if reason:
                            raise ValueError(reason)
                if rule.get("workflowLinkId") and any(not context[k] for k in ("ticket", "artifact", "context")):
                    raise ValueError("Formal handoffs require --ticket, --artifact and --context (or --context-file).")
            event = {"key": key, "eventId": event_id, "eventName": name, "agent": agent["name"],
                     "createdAt": time.time(), "agentId": agent["id"], **context}
            if preview:
                return {'eventId': event_id, 'assignmentContract': context.get('assignmentContract'),
                        'messages': [self.message(rule, 'reviewed event preview', event) for rule in rules],
                        'targetAgentId': context['targetAgentId'], 'notice': 'Preview only. No event reserved or instruction sent.'}
            before = deepcopy(self.data)
            actions = []
            try:
                for rule in rules:
                    actions += self.fire(fleet, rule, agent["name"] + " reported " + name + " (" + event_id + ")",
                                         {}, context["targetAgentId"], event, dispatch=False)
                if not actions:
                    raise ValueError("Workflow changed before the event could be recorded. Retry the same event ID.")
                self.data["events"].append(event)
                self.data["events"] = self.data["events"][-500:]
                self.data["seenEvents"][key] = {"digest": fingerprint, "actionIds": [a["id"] for a in actions]}
                if event.get('assignmentContract'):
                    from studio_contracts import digest, current_key
                    contract = event['assignmentContract']
                    self.data['contractIds'][fleet['id'] + ':' + contract['id']] = {'digest': digest(contract), 'eventKey': key}
                    self.data['currentContracts'][current_key(fleet['id'], contract)] = contract['id']
                self.persist()  # Event dedupe and all delivery intents commit together.
            except Exception:
                self.data = before
                raise
            monitor = self.app.status()
            for action in actions:
                self.deliver(action, monitor)
            self.persist()
            return {"duplicate": False, "eventId": event_id, "actions": deepcopy(actions)}

    def review(self, action_id):
        with self.lock:
            action = next((a for a in self.data["actions"] if a["id"] == action_id), None)
            if not action or action["status"] not in ("pending", "held", "needs-attention"):
                raise ValueError("This action is not awaiting review.")
            receipts = {r["id"]: r for r in self.app.controls.snapshot(full=True)}
            if action.get("controlId") in receipts or (action.get("controlId") and action["status"] == "needs-attention"):
                raise ValueError("This action has already been dispatched. Check its receipt before creating another instruction.")
            existing = self.app.controls.existing_preview(action.get("controlId"))
            if existing:
                return existing
            fleet = self.app.runtime_fleet(self.app.store.fleet(action["fleetId"]))
            rule = next((r for r in fleet.get("triggers", []) if r["id"] == action["triggerId"]), None)
            if not rule or action.get("ruleSignature") != rule_signature(rule):
                raise ValueError("The deployed trigger changed. Dismiss this old action and report a new event.")
            problem = contract_problem(fleet, rule)
            if problem:
                raise ValueError(problem)
            if rule['kind'] == 'event' and rule.get('deploymentId') and not action.get('sourceAgentId'):
                raise ValueError('Reconcile the missing source identity before reviewing delivery.')
            agents = self.targets(fleet, rule, action.get("targetAgentId"))
            if len(agents) != 1 or not agents[0]["enabled"] or agents[0]["sessionId"] != action.get("targetSessionId") or agents[0]["directory"] != action.get("targetDirectory") or runtime_binding(agents[0]) != action.get("targetRuntime", {}):
                raise ValueError("The original recipient was excluded or rebound. Review current scope and report a new handoff; this action cannot be redirected.")
            preview = self.app.controls.preview({"fleetId": fleet["id"], "action": "message",
                "agentIds": [a["id"] for a in agents], "message": action["message"],
                'taskUrl': action.get('ticket', ''),
                    **({'handoffId': action['id']} if action.get('eventId') else {}),
                **({'assignmentContract': action['assignmentContract']} if action.get('assignmentContract') else {}),
                    **({'continuationOf': action['continuationOf']} if action.get('continuationOf') else {}),
                **({"deploymentId": rule["deploymentId"], "automation": True, "sourceAgentId": action.get("sourceAgentId"), "idleOnly": rule["kind"] == "idle"} if rule.get("deploymentId") else {})})
            action["controlId"] = preview["id"]
            self.persist()
            return preview

    def dismiss(self, action_id):
        with self.lock:
            action = next((a for a in self.data["actions"] if a["id"] == action_id), None)
            if not action or action["status"] not in ("pending", "held", "needs-attention"):
                raise ValueError("This trigger action cannot be dismissed.")
            with self.app.controls.lock:
                if action.get("controlId") in self.app.controls.receipts:
                    raise ValueError("Dispatch already started; check its receipt.")
                self.app.controls.previews.pop(action.get("controlId"), None)
                action["status"] = "dismissed"
            self.persist()
            return deepcopy(action)
