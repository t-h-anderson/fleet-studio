"""Report a formal handoff event to the running Fleet Studio."""
import argparse
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError

def report_event(agent, event, event_id, fleet=None, app_root=None, *, to=None, ticket="", artifact="", context="", contract=None, preview=False):
    root = Path(app_root) if app_root else Path(__file__).resolve().parent
    receipt = json.loads((root / ".runtime/server.json").read_text(encoding="utf-8"))
    url = receipt["url"]
    if not re.fullmatch(r"http://127\.0\.0\.1:\d+", url):
        raise ValueError("Fleet Studio must be running on loopback.")
    with urlopen(url + "/api/health", timeout=5) as response:
        health = json.load(response)
    if health.get("appId") != "codex-fleet-studio" or health.get("instanceId") != receipt["instanceId"]:
        raise ValueError("The saved Fleet Studio instance is no longer running.")
    with urlopen(url + "/api/bootstrap", timeout=5) as response:
        bootstrap = json.load(response)
    state = bootstrap["state"]
    fleets = [f for f in state["fleets"] if (f["id"] == fleet or f["name"] == fleet)] if fleet else [f for f in state["fleets"] if f["id"] == state["defaultFleetId"]]
    if len(fleets) != 1:
        raise ValueError("Choose one existing fleet by name or UUID.")
    chosen = fleets[0]
    agents = [a for a in chosen["agents"] if agent in (a["id"], a["name"], a["sessionId"])]
    if len(agents) != 1:
        raise ValueError("Choose one reporting agent by name, ID or session UUID.")
    payload = {"fleetId": chosen["id"], "agentId": agents[0]["id"], "eventName": event, "eventId": event_id}
    if to:
        targets = [a for a in chosen["agents"] if to in (a["id"], a["name"], a["sessionId"])]
        if len(targets) != 1:
            raise ValueError("Choose one recipient by name, ID or session UUID using --to.")
        payload["targetAgentId"] = targets[0]["id"]
    payload.update(ticket=ticket, artifact=artifact, context=context)
    if contract is not None:
        payload['assignmentContract'] = contract
    request = Request(url + ('/api/events/preview' if preview else '/api/events'), data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", "X-Fleet-Token": bootstrap["token"]})
    with urlopen(request, timeout=30) as response:
        return json.load(response)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--id", required=True, help="Stable unique handoff ID, such as ticket + reviewed commit.")
    parser.add_argument("--fleet")
    parser.add_argument("--to", help="Recipient name or ID; required when the target role has multiple agents.")
    parser.add_argument("--ticket", default="", help="Current ticket or handoff comment URL (up to 400 characters).")
    parser.add_argument("--artifact", default="", help="Exact head and evidence/artifact location (up to 700 characters).")
    parser.add_argument('--contract-file', type=Path, help='Optional versioned JSON assignment envelope; no scripts are executed.')
    parser.add_argument('--preview', action='store_true', help='Validate and display the handoff without reserving an event or sending instructions.')
    context_group = parser.add_mutually_exclusive_group()
    context_group.add_argument("--context", default="", help="Bounded next action and satisfied guard (up to 1800 characters).")
    context_group.add_argument("--context-file", type=Path, help="UTF-8 file containing the handoff instructions.")
    args = parser.parse_args()
    context = args.context_file.read_text(encoding="utf-8-sig") if args.context_file else args.context
    try:
        contract = None
        if args.contract_file:
            if args.contract_file.stat().st_size > 24000:
                raise ValueError('The assignment contract file is too large.')
            contract = json.loads(args.contract_file.read_text(encoding='utf-8-sig'))
        result = report_event(args.agent, args.event, args.id, args.fleet,
                              to=args.to, ticket=args.ticket, artifact=args.artifact, context=context, contract=contract, preview=args.preview)
    except HTTPError as exc:
        parser.exit(1, json.loads(exc.read()).get("error", str(exc)) + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result))

if __name__ == "__main__":
    main()
