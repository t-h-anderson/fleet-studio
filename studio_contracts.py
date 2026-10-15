"""Optional, declarative assignment envelopes. Validation grants no authority."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit, unquote

from studio_bindings import runtime_binding, same_binding
from studio_trackers import issue_prefix
from workflow_spec import obj, rows, choice, validate_node

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1000}
ID = {'type': 'string', 'pattern': '^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$', 'maxLength': 100}
SHA = {'type': 'string', 'pattern': '^(?:[0-9a-f]{40}|[0-9a-f]{64})?$', 'maxLength': 64}
SCHEMA = obj({
    'schemaVersion': {'type': 'integer', 'const': 1}, 'id': ID, 'taskUrl': TEXT,
    'agentId': TEXT, 'releaseId': TEXT,
    'binding': obj({'sessionId': TEXT, 'directory': TEXT,
                    'roleIds': {'type': 'array', 'items': TEXT, 'maxItems': 50, 'uniqueItems': True},
                    'backend': choice('codex', 'claude-code'), 'hostId': TEXT}),
    'scope': {'type': 'array', 'items': TEXT, 'maxItems': 50, 'uniqueItems': True},
    'deliverable': TEXT, 'requirements': rows({'id': ID, 'description': TEXT}, 20),
    'dependencies': {'type': 'array', 'items': TEXT, 'maxItems': 20, 'uniqueItems': True},
    'evidence': {'type': 'array', 'items': TEXT, 'maxItems': 20, 'uniqueItems': True},
    'nextAgentId': {'type': 'string', 'maxLength': 100},
    'baseRevision': SHA, 'candidateRevision': SHA,
})
SCHEMA['properties']['supersedes'] = ID
SCHEMA['properties']['continuation'] = obj({
    'authorized': {'type': 'boolean', 'const': True},
    'when': {'type': 'string', 'const': 'dependencies-accepted'},
    'message': {'type': 'string', 'minLength': 1, 'maxLength': 1800},
})


def digest(contract):
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def project_link(value, config, ticket=False):
    """Require canonical links in this project; query strings may carry secrets."""
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ValueError('Contract links must be HTTPS project references.')
    parsed, project = urlsplit(value), urlsplit(config.get('projectUrl', ''))
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.netloc.lower() != project.netloc.lower() or not parsed.path.startswith(project.path.rstrip('/') + '/')
            or '%' in parsed.path or '\\' in value or any(part in ('.', '..') for part in parsed.path.split('/'))
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Contract links must stay in the configured HTTPS project without credentials, queries or traversal.')
    if ticket and not re.fullmatch(re.escape(urlsplit(issue_prefix(config)).path) + r'[1-9][0-9]*', parsed.path):
        raise ValueError('Contract task and dependency links must identify a tracker issue in this project.')
    return urlunsplit(('https', parsed.netloc.lower(), parsed.path, '', '' if ticket else parsed.fragment))


def validate(contract, app, fleet, *, check_base=False, revalidated=False):
    validate_node(contract, SCHEMA, '$assignment')
    if len(json.dumps(contract)) > 12000:
        raise ValueError('Keep the assignment envelope within 12000 characters; use canonical evidence links for detail.')
    value = deepcopy(contract)
    from studio_progress import configuration
    config, _ = configuration(fleet)
    value['taskUrl'] = project_link(value['taskUrl'], config, True)
    value['dependencies'] = [project_link(u, config, True) for u in value['dependencies']]
    value['evidence'] = [project_link(u, config) for u in value['evidence']]
    if value['taskUrl'] in value['dependencies'] or len(set(value['dependencies'])) != len(value['dependencies']):
        raise ValueError('Dependencies must be distinct issues other than the assigned task.')
    if value.get('continuation') and (not value['dependencies'] or not value['continuation']['message'].strip()
                                      or value['continuation']['message'].lstrip().startswith(('/', '!'))):
        raise ValueError('A continuation needs dependencies and bounded plain instructions.')
    if not value['scope'] or not value['requirements']:
        raise ValueError('An assignment contract needs bounded scope and at least one completion requirement.')
    for scope in value['scope']:
        path = PurePosixPath(scope)
        if scope == '.' or path.is_absolute() or scope != path.as_posix() or any(p in ('.', '..') for p in path.parts) or any(c in scope for c in ':\\*?') or any(ord(c) < 32 for c in scope):
            raise ValueError('Contract scope uses explicit relative files or directories without traversal or wildcards.')
    ids = [r['id'] for r in value['requirements']]
    if len(set(ids)) != len(ids):
        raise ValueError('Completion requirement IDs must be unique.')
    rid = app.deployments.data['active'].get(fleet['id'])
    from studio_recovery import effective_release
    if not rid or (value['releaseId'] != rid and (not revalidated or effective_release(app, fleet['id'], value) != rid)):
        raise ValueError('The assignment contract must name the active workflow release.')
    agent = next((a for a in fleet['agents'] if a['id'] == value['agentId']), None)
    deployed = next((a for a in app.deployments.release(rid)['fleet']['agents'] if a['id'] == value['agentId']), None)
    if (not agent or not agent['enabled'] or not deployed or not deployed['enabled']
            or not same_binding(agent, deployed, ('sessionId', 'directory', 'roleIds'))
            or not same_binding(value['binding'], agent, ('sessionId', 'directory', 'roleIds'))):
        raise ValueError('Assignment contract recipient, session, folder or roles no longer match the deployed binding.')
    if value['nextAgentId'] and not any(a['id'] == value['nextAgentId'] and a['enabled'] for a in fleet['agents']):
        raise ValueError('The next recipient must be an enabled fleet agent, or omitted with an empty ID.')
    if check_base and value['baseRevision']:
        if agent.get('hostId', 'local') != 'local':
            raise ValueError('A remote checkout base cannot be verified by this local contract validator.')
        try:
            result = subprocess.run(['git', '-C', agent['directory'], 'rev-parse', '--verify', 'HEAD'], capture_output=True,
                                    text=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Could not verify the assigned checkout base revision.') from None
        if result.returncode or result.stdout.strip() != value['baseRevision']:
            raise ValueError('The assigned checkout no longer has the contract base revision; review the assignment.')
    return value


def current(db, fid, agent_id, task_url):
    rows = [json.loads(r['data']) for r in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fid,))]
    typed = [h for h in rows if not h.get('continuationOf') and h.get('assignmentContract') and h.get('targetAgentId') == agent_id
             and h['assignmentContract']['taskUrl'] == task_url]
    superseded = {h['assignmentContract'].get('supersedes') for h in typed}
    return max((h for h in typed if h['assignmentContract']['id'] not in superseded), key=lambda h: (h['createdAt'], h['id']), default=None)


def current_key(fid, contract):
    return json.dumps([fid, contract['agentId'], contract['taskUrl']], separators=(',', ':'))


def registered_task(app, fid, agent_id, task):
    try:
        url = urlsplit(task)
        canonical = urlunsplit((url.scheme.lower(), url.netloc.lower(), unquote(url.path).rstrip('/'), '', ''))
    except (TypeError, ValueError):
        return None
    key = current_key(fid, {'agentId': agent_id, 'taskUrl': canonical})
    return canonical if key in app.triggers.data.get('currentContracts', {}) else None


def untyped_hold(app, fid, agent_id, task):
    if registered_task(app, fid, agent_id, task):
        return 'A typed assignment contract is current for this recipient/task; reconcile this untyped handoff or supply an explicit replacement contract.'
    return ''


def check_new(app, fleet, contract, event_key):
    known = app.triggers.data.get('contractIds', {}).get(fleet['id'] + ':' + contract['id'])
    if known and (known['digest'] != digest(contract) or known['eventKey'] != event_key):
        raise ValueError('This contract ID already belongs to another envelope or event. Retry its original event or use a new ID.')
    old = app.triggers.data.get('currentContracts', {}).get(current_key(fleet['id'], contract))
    if old and old != contract['id']:
        if contract.get('supersedes') != old:
            raise ValueError('Name the current contract ID in supersedes before replacing an assignment.')
    elif contract.get('supersedes') and not old:
        raise ValueError('The contract to supersede is not available; reconcile its handoff first.')


def delivery_hold(app, fleet_id, contract):
    """Recheck identity, source, supersession and optional base before effects."""
    try:
        fleet = app.store.fleet(fleet_id)
        validate(contract, app, fleet, check_base=True, revalidated=True)
        with app.progress.lock, closing(app.progress.connect()) as db:
            active = current(db, fleet_id, contract['agentId'], contract['taskUrl'])
        latest = app.triggers.data.get('currentContracts', {}).get(current_key(fleet_id, contract))
        if not active or active.get('status') == 'dismissed' or latest != contract['id'] or digest(active['assignmentContract']) != digest(contract):
            return 'Assignment contract is superseded or not durably observed; inspect its current handoff.'
    except ValueError as exc:
        return str(exc)
    return ''


def report_fields(payload, fleet):
    from studio_progress import plain, configuration
    config, _ = configuration(fleet)
    fields = {}
    if 'contractId' in payload:
        fields['contractId'] = plain(payload['contractId'], 'contractId', 100, True)
    if 'completionEvidence' in payload:
        evidence = payload['completionEvidence']
        if not isinstance(evidence, dict) or len(evidence) > 20 or any(not isinstance(k, str) for k in evidence):
            raise ValueError('Completion evidence must map at most 20 requirement IDs to project evidence URLs.')
        fields['completionEvidence'] = {k: project_link(v, config) for k, v in evidence.items()}
    if 'revision' in payload and payload.get('kind') != 'test-result':
        if not isinstance(payload['revision'], str) or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', payload['revision']):
            raise ValueError('Report the full lowercase candidate revision SHA.')
        fields['revision'] = payload['revision']
    return fields


def validate_report(db, app, fleet, agent, data):
    if data['kind'] == 'handoff-received':
        # Acknowledging an earlier obligation must remain possible after
        # supersession; the existing receipt path verifies its original binding.
        row = db.execute('SELECT data FROM handoffs WHERE fleet=? AND id=?', (fleet['id'], data['handoffId'])).fetchone()
        handoff = json.loads(row['data']) if row else {}
        contract = handoff.get('assignmentContract')
        if (contract and data.get('contractId') != contract['id']) or (not contract and data.get('contractId')):
            raise ValueError('The receipt contract ID must match the original handoff.')
        if data.get('completionEvidence'):
            raise ValueError('Handoff receipt is separate from completion evidence.')
        return
    registered = registered_task(app, fleet['id'], agent['id'], data['taskUrl'])
    if registered and registered != data['taskUrl']:
        raise ValueError('Use the exact canonical task URL in the current assignment contract.')
    active = current(db, fleet['id'], agent['id'], data['taskUrl'])
    if not active:
        if registered:
            raise ValueError('The current contract is not durably observed; reconcile its handoff before reporting.')
        if data.get('contractId') or data.get('completionEvidence'):
            raise ValueError('No current assignment contract matches this report.')
        return
    contract = validate(active['assignmentContract'], app, fleet, revalidated=True)
    latest = app.triggers.data.get('currentContracts', {}).get(current_key(fleet['id'], contract))
    if latest != contract['id']:
        raise ValueError('The current contract handoff is not durably observed; retry reporting after reconciliation.')
    if data.get('contractId') != contract['id']:
        raise ValueError('Report the current assignment contract ID; earlier or missing contract identity cannot advance this work.')
    if active.get('status') == 'dismissed' and data['kind'] != 'blocked':
        raise ValueError('This assignment contract was withdrawn; report the blocker or obtain a new scoped contract.')
    if data['kind'] == 'handoff-received' and data['handoffId'] != active['id']:
        raise ValueError('The receipt must identify the current contract handoff.')
    old = db.execute('SELECT data FROM agent_tasks WHERE fleet=? AND agent=? AND url=?', (fleet['id'], agent['id'], data['taskUrl'])).fetchone()
    previous = json.loads(old['data']) if old else {}
    if previous.get('contractId') != contract['id']:
        previous = {}
    supplied = data.get('completionEvidence', previous.get('completionEvidence', {}))
    requirements = {r['id'] for r in contract['requirements']}
    if set(supplied) - requirements:
        raise ValueError('Completion evidence names an unknown contract requirement.')
    phase = data.get('phase') or (previous.get('phase') if data['kind'] in ('progress-updated', 'unblocked') else None)
    submission = data['kind'] == 'work-finished' or phase == 'submitted'
    if contract['candidateRevision'] and (submission or data['kind'] == 'test-result' or data.get('revision')):
        revision = data.get('revision', previous.get('revision') if data['kind'] in ('progress-updated', 'unblocked') else None)
        if revision != contract['candidateRevision']:
            raise ValueError('The reported revision must match this contract candidate; review a new envelope for changed work.')
    # New explicit submissions carry their own complete evidence map. Other
    # milestones may refer to retained facts for this exact immutable contract.
    if data['kind'] == 'work-finished' or data.get('phase') == 'submitted':
        supplied = data.get('completionEvidence', {})
    if submission and requirements - set(supplied):
        raise ValueError('Missing contract completion evidence: ' + ', '.join(sorted(requirements - set(supplied))))


def describe(contract):
    return ('Assignment contract (verify scope and evidence; validity is not acceptance):\n'
            + json.dumps(contract, indent=2, ensure_ascii=True)
            + '\nReport contractId ' + contract['id'] + ' with milestones. Preserve explicit stops and project permissions.')


def projection(contract, report):
    evidence = report.get('completionEvidence', {}) if report and report.get('contractId') == contract['id'] else {}
    return {**deepcopy(contract), 'requirements': [{**r, 'evidenceUrl': evidence.get(r['id'], '')} for r in contract['requirements']],
            'missingRequirements': [r['id'] for r in contract['requirements'] if not evidence.get(r['id'])]}
