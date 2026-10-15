"""Report a milestone without dispatching work or changing the external tracker."""
import argparse
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from studio_progress import KINDS


def report_progress(args, root=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    receipt = json.loads((root / '.runtime/server.json').read_text(encoding='utf-8'))
    url = receipt['url']
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', url):
        raise ValueError('Fleet Studio must run on loopback.')
    with urlopen(url + '/api/health', timeout=5) as response:
        health = json.load(response)
    if health.get('appId') != 'codex-fleet-studio' or health.get('instanceId') != receipt['instanceId']:
        raise ValueError('The saved Studio instance no longer matches.')
    with urlopen(url + '/api/bootstrap', timeout=5) as response:
        bootstrap = json.load(response)
    fleets = [f for f in bootstrap['state']['fleets'] if args.fleet in (f['id'], f['name'])]
    if len(fleets) != 1:
        raise ValueError('Choose one fleet by name or ID.')
    agents = [a for a in fleets[0]['agents'] if args.agent in (a['id'], a['name'])]
    if len(agents) != 1:
        raise ValueError('Choose one reporting agent by name or ID.')
    payload = {'fleetId': fleets[0]['id'], 'agentId': agents[0]['id'], 'sessionId': agents[0]['sessionId'],
               'releaseId': args.release, 'eventId': args.id, 'kind': args.kind, 'taskUrl': args.task,
               'evidenceUrl': args.evidence, 'summary': args.summary, 'reason': args.reason,
               'nextAction': args.next_action, 'handoffId': args.handoff, 'dependsOn': args.depends_on}
    if args.kind == 'test-result':
        payload.update(testStatus=args.test_status, suite=args.suite, revision=args.revision,
                       durationSeconds=args.duration_seconds, passed=args.passed, failed=args.failed, skipped=args.skipped)
    for name in ('goalId', 'completionCriteria', 'blockerCategory', 'blockerOwner', 'resumeCondition'):
        attr = re.sub(r'([A-Z])', lambda m: '_' + m[1].lower(), name)
        if getattr(args, attr, None):
            payload[name] = getattr(args, attr)
    if getattr(args, 'phase', None):
        payload['phase'] = args.phase
    if getattr(args, 'contract_id', None):
        payload['contractId'] = args.contract_id
    if getattr(args, 'revision', None) and args.kind != 'test-result':
        payload['revision'] = args.revision
    if getattr(args, 'completion_evidence_file', None):
        if args.completion_evidence_file.stat().st_size > 24000:
            raise ValueError('Completion evidence file is too large.')
        payload['completionEvidence'] = json.loads(args.completion_evidence_file.read_text(encoding='utf-8-sig'))
    request = Request(url + '/api/progress/report', data=json.dumps(payload).encode('utf-8'),
                      headers={'Content-Type': 'application/json', 'X-Fleet-Token': bootstrap['token']})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fleet', 'agent', 'release', 'id', 'task', 'evidence'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--kind', choices=KINDS, required=True)
    for name in ('summary', 'reason', 'next-action', 'handoff'):
        parser.add_argument('--' + name, default='')
    parser.add_argument('--depends-on', action='append', default=[])
    from studio_assignments import PHASES
    parser.add_argument('--phase', choices=PHASES, help='Optional phase for a progress-updated milestone at a module or substantial-part boundary.')
    parser.add_argument('--contract-id', help='Current typed assignment contract ID, when one was supplied.')
    parser.add_argument('--completion-evidence-file', type=Path, help='JSON object mapping contract requirement IDs to canonical project evidence links.')
    for name in ('goal-id', 'completion-criteria', 'blocker-owner', 'resume-condition'):
        parser.add_argument('--' + name, default='')
    from studio_goals import BLOCKERS
    parser.add_argument('--blocker-category', choices=BLOCKERS)
    parser.add_argument('--test-status', choices=('passed', 'failed', 'blocked', 'running', 'skipped'))
    parser.add_argument('--suite')
    parser.add_argument('--revision', help='Full Git commit SHA tested; describe any working-tree changes in the evidence.')
    parser.add_argument('--duration-seconds', type=float)
    for name in ('passed', 'failed', 'skipped'):
        parser.add_argument('--' + name, type=int)
    parser.add_argument('--data-root', type=Path, help='Studio data directory; defaults to the application folder.')
    args = parser.parse_args()
    try:
        print(json.dumps(report_progress(args, args.data_root)))
    except HTTPError as exc:
        parser.exit(1, json.loads(exc.read()).get('error', 'Report rejected.') + '\n')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
