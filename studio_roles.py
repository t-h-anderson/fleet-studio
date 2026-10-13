"""Shared role definitions, committed-policy provenance and formal workflow design."""
from copy import deepcopy
import difflib
from pathlib import Path
import re
import subprocess
import uuid

POLICY_PATH = "doc/ROLES_AND_RESPONSIBILITIES.md"
POLICY_URL = "https://gitlab.example/project/-/blob/"



def git(repo, *args):
    run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, encoding="utf-8",
                         errors="replace", timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode:
        raise ValueError("Policy Git operation failed: " + run.stderr.strip()[-700:])
    return run.stdout


def parse_roles(body):
    rows = []
    in_table = False
    for line in body.splitlines():
        if line.startswith("| Role | Responsibilities |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        cells = [cell.strip().replace(r"\|", "|").replace("<br>", "\n")
                 for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) != 3 or cells[0] == "Role" or re.fullmatch(r"[-: ]+", cells[0] or "-"):
            continue
        rows.append({"name": cells[0], "responsibilities": cells[1], "authority": cells[2]})
    if not rows:
        raise ValueError("The committed document has no recognized role table.")
    return rows


def read_policy(repo, refresh=False):
    repo = Path(repo).resolve()
    if refresh:
        git(repo, "fetch", "origin", "main")
    commit = git(repo, "rev-parse", "--verify", "origin/main^{commit}").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("Cannot resolve the policy commit.")
    body = git(repo, "show", commit + ":" + POLICY_PATH)
    return {"repoDirectory": str(repo), "commit": commit, "path": POLICY_PATH,
            "url": POLICY_URL + commit + "/" + POLICY_PATH}, parse_roles(body), body


def key_for(name):
    lowered = name.lower()
    for key in ("project owner", "coordinator", "integrator", "integration preparation",
                "writer", "subagent reviewers", "process monitor", "grad contributor"):
        if lowered.startswith(key):
            return key.replace(" ", "-")
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def make_role(row, index, linked=True):
    positions = {
        "project-owner": (70, 40), "coordinator": (420, 40), "process-monitor": (1120, 40),
        "writer": (70, 300), "subagent-reviewers": (770, 300),
        "integration-preparation": (70, 590), "integrator": (420, 590),
        "grad-contributor": (1120, 300),
    }
    key = key_for(row["name"])
    x, y = positions.get(key, (80 + index % 3 * 350, 60 + index // 3 * 260))
    return {"id": str(uuid.uuid4()), "key": key, **row, "baseline": deepcopy(row) if linked else None,
            "position": {"x": x, "y": y}}


def workflow_defaults(roles):
    by_key = {role["key"]: role["id"] for role in roles}
    specs = [
        ("project-owner", "coordinator", "Set scope and priorities", "A scope or priority decision is needed", "Approved scope and acceptance", "Owner authority; preserve source and publication restrictions"),
        ("coordinator", "writer", "Assign scoped packet", "An eligible, unclaimed packet is ready", "Ticket with source, file scope and unit acceptance", "One writer per owned file"),
        ("writer", "subagent-reviewers", "Request static review", "Implementation, tests and drafts are ready", "Exact diff, source basis, tests and draft documentation", "Read-only reviewers; no independent execution"),
        ("subagent-reviewers", "writer", "Return findings", "Independent review is complete", "Concrete findings and coverage gaps", "Writer fixes findings and renews affected evidence"),
        ("writer", "integrator", "Submit reviewed packet", "Reviewed packet is AI Awaiting Merge", "Exact head, test and analysis evidence and usable drafts", "Integrator checks evidence and records bundle ownership"),
        ("coordinator", "integration-preparation", "Assign bounded group", "Compatible completed packets are identified", "Tickets, heads, exact file scope, custody and return owner", "No queue scoping or shared-file ownership is transferred"),
        ("integration-preparation", "integrator", "Transfer candidate", "Candidate review and evidence map are complete", "Exact candidate plus per-packet drafts and evidence", "Integrator acknowledges custody before overlapping edits"),
        ("integrator", "subagent-reviewers", "Review integrated diff", "Substantive bundle changes are ready", "Integrated diff and evidence applicability", "Read-only; reuse valid completed review"),
        ("subagent-reviewers", "integrator", "Return bundle findings", "Integration review is complete", "Findings, corrections and remaining limits", "Integrator renews checks invalidated by changes"),
        ("integrator", "coordinator", "Escalate scope exception", "A cross-bundle or acceptance exception remains", "Concrete conflict, evidence and requested decision", "Ordinary acceptance and merge remain with Integrator"),
        ("process-monitor", "project-owner", "Propose process improvement", "A workflow bottleneck is established", "Observed bottleneck and proposed improvement", "Monitor cannot change technical acceptance or merge policy"),
    ]
    return [{"id": str(uuid.uuid4()), "source": by_key[source], "target": by_key[target],
             "label": action, "trigger": trigger, "artifact": artifact, "condition": condition}
            for source, target, action, trigger, artifact, condition in specs
            if source in by_key and target in by_key]


def enrich_state(state):
    changed = False
    for fleet in state["fleets"]:
        if "triggers" not in fleet:
            fleet["triggers"] = []
            changed = True
        if "roles" in fleet:
            continue
        changed = True
        source = None
        rows = [
            {"name": "Coordinator", "responsibilities": "Set bounded work, assign owners and resolve blockers.", "authority": "Own scope and assignments."},
            {"name": "Writer", "responsibilities": "Implement assigned work, verify it and resolve review findings.", "authority": "Own only assigned files and scope."},
            {"name": "Subagent reviewers", "responsibilities": "Review changes independently and return concrete findings.", "authority": "Read-only review; implementation stays with the parent."},
            {"name": "Integrator", "responsibilities": "Integrate reviewed work and complete delivery.", "authority": "Own assigned integration and delivery."},
        ]
        fleet["roles"] = [make_role(row, i, bool(source)) for i, row in enumerate(rows)]
        fleet["roleSource"] = source
        fleet["roleLinks"] = workflow_defaults(fleet["roles"])
        role_map = {role["key"]: role["id"] for role in fleet["roles"]}
        for agent in fleet["agents"]:
            key = {"Coordination": "coordinator", "Writer": "writer", "Integration": "integrator",
                   "Preparation": "integration-preparation"}.get(agent.get("role"))
            agent["roleIds"] = [role_map[key]] if key in role_map else []
    return changed


def clean_design(raw, agents):
    def bounded(value, label, limit, empty=True):
        if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
            raise ValueError("Invalid " + label + ".")
        return value.strip()
    def identity(value):
        return str(uuid.UUID(str(value)))
    roles, ids, names = [], set(), set()
    rows = raw.get("roles", [])
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError("A fleet can contain at most 50 roles.")
    for row in rows:
        rid = identity(row.get("id"))
        name = bounded(row.get("name"), "role name", 80, False)
        if rid in ids or name.casefold() in names:
            raise ValueError("Role names and IDs must be unique.")
        ids.add(rid); names.add(name.casefold())
        position = row.get("position", {})
        xy = {}
        for axis in ("x", "y"):
            v = position.get(axis, 60)
            if type(v) not in (int, float) or not -float("inf") < v < float("inf"):
                raise ValueError("Invalid role position.")
            xy[axis] = max(0, min(4000, round(v)))
        baseline = row.get("baseline")
        if baseline is not None:
            baseline = {key: bounded(baseline.get(key), "role baseline", 12000)
                        for key in ("name", "responsibilities", "authority")}
        roles.append({"id": rid, "key": key_for(name), "name": name,
                      "responsibilities": bounded(row.get("responsibilities", ""), "responsibilities", 12000),
                      "authority": bounded(row.get("authority", ""), "authority", 12000),
                      "baseline": baseline, "position": xy,
                      **{k: deepcopy(row[k]) for k in ("specId", "kind", "sources") if k in row}})
    for original, agent in zip(raw.get("agents", []), agents):
        assigned = original.get("roleIds", [])
        if not isinstance(assigned, list) or len(assigned) != len(set(assigned)) or any(rid not in ids for rid in assigned):
            raise ValueError("Assign only existing roles from this fleet.")
        agent["roleIds"] = assigned
    links, link_ids = [], set()
    links_raw = raw.get("roleLinks", [])
    if not isinstance(links_raw, list) or len(links_raw) > 300:
        raise ValueError("A workflow can contain at most 300 relationships.")
    for link in links_raw:
        lid = identity(link.get("id"))
        if lid in link_ids or link.get("source") not in ids or link.get("target") not in ids or link["source"] == link["target"]:
            raise ValueError("Workflow relationships need unique IDs and two distinct existing roles.")
        link_ids.add(lid)
        links.append({**{k: deepcopy(link[k]) for k in ("specId", "sources") if k in link}, "id": lid, "source": link["source"], "target": link["target"],
                      "label": bounded(link.get("label"), "workflow action", 100, False),
                      **{key: bounded(link.get(key, ""), key, 2000) for key in ("trigger", "artifact", "condition")}})
    source = raw.get("roleSource")
    if source:
        if source.get("path") != POLICY_PATH or not re.fullmatch(r"[0-9a-f]{40,64}", source.get("commit", "")):
            raise ValueError("Invalid committed policy reference.")
        source = {"repoDirectory": bounded(source.get("repoDirectory"), "policy repository", 1000, False),
                  "commit": source["commit"], "path": POLICY_PATH,
                  "url": bounded(source.get("url"), "policy URL", 2000, False)}
    return {"roles": roles, "roleLinks": links, "roleSource": source}


def role_changed(role):
    baseline = role.get("baseline")
    return baseline is None or any(role[key] != baseline[key] for key in ("name", "responsibilities", "authority"))


def refresh_roles(fleet):
    source = fleet.get("roleSource")
    if not source:
        raise ValueError("This fleet has no linked project policy.")
    fresh_source, fresh_rows, _ = read_policy(source["repoDirectory"], refresh=True)
    old_rows = parse_roles(git(source["repoDirectory"], "show", source["commit"] + ":" + POLICY_PATH))
    old_by_name = {row["name"]: row for row in old_rows}
    fresh_by_name = {row["name"]: row for row in fresh_rows}
    local_by_base = {role["baseline"]["name"]: role for role in fleet["roles"] if role.get("baseline")}
    locals_new = [role for role in fleet["roles"] if not role.get("baseline")]
    renewed, consumed = [], set()
    def fields(role):
        return {key: role[key] for key in ("name", "responsibilities", "authority")}
    def adopt(row, local=None):
        role = make_role(row, len(renewed))
        if local:
            role["id"], role["position"] = local["id"], deepcopy(local["position"])
        renewed.append(role)
        consumed.add(row["name"])
    for name, original in old_by_name.items():
        local, fresh = local_by_base.get(name), fresh_by_name.get(name)
        if local and local["baseline"] != original:
            raise ValueError("A local role baseline does not match the committed policy.")
        # A renamed local draft may now have been published under its new name.
        published_rename = fresh_by_name.get(local["name"]) if local and local["name"] != name else None
        if published_rename and fields(local) == published_rename and not fresh:
            adopt(published_rename, local)
        elif fresh == original:
            consumed.add(name)
            if local:
                renewed.append(deepcopy(local))
        elif not local and not fresh:
            pass
        elif local and (fields(local) == original or fields(local) == fresh):
            if fresh:
                adopt(fresh, local)
        else:
            raise ValueError("Both the policy and your draft changed role '" + name + "'. Export the proposal and reconcile that role first.")
    for row in fresh_rows:
        if row["name"] in consumed or row["name"] in old_by_name:
            continue
        local = next((role for role in locals_new if role["name"] == row["name"]), None)
        if local and fields(local) != row:
            raise ValueError("The new policy role conflicts with your local definition: " + row["name"])
        adopt(row, local)
    for role in locals_new:
        if role["name"] not in consumed:
            renewed.append(deepcopy(role))
    valid = {role["id"] for role in renewed}
    for agent in fleet["agents"]:
        agent["roleIds"] = [rid for rid in agent.get("roleIds", []) if rid in valid]
    fleet["roles"] = renewed
    fleet["roleLinks"] = [link for link in fleet["roleLinks"] if link["source"] in valid and link["target"] in valid]
    fleet["triggers"] = [rule for rule in fleet.get("triggers", []) if rule["targetRoleId"] in valid and
                        (rule["kind"] == "timer" or rule["sourceRoleId"] in valid)]
    fleet["roleSource"] = fresh_source


def policy_proposal(fleet):
    source = fleet.get("roleSource")
    if not source:
        raise ValueError("Link a committed project policy before preparing a policy patch.")
    body = git(source["repoDirectory"], "show", source["commit"] + ":" + POLICY_PATH)
    original_rows = parse_roles(body)
    original = {row["name"]: row for row in original_rows}
    # A baseline is an identity/provenance reference, not authority to rewrite unrelated text.
    for role in fleet["roles"]:
        baseline = role.get("baseline")
        if baseline and baseline != original.get(baseline["name"]):
            raise ValueError("A role baseline no longer matches the committed policy.")
    lines = body.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("| Role | Responsibilities |")), None)
    if start is None:
        raise ValueError("Cannot locate the responsibilities table.")
    end = start + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    def cell(value):
        return value.replace("|", r"\|").replace("\r", "").replace("\n", "<br>")
    table = ["| Role | Responsibilities | Authority and handoff |\n", "|---|---|---|\n"]
    table += ["| " + " | ".join(cell(role[key]) for key in ("name", "responsibilities", "authority")) + " |\n"
              for role in fleet["roles"]]
    updated = "".join(lines[:start] + table + lines[end:])
    patch = "".join(difflib.unified_diff(body.splitlines(True), updated.splitlines(True),
                                       fromfile="a/" + POLICY_PATH, tofile="b/" + POLICY_PATH))
    return {"patch": patch, "document": updated, "baseCommit": source["commit"], "url": source["url"],
            "changed": bool(patch), "notice": "Draft policy patch. Review consistency with the rest of the policy before committing through the project workflow."}