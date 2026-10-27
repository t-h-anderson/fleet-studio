"""Read your Fleet Studio brief and report done, blocked or hand-off.

    python workflow_client.py instructions --fleet F --agent A
    python workflow_client.py report done --fleet F --agent A --ticket URL --note "what changed"
    python workflow_client.py report blocked --fleet F --agent A --note "what you need"
    python workflow_client.py report handoff --fleet F --agent A --to ROLE --note "what to pick up"
"""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from studio_bindings import same_binding


def post(path, payload, root=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    receipt = json.loads((root / ".runtime/server.json").read_text(encoding="utf-8"))
    url = receipt["url"]
    if not re.fullmatch(r"http://127\.0\.0\.1:\d+", url):
        raise ValueError("Fleet Studio must run on loopback.")
    with urlopen(url + "/api/health", timeout=5) as response:
        health = json.load(response)
    if health.get("appId") != "codex-fleet-studio" or health.get("instanceId") != receipt["instanceId"]:
        raise ValueError("Saved Studio instance no longer matches.")
    with urlopen(url + "/api/bootstrap", timeout=5) as response:
        bootstrap = json.load(response)
    request = Request(url + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "X-Fleet-Token": bootstrap["token"]})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def acknowledge(fleet, agent, release, root=None):
    return post("/api/workflow/acknowledge", {"fleetId": fleet, "agentId": agent, "releaseId": release}, root)


def report(kind, fleet, agent, note, ticket=None, to=None, root=None):
    payload = {"kind": kind, "fleetId": fleet, "agentId": agent, "note": note}
    if ticket:
        payload["ticket"] = ticket
    if to:
        payload["to"] = to
    return post("/api/report", payload, root)


def instructions(fleet_id, agent_id, root=None, directory=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    manifest = json.loads((root / ".runtime/workflows.json").read_text(encoding="utf-8"))
    rid = manifest["active"].get(fleet_id)
    if not rid or not re.fullmatch("[0-9a-f]{32}", rid):
        raise ValueError("No active workflow release for this fleet.")
    if not re.fullmatch("[0-9a-f-]{36}", agent_id):
        raise ValueError("Invalid agent ID.")
    release = json.loads((root / "workflow-releases" / rid / "release.json").read_text(encoding="utf-8"))
    if not any(a["id"] == agent_id for a in release["fleet"]["agents"]):
        raise ValueError("No deployed assignment for this agent.")
    from workflow_spec import assignment_document, digest
    if digest(release["spec"]) != release["digest"]:
        raise ValueError("Release integrity check failed.")
    agent = next(a for a in release["fleet"]["agents"] if a["id"] == agent_id)
    state = json.loads((root / "fleet-studio.json").read_text(encoding="utf-8-sig"))
    current = next((a for f in state["fleets"] if f["id"] == fleet_id for a in f["agents"] if a["id"] == agent_id), None)
    expected = Path(directory or Path.cwd()).resolve()
    if agent.get('hostId', 'local') != 'local' or not same_binding(current, agent) or Path(agent["directory"]).resolve() != expected:
        raise ValueError("This agent folder or session no longer matches the deployed binding. Review and redeploy the assignment.")
    return assignment_document(release["fleet"], release["spec"], agent, rid, Path(__file__).parent)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["instructions", "report", "acknowledge"])
    parser.add_argument("kind", nargs="?", choices=["done", "blocked", "handoff"])
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--note")
    parser.add_argument("--ticket")
    parser.add_argument("--to")
    parser.add_argument("--release")
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.action == "instructions":
        print(instructions(args.fleet, args.agent, directory=args.directory))
    elif args.action == "report":
        if not args.kind or not args.note:
            parser.error("report needs done, blocked or handoff and --note")
        try:
            result = report(args.kind, args.fleet, args.agent, args.note, args.ticket, args.to)
        except HTTPError as exc:
            # One clear line, so an agent does not investigate Studio internals.
            print("Not recorded: " + json.load(exc).get("error", str(exc.code)))
            sys.exit(1)
        print("Recorded " + result["kind"] + (" for " + result["recipient"] if result.get("recipient") else "") + ". Carry on.")
    else:
        if not args.release:
            parser.error("acknowledge requires --release")
        print(json.dumps(acknowledge(args.fleet, args.agent, args.release)))
