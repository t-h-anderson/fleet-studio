"""Read bounded current Fleet Studio context; this never dispatches instructions."""
import argparse
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen


def fetch(args):
    root = Path(args.data_root) if args.data_root else Path(__file__).resolve().parent
    saved = json.loads((root / '.runtime/server.json').read_text(encoding='utf-8'))
    url = saved['url']
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', url):
        raise ValueError('Fleet Studio must run on loopback.')
    with urlopen(url + '/api/health', timeout=5) as response:
        health = json.load(response)
    if health.get('appId') != 'codex-fleet-studio' or health.get('instanceId') != saved['instanceId']:
        raise ValueError('Saved Studio instance no longer matches.')
    with urlopen(url + '/api/bootstrap', timeout=5) as response:
        bootstrap = json.load(response)
    fleet = next((f for f in bootstrap['state']['fleets'] if f['id'] == args.fleet), None)
    if not fleet or not any(a['id'] == args.agent for a in fleet['agents']):
        raise ValueError('Choose fleet and agent IDs from the deployed assignment.')
    payload = {'fleetId': args.fleet, 'agentId': args.agent, 'maxChars': args.max_chars, 'cursor': args.cursor, 'since': args.since}
    path = '/api/context'
    if args.handoff:
        payload['handoffId'] = args.handoff
        path += '/handoff'
    request = Request(url + path, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json', 'X-Fleet-Token': bootstrap['token']})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fleet', required=True)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--data-root')
    parser.add_argument('--max-chars', type=int, default=12000)
    parser.add_argument('--cursor')
    parser.add_argument('--since', help='Return only the repeated safety header when the fingerprint is unchanged.')
    parser.add_argument('--handoff', help='Retrieve a full canonical handoff instead of a context page.')
    parser.add_argument('--json', action='store_true', help='Full structured response for inspection; max-chars bounds its text field only.')
    args = parser.parse_args()
    result = fetch(args)
    print(json.dumps(result, indent=2, ensure_ascii=True) if args.json or args.handoff else result['text'], end='' if not (args.json or args.handoff) else '\n')
