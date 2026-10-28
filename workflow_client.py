"""Read your Fleet Studio brief and report done, blocked or hand-off.

    python workflow_client.py instructions --fleet F --agent A
    python workflow_client.py report done --fleet F --agent A --ticket URL --note "what changed"
    python workflow_client.py report blocked --fleet F --agent A --note "what you need"
    python workflow_client.py report unblocked --fleet F --agent A --note "what changed"
    python workflow_client.py report handoff --fleet F --agent A --to ROLE --note "what to pick up"
"""
import argparse
import json
from pathlib import Path
import re
import sys
import uuid
from urllib.error import HTTPError, URLError
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


def report(kind, fleet, agent, note, ticket=None, to=None, root=None, retry_id=None):
    payload = {"kind": kind, "fleetId": fleet, "agentId": agent, "note": note, "retryId": retry_id or uuid.uuid4().hex}
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
    here = Path(directory or Path.cwd()).resolve()
    home = Path(agent["directory"]).resolve()
    if agent.get('hostId', 'local') != 'local' or not (here == home or here.is_relative_to(home)):
        raise ValueError("This brief belongs to " + agent["name"] + " in " + str(home) + "; run the command from that folder.")
    # Subfolders are fine, but a nested checkout has its own workflow and must not inherit this one.
    for folder in [here, *here.parents]:
        if folder == home:
            break
        if (folder / ".git").exists():
            raise ValueError(str(folder) + " is a separate checkout; this brief does not apply there.")
    if not same_binding(current, agent):
        raise ValueError("Your Studio binding changed since this brief was published. Carry on with your current task; Studio will send you an updated brief.")
    return assignment_document(release["fleet"], release["spec"], agent, rid, Path(__file__).parent)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["instructions", "report", "acknowledge"])
    parser.add_argument("kind", nargs="?", choices=["done", "blocked", "unblocked", "handoff"])
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--note", help="the note, or - to read it from standard input")
    parser.add_argument("--note-file", help="read the note from this file (avoids shell quoting)")
    parser.add_argument("--ticket")
    parser.add_argument("--to")
    parser.add_argument("--release")
    parser.add_argument("--directory")
    args = parser.parse_args()
    # Every outcome is one plain line, so an agent never goes investigating Studio itself.
    if args.action == "instructions":
        try:
            print(instructions(args.fleet, args.agent, directory=args.directory))
        except ValueError as exc:
            print("Brief unavailable: " + str(exc) + " Carry on with your current task.")
            sys.exit(2)
        except (OSError, KeyError):
            print("Brief unavailable: Studio's files could not be read. Carry on with your current task.")
            sys.exit(3)
    elif args.action == "report":
        note = args.note
        if args.note_file:
            note = Path(args.note_file).read_text(encoding="utf-8")
        elif note == "-":
            note = sys.stdin.read()
        if not args.kind or not (note or "").strip():
            parser.error("report needs done, blocked, unblocked or handoff, and a note")
        retry_id = uuid.uuid4().hex
        result = None
        for attempt in range(2):
            try:
                # The same retry ID makes a resend after a dropped connection harmless.
                result = report(args.kind, args.fleet, args.agent, note.strip(), args.ticket, args.to, retry_id=retry_id)
                break
            except HTTPError as exc:
                try:
                    message = json.load(exc).get("error", str(exc.code))
                except ValueError:
                    message = "Studio returned an error"
                print("Not recorded: " + message)
                sys.exit(1)
            except (URLError, OSError, ValueError):
                if attempt:
                    print("Studio is unavailable, so this was not recorded. Note it in the ticket and carry on.")
                    sys.exit(3)
        next_step = result.get("next") or {"done": "Stop here.", "blocked": "Stop here.", "unblocked": "Carry on with the task.",
                                           "handoff": "Stop work on this; it is theirs now."}[result["kind"]]
        who = " for " + result["recipient"] if result.get("recipient") else ""
        delivery = " (" + result["delivery"] + ")" if result.get("delivery") and result["delivery"] not in ("queued", "recorded") else ""
        print("Recorded " + result["kind"] + who + delivery + ". " + next_step)
    else:
        if not args.release:
            parser.error("acknowledge requires --release")
        print(json.dumps(acknowledge(args.fleet, args.agent, args.release)))
