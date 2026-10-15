"""Portable workflow model. Definitions live once; local bindings stay in the fleet."""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
import uuid

ROOT = Path(__file__).resolve().parent
ID = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,99}$", "maxLength": 100}
TEXT = {"type": "string", "maxLength": 12000}
SHORT = {"type": "string", "minLength": 1, "maxLength": 100}
REFS = {"type": "array", "items": ID, "maxItems": 300, "uniqueItems": True}

def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

def rows(properties, maximum=300):
    return {"type": "array", "items": obj(properties), "maxItems": maximum}

def choice(*values):
    return {"type": "string", "enum": list(values)}

SCHEMA = obj({
    "schemaVersion": {"const": 1, "type": "integer"}, "id": ID, "name": SHORT,
    "version": SHORT, "description": TEXT,
    "sources": rows({"id": ID, "kind": choice("committed-policy", "owner-direction", "observed-assignment", "proposal"),
                     "url": {"type": "string", "maxLength": 2000, "pattern": "^https://[^\\s]+$"},
                     "revision": TEXT, "note": TEXT}),
    "roles": rows({"id": ID, "name": {"type": "string", "minLength": 1, "maxLength": 80},
                   "kind": choice("agent", "human", "subagent"), "responsibilities": TEXT, "authority": TEXT, "sources": REFS}, 50),
    "handoffs": rows({"id": ID, "source": ID, "target": ID, "action": SHORT,
                      "when": {"type": "string", "maxLength": 2000}, "artifact": {"type": "string", "maxLength": 2000},
                      "guard": {"type": "string", "maxLength": 2000}, "sources": REFS}),
    "states": rows({"id": ID, "name": SHORT, "roles": REFS, "issueState": choice("any", "opened", "closed"),
                    "labelsAny": {"type": "array", "items": SHORT, "maxItems": 100, "uniqueItems": True},
                    "labelPrefixes": {"type": "array", "items": SHORT, "maxItems": 100, "uniqueItems": True},
                    "fallback": {"type": "boolean"}}, 50),
    "transitions": rows({"id": ID, "from": ID, "to": ID, "role": ID, "action": SHORT, "guard": TEXT, "sources": REFS}),
    "policies": rows({"id": ID, "name": SHORT, "roles": REFS, "text": {"type": "string", "maxLength": 60000}, "sources": REFS}),
    "triggers": rows({"id": ID, "name": SHORT, "kind": choice("event", "condition", "timer", "idle"),
                      "source": {"type": "string", "maxLength": 100}, "target": ID, "event": SHORT,
                      "condition": choice("turn-completed", "idle", "attention", "closed"),
                      "minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                      "message": {"type": "string", "minLength": 1, "maxLength": 12000},
                      "mode": choice("automatic", "review"), "handoff": {"type": "string", "maxLength": 100},
                      "contract": {"type": "string", "pattern": "(?:[0-9a-f]{64}|stale)?", "maxLength": 64}}, 100),
    "gaps": rows({"id": ID, "summary": TEXT, "roles": REFS, "blocking": {"type": "boolean"},
                  "resolution": choice("open", "accepted"), "sources": REFS})
})
SCHEMA.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Fleet Studio workflow v1"})
# Optional for backward-compatible reading of immutable v1.0/v1.1 releases.
SCHEMA["properties"]["triggers"]["items"]["properties"]["recipientMode"] = choice("single", "all")
# Optional role runtime constraint: which agent backends and locations may hold the role.
# An empty backend list allows any backend. Absent means unconstrained, so earlier releases keep their digests.
RUNTIME = obj({"backends": {"type": "array", "items": choice("codex", "claude-code"), "maxItems": 2, "uniqueItems": True},
               "location": choice("any", "local", "remote")})
SCHEMA["properties"]["roles"]["items"]["properties"]["runtime"] = RUNTIME
SCHEMA["properties"]["states"]["items"]["properties"]["acceptsDelivery"] = {"type": "boolean"}


def validate_node(value, schema, at="$", depth=0):
    if depth > 20:
        raise ValueError("Workflow nesting is too deep.")
    typ = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if typ and type(value) is not types[typ]:
        raise ValueError(at + " must be " + typ)
    if "const" in schema and value != schema["const"]:
        raise ValueError(at + " has an unsupported version")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(at + " has an unsupported value")
    if typ == "object":
        extra = set(value) - set(schema["properties"])
        missing = set(schema["required"]) - set(value)
        if extra or missing:
            raise ValueError(at + ": unknown fields " + str(sorted(extra)) + "; missing fields " + str(sorted(missing)))
        for key, child in value.items():
            validate_node(child, schema["properties"][key], at + "." + key, depth + 1)
    elif typ == "array":
        if len(value) > schema.get("maxItems", 1000):
            raise ValueError(at + " has too many entries")
        if schema.get("uniqueItems") and len(set(value)) != len(value):
            raise ValueError(at + " contains duplicates")
        for i, child in enumerate(value):
            validate_node(child, schema["items"], at + "[" + str(i) + "]", depth + 1)
    elif typ == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 60000):
            raise ValueError(at + " has invalid length")
        if value != value.strip():
            raise ValueError(at + " has leading or trailing whitespace; remove it before import")
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError(at + " contains control characters")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ValueError(at + " has invalid format")
    elif typ == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise ValueError(at + " is outside its allowed range")


def validate(spec):
    validate_node(spec, SCHEMA)
    indices = {}
    for section in ("roles", "handoffs", "states", "transitions", "policies", "triggers", "sources", "gaps"):
        index = {r["id"]: r for r in spec[section]}
        if len(index) != len(spec[section]):
            raise ValueError("Duplicate IDs in " + section)
        indices[section] = index
    if not spec["roles"]:
        raise ValueError("A workflow needs roles.")
    for state in spec['states']:
        if state.get('acceptsDelivery') and (state['issueState'] != 'closed' or state['fallback']):
            raise ValueError('An acceptance rule must explicitly match closed tracker tasks and cannot be the fallback.')
    if len({r["name"].casefold() for r in spec["roles"]}) != len(spec["roles"]):
        raise ValueError("Role names must be unique.")
    def ref(section, value):
        if value not in indices[section]:
            raise ValueError("Unknown " + section + " reference: " + str(value))
    for section in indices:
        for row in spec[section]:
            for value in row.get("sources", []):
                ref("sources", value)
            for value in row.get("roles", []):
                ref("roles", value)
    for row in spec["handoffs"]:
        ref("roles", row["source"]); ref("roles", row["target"])
        if row["source"] == row["target"]:
            raise ValueError("A handoff needs two distinct roles.")
    for row in spec["transitions"]:
        ref("states", row["from"]); ref("states", row["to"]); ref("roles", row["role"])
    for row in spec["triggers"]:
        ref("roles", row["target"])
        if row["kind"] not in ("timer", "idle") or row["source"]:
            ref("roles", row["source"])
        if row["message"].lstrip().startswith(("/", "!")):
            raise ValueError("Triggers must contain plain instructions.")
        if row["handoff"]:
            ref("handoffs", row["handoff"])
            handoff = indices["handoffs"][row["handoff"]]
            if (row["source"], row["target"]) != (handoff["source"], handoff["target"]):
                raise ValueError("Trigger endpoints must match its handoff.")
        if any(indices["roles"].get(row[k], {}).get("kind") == "subagent" for k in ("source", "target")):
            raise ValueError("Reviewer subagents are managed by their parent, not fleet triggers.")
    if spec["states"]:
        if sum(s["fallback"] for s in spec["states"]) != 1 or not spec["states"][-1]["fallback"]:
            raise ValueError("Ticket states need exactly one fallback, placed last (first match wins).")
        if any(s["fallback"] and (s["labelsAny"] or s["labelPrefixes"] or s["issueState"] != "any") for s in spec["states"]):
            raise ValueError("The fallback must match any ticket.")
    return deepcopy(spec)


def stable(row, prefix):
    return row.get("specId") or prefix + "-" + row["id"].replace("-", "")


def metadata(spec):
    return {k: deepcopy(v) for k, v in spec.items() if k not in ("roles", "handoffs", "triggers")}


def contract_digest(handoff):
    return digest({k: handoff[k] for k in ("source", "target", "action", "when", "artifact", "guard")})


def export_spec(fleet):
    from studio_workflow import contract_problem
    meta = deepcopy(fleet.get("workflow") or {"schemaVersion": 1, "id": "workflow-" + fleet["id"].replace("-", ""),
        "name": fleet["name"], "version": "1.0.0", "description": fleet.get("description", ""),
        "sources": [], "states": [], "transitions": [], "policies": [], "gaps": []})
    rid = {r["id"]: stable(r, "role") for r in fleet["roles"]}
    hid = {h["id"]: stable(h, "handoff") for h in fleet["roleLinks"]}
    if any(t.get("workflowLinkId") and t["workflowLinkId"] not in hid for t in fleet.get("triggers", [])):
        raise ValueError("A trigger links to a removed handoff. Remove or rebuild that trigger before exporting or deploying.")
    meta["roles"] = [{"id": rid[r["id"]], "name": r["name"], "kind": r.get("kind", "subagent" if r.get("key") == "subagent-reviewers" else "human" if r.get("key") == "project-owner" else "agent"),
                      "responsibilities": r["responsibilities"], "authority": r["authority"], "sources": r.get("sources", []),
                      **({"runtime": deepcopy(r["runtime"])} if r.get("runtime") else {})} for r in fleet["roles"]]
    meta["handoffs"] = [{"id": hid[h["id"]], "source": rid[h["source"]], "target": rid[h["target"]],
                         "action": h["label"], "when": h["trigger"], "artifact": h["artifact"], "guard": h["condition"],
                         "sources": h.get("sources", [])} for h in fleet["roleLinks"]]
    meta["triggers"] = [{**({"recipientMode": t["recipientMode"]} if "recipientMode" in t else {}), "id": stable(t, "trigger"), "name": t["name"], "kind": t["kind"], "source": rid.get(t["sourceRoleId"], ""),
                        "target": rid[t["targetRoleId"]], "event": t["eventName"], "condition": t["condition"], "minutes": t["minutes"],
                        "message": t["message"], "mode": t["mode"], "handoff": hid.get(t.get("workflowLinkId"), ""),
                        "contract": (t.get("specContract", "stale") if contract_problem(fleet, t) else contract_digest(next(h for h in meta["handoffs"] if h["id"] == hid[t["workflowLinkId"]]))) if t.get("workflowLinkId") else ""} for t in fleet.get("triggers", [])]
    return validate(meta)


def apply_spec(fleet, spec):
    from studio_roles import key_for
    from studio_workflow import signature
    spec = validate(spec)
    updated = deepcopy(fleet)
    def match(rows, incoming, prefix, name_field):
        found = next((r for r in rows if stable(r, prefix) == incoming["id"]), None)
        if not found and not fleet.get("workflow"):
            candidates = [r for r in rows if r.get(name_field) == incoming.get("name", incoming.get("action"))]
            if len(candidates) == 1:
                found = candidates[0]
        return found
    roles = []
    for i, role in enumerate(spec["roles"]):
        old = match(fleet["roles"], role, "role", "name")
        roles.append({"id": old["id"] if old else str(uuid.uuid4()), "specId": role["id"], "key": key_for(role["name"]),
                      **{k: role[k] for k in ("name", "kind", "responsibilities", "authority", "sources")},
                      **({"runtime": deepcopy(role["runtime"])} if "runtime" in role else {}),
                      "baseline": deepcopy(old.get("baseline")) if old else None,
                      "position": deepcopy(old["position"]) if old else {"x": 80 + i % 3 * 360, "y": 80 + i // 3 * 260}})
    rmap = {r["specId"]: r["id"] for r in roles}
    valid = set(rmap.values())
    bound = [a["name"] for a in fleet["agents"] if set(a.get("roleIds", [])) - valid]
    if bound:
        raise ValueError("Import would remove roles assigned to: " + ", ".join(bound) + ". Reassign those agents first.")
    links = []
    for handoff in spec["handoffs"]:
        old = match(fleet["roleLinks"], handoff, "handoff", "label")
        links.append({"id": old["id"] if old else str(uuid.uuid4()), "specId": handoff["id"], "source": rmap[handoff["source"]],
                      "target": rmap[handoff["target"]], "label": handoff["action"], "trigger": handoff["when"],
                      "artifact": handoff["artifact"], "condition": handoff["guard"], "sources": handoff["sources"]})
    hmap = {h["specId"]: h for h in links}
    triggers = []
    for trigger in spec["triggers"]:
        old = match(fleet.get("triggers", []), trigger, "trigger", "name")
        handoff = hmap.get(trigger["handoff"])
        triggers.append({**({"recipientMode": trigger["recipientMode"]} if "recipientMode" in trigger else {}), "id": old["id"] if old else str(uuid.uuid4()), "specId": trigger["id"], "name": trigger["name"],
            "kind": trigger["kind"], "specContract": trigger["contract"], "sourceRoleId": rmap.get(trigger["source"], ""), "targetRoleId": rmap[trigger["target"]],
            "eventName": trigger["event"], "condition": trigger["condition"], "minutes": trigger["minutes"],
            "message": trigger["message"], "mode": trigger["mode"], "enabled": False,
            **({"workflowLinkId": handoff["id"], "workflowSignature": (signature(handoff) if trigger["contract"] == contract_digest(next(h for h in spec["handoffs"] if h["id"] == trigger["handoff"])) else "0" * 64)} if handoff else {})})
    updated.update(roles=roles, roleLinks=links, triggers=triggers, workflow=metadata(spec))
    return updated


def diff(before, after):
    result = {}
    for section in ("roles", "handoffs", "states", "transitions", "policies", "triggers", "sources", "gaps"):
        old, new = ({r["id"]: r for r in s[section]} for s in (before, after))
        result[section] = {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
                           "changed": sorted(k for k in new.keys() & old.keys() if new[k] != old[k])}
        if list(old) != list(new):
            result[section]["orderChanged"] = True
    result["metadata"] = {k: {"before": before[k], "after": after[k]} for k in ("id", "name", "version", "description") if before[k] != after[k]}
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def render(spec, role_ids=None):
    spec = validate(spec)
    lines = ["# " + spec["name"], "", "Workflow " + spec["id"] + " / " + spec["version"] + " / SHA-256 " + digest(spec), "",
             "Generated from workflow.json. Edit the workflow in Fleet Studio, then review and deploy a new release.", "",
             "This deployed workflow replaces the workflow rules in its historical source documents. References below to former roles, backlog and handoff documents identify migration sources; the definitions in this release control workflow behavior. Continue following applicable engineering standards, source/publication restrictions and explicit ticket/host stops. Report conflicts before acting.", "", spec["description"], "",
             "Prose guards and ticket transitions are agent responsibilities. Studio evaluates only the configured event, session-condition and timer triggers; a notification is not technical acceptance.", ""]
    names = {r["id"]: r["name"] for r in spec["roles"]}
    for role in spec["roles"]:
        if role_ids is None or role["id"] in role_ids:
            lines += ["## Role: " + role["name"] + " [" + role["id"] + "]", "", "Kind: " + role["kind"], "", role["responsibilities"], "", "Authority: " + role["authority"], ""]
            if "runtime" in role:
                lines += ["Runtime: " + (", ".join(role["runtime"]["backends"]) or "any backend") + "; location " + role["runtime"]["location"], ""]
    lines += ["## Handoffs", ""]
    for h in spec["handoffs"]:
        if role_ids is None or h["source"] in role_ids or h["target"] in role_ids:
            lines += ["### " + h["action"] + " [" + h["id"] + "]", "", names[h["source"]] + " -> " + names[h["target"]], "",
                      "When: " + h["when"], "", "Artifact: " + h["artifact"], "", "Required guard: " + h["guard"], ""]
    lines += ["## Ticket states (first matching rule wins)", ""]
    for s in spec["states"]:
        lines += ["- " + s["id"] + ": " + s["name"] + "; queue roles: " + ", ".join(s["roles"]) + "; " +
                  ("fallback" if s["fallback"] else s["issueState"] + " / labels " + ", ".join(s["labelsAny"]) + " / prefixes " + ", ".join(s["labelPrefixes"])) +
                  ("; establishes accepted delivery" if s.get('acceptsDelivery') else "")]
    lines += ["", "## Ticket transitions", ""]
    for t in spec["transitions"]:
        lines += ["- " + t["id"] + ": " + t["from"] + " -> " + t["to"] + "; " + t["action"] + " by " + names[t["role"]] , "  Guard: " + t["guard"]]
    lines += ["", "## Agent policies", ""]
    for p in spec["policies"]:
        if role_ids is None or not p["roles"] or set(role_ids) & set(p["roles"]):
            lines += ["### " + p["name"] + " [" + p["id"] + "]", "", p["text"], ""]
    lines += ["## Trigger templates", ""]
    for t in spec["triggers"]:
        lines += ["### " + t["name"] + " [" + t["id"] + "]", "", t["kind"] + "; event=" + t["event"] + "; condition=" + t["condition"] +
                  "; interval=" + str(t["minutes"]) + " minutes; delivery=" + t["mode"] +
                  ("; recipients=" + t.get("recipientMode", "single") if t["kind"] == "event" else ""), "", t["message"], ""]
    lines += ["## Mapping gaps", ""]
    for g in spec["gaps"]:
        lines += ["- " + g["id"] + " (" + g["resolution"] + (", blocks deployment" if g["blocking"] else "") + "): " + g["summary"]]
    lines += ["", "## Sources", ""]
    for source in spec["sources"]:
        lines += ["- " + source["id"] + " [" + source["kind"] + "]: " + source["url"] + " (" + source["revision"] + "). " + source["note"]]
    lines += ["", "## Design diagram", "", "```mermaid", "flowchart LR"]
    def safe(value):
        return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
    for i, role in enumerate(spec["roles"]):
        lines.append("  n" + str(i) + '["' + safe(role["name"]) + '"]')
    nodes = {r["id"]: "n" + str(i) for i, r in enumerate(spec["roles"])}
    for h in spec["handoffs"]:
        lines.append("  " + nodes[h["source"]] + ' -->|"' + safe(h["action"]) + '"| ' + nodes[h["target"]])
    lines += ["```", ""]
    return "\n".join(line.rstrip() for line in lines)


def assignment_document(fleet, spec, agent, release_id, app_root):
    from studio_trackers import tracker_label
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    by_id = {r["id"]: stable(r, "role") for r in fleet["roles"]}
    roles = [by_id[r] for r in agent.get("roleIds", [])]
    lines = ["# Assignment: " + agent["name"], "", "Agent ID: " + agent["id"], "Release: " + release_id, "",
             "Individual scope: " + agent.get("notes", ""), "", "## Workflow event reporting", "",
             "Only report a handoff after its artifact and guard are satisfied and the trigger is enabled in this release. Use the same stable event ID and identical details for retries of the same logical handoff (ticket + exact head + handoff); do not create a new ID to retry delivery. Name the actual recipient with --to; multiple Writers must never receive the same assignment by accident. Include the current ticket/comment, exact candidate/evidence artifact, and bounded next action. A report notifies the target; it does not grant acceptance or revoke a stop. Held deliveries retry automatically while this release remains active; uncertain dispatch outcomes require inspection.", "",
             "Recurring idle checks are scheduled by the Studio server. Check current authorized work and handoffs, then finish the check if none is eligible; do not run your own polling/sleep loop or post repeated no-change ticket comments. Explicit pauses, blocked/limited goals and custody remain in force. " + tracker_label(fleet) + " remains the task record and fallback when Studio is unavailable.", ""]
    for trigger in fleet.get("triggers", []):
        if trigger["kind"] == "event" and trigger["sourceRoleId"] in agent.get("roleIds", []):
            command = ("python " + quote(Path(app_root) / "fleet_event.py") + " --fleet " + fleet["id"] + " --agent " + agent["id"] +
                       " --event " + quote(trigger["eventName"]) + " --id '<ticket>-<exact-head>-<handoff>' --to '<recipient-name-or-id>'" +
                       " --ticket '<current-ticket-or-comment-URL>' --artifact '<exact-head-and-evidence-location>' --context '<bounded next action and satisfied guard>'")
            targets = [a["name"] for a in fleet["agents"] if trigger["targetRoleId"] in a.get("roleIds", [])]
            lines += ["### " + trigger["name"] + (" (enabled)" if trigger["enabled"] else " (disabled; wait for a deployment enabling it)"), "", "Eligible recipients: " + ", ".join(targets), "", "```powershell", command, "```", ""]
    if any(p['id'] == 'progress-reporting' for p in spec.get('policies', [])):
        command = ("python " + quote(Path(app_root) / "fleet_progress.py") + " --fleet " + fleet['id'] + " --agent " + agent['id'] +
                   " --release " + release_id + " --id '<stable-milestone-id>' --kind task-started --task '<ticket-URL>' --evidence '<evidence-comment-URL>' --summary '<meaningful progress>'")
        lines += ["## Progress reports", "", "Report meaningful milestones after recording tracker evidence. Kinds: task-started, progress-updated, blocked, unblocked, handoff-received, work-finished, test-result. Report existing local test evidence with test-result, --suite, --test-status, --revision (full tested commit SHA), and optional --passed/--failed/--skipped counts and --duration-seconds. Explain scope and any uncommitted changes in the evidence. A test report never authorizes a new test run or overrides runtime custody. Blocked reports require --reason and --next-action. Receipts require --handoff with the delivery action ID. Use --depends-on for an explicitly reported dependency. A work-finished report does not close or accept a task. Reuse an event ID and identical details when retrying. If Studio is unavailable, keep the tracker current and retry at the next safe boundary.", "", "```powershell", command, "```", ""]
        lines += ['Optional: use --kind progress-updated --phase implementing, verifying or submitted at a module/substantial-part milestone. Supply --blocker-owner and --resume-condition for a blocker even without a native goal. No per-function report or test gate is required. These observations never start verification, change task ownership or resume a stopped goal.', '']
    if any(p['id'] == 'bounded-goals' for p in spec.get('policies', [])):
        lines += ['## Bounded goal evidence', '',
                  'When explicitly authorized to use a native goal, define one bounded work packet with a ticket and completion criterion. Roles remain in the workflow. In Progress > Goals, link the exact observed goal ID to the ticket, or add --goal-id <native-goal-ID-shown-in-Studio> and --completion-criteria <done-condition> to your first task-started/progress-updated report. Later substantive reports use the same --goal-id. Goal-linked blocked reports also require --blocker-category (dependency, approval, restricted, host, capacity or other), --reason, --next-action, --blocker-owner and --resume-condition; use --depends-on for dependency links. Keep task evidence in the tracker. Missing native observations are unknown, not proof of completion.', '',
                  'Complete only the bounded goal whose criterion has actually been met. Never clear or resume a stopped goal merely to accept new work. Report resolved dependencies with unblocked and evidence; that report does not activate a native goal. Operator complete/cancel/resume actions are separately reviewed with ticket evidence and require the session to be closed with an empty queue. Budget/usage-limited goals cannot be resumed by this control. Keep handoff-received and test-result reports separate: omit --goal-id. Receipts and adoption acknowledgements remain possible during stops and do not resume technical work.', '']
    lines += ["## Acknowledge adoption", "", "After reading and accepting your assignment, acknowledge this release yourself. Report conflicts before acknowledging.", "", "```powershell",
              "python " + quote(Path(app_root) / "workflow_client.py") + " acknowledge --fleet " + fleet["id"] + " --agent " + agent["id"] + " --release " + release_id,
              "```", "", render(spec, roles)]
    return "\n".join(lines)


def runtime_problems(fleet):
    """Enabled agents whose backend or host breaks a role's runtime constraint."""
    roles = {r["id"]: r for r in fleet.get("roles", [])}
    problems = []
    for agent in fleet.get("agents", []):
        if not agent.get("enabled", True):
            continue
        backend, remote = agent.get("backend", "codex"), agent.get("hostId", "local") != "local"
        if remote and agent.get("sessionId"):
            problems.append(agent["name"] + " is remote: managed workflow deployment requires remote assignment and receipt transport, which is not supported yet. Local monitoring and explicit remote controls remain available.")
        for rid in agent.get("roleIds", []):
            runtime = roles.get(rid, {}).get("runtime")
            if not runtime:
                continue
            if runtime["backends"] and backend not in runtime["backends"]:
                problems.append(agent["name"] + " uses " + backend + " but " + roles[rid]["name"] + " requires " + " or ".join(runtime["backends"]))
            if runtime["location"] != "any" and remote != (runtime["location"] == "remote"):
                problems.append(agent["name"] + " must run " + runtime["location"] + "ly for " + roles[rid]["name"])
    return problems


def project_ticket(ticket, spec):
    for s in spec.get("states", []):
        labels = ticket.get("labels", [])
        state_match = s["issueState"] == "any" or ticket["state"] == s["issueState"]
        labels_match = not (s["labelsAny"] or s["labelPrefixes"]) or bool(set(labels) & set(s["labelsAny"])) or any(l.startswith(p) for p in s["labelPrefixes"] for l in labels)
        if s["fallback"] or state_match and labels_match:
            return s
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate and render portable Fleet Studio workflows")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    spec = validate(json.loads(args.spec.read_text(encoding="utf-8-sig")))
    document = render(spec)
    if args.check:
        if not args.output or not args.output.exists() or args.output.read_text(encoding="utf-8") != document:
            parser.exit(1, "Generated workflow document differs. Render it again.\n")
    elif args.output:
        args.output.write_text(document, encoding="utf-8")
    print("Valid workflow: " + spec["id"] + " / " + digest(spec))
