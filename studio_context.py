"""Bounded current context and reviewed retirement of resolved display records."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import time
import uuid

from studio_bindings import runtime_binding, session_key, same_binding
from studio_contracts import current_key
from studio_progress import task_key, configuration
from studio_recovery import checkpoint


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def semantic(value):
    """Polling timestamps alone do not change context or invalidate a preview."""
    return {k: v for k, v in value.items() if k not in ('observedAt', 'awaitingReceipt', 'overdue', 'checkpoint')}


class CurrentContext:
    def __init__(self, app):
        self.app = app
        self.previews = {}

    def records(self, fid, aid):
        app = self.app
        fleet = app.store.fleet(fid)
        agent = next((a for a in fleet['agents'] if a['id'] == aid), None)
        rid = app.deployments.data['active'].get(fid)
        if not agent or not rid:
            raise ValueError('Choose an agent with a deployed assignment.')
        release = app.deployments.release(rid)
        deployed = next((a for a in release['fleet']['agents'] if a['id'] == aid), None)
        with app.lock:
            monitor = deepcopy(app.monitor)
        observed = monitor.get('sessions', {}).get(session_key(agent), {})
        stale = not observed.get('observedAt') or not 0 <= time.time() - observed['observedAt'] <= 20
        header = {'fleetId': fid, 'agentId': aid, 'releaseId': rid, 'releaseDigest': release['digest'],
                  'bindingCurrent': bool(deployed and same_binding(agent, deployed, ('sessionId', 'directory', 'roleIds'))),
                  'binding': {k: agent.get(k) for k in ('sessionId', 'directory', 'roleIds')}, 'runtime': runtime_binding(agent),
                  'enabled': agent['enabled'], 'automationPaused': bool(app.deployments.data['paused'].get(fid)),
                  'goalObservation': 'unknown/stale' if stale or not observed.get('goalKnown') else observed.get('goalStatus') or 'no active goal observed',
                  'goalIdentity': {k: (observed.get('goal') or {}).get(k) for k in ('id', 'createdAt')},
                  'goalObjectiveDigest': fingerprint((observed.get('goal') or {}).get('objective')),
                  'policyDocument': str(app.root / 'workflow-releases' / rid / 'agents' / (aid + '.md')),
                  'instructions': 'Read the complete deployed instructions at the policy document above. This context supplements them. '
                                  'Preserve explicit stops, scope, custody and source permissions. Unknown or stale observations never authorize resuming work.'}
        with app.progress.lock, closing(app.progress.connect()) as db:
            handoffs = [json.loads(r['data']) for r in db.execute('SELECT data FROM handoffs WHERE fleet=?', (fid,))]
            reports = {r['url']: json.loads(r['data']) for r in db.execute('SELECT * FROM agent_tasks WHERE fleet=? AND agent=?', (fid, aid))}
            tasks = {r['url']: json.loads(r['data']) for r in db.execute('SELECT * FROM tasks WHERE fleet=?', (fid,))}
        controls = {r['id']: r for r in app.controls.snapshot(full=True)}
        with app.backlog.lock:
            tracker = deepcopy(app.backlog.cache.get(fid) or {})
        _, source = configuration(fleet)
        records = []
        for h in sorted((h for h in handoffs if h.get('targetAgentId') == aid), key=lambda h: (h['createdAt'], h['id'])):
            report = reports.get(task_key(h.get('ticket') or ''))
            control = controls.get(h.get('controlId'))
            c = h.get('assignmentContract')
            superseded = bool(c and app.triggers.data['currentContracts'].get(current_key(fid, c)) != c['id'])
            task = tasks.get(task_key(h.get('ticket') or ''))
            accepted = False
            if (task and tracker.get('observedAt') is not None and task.get('observedAt') == tracker['observedAt']
                    and not tracker.get('error') and not tracker.get('partial') and not tracker.get('loading')
                    and 0 <= time.time() - tracker['observedAt'] <= 180 and task.get('releaseId') == rid
                    and task.get('sourceKey') == source and task['state'] == 'closed' and h.get('submittedAt')
                    and task['observedAt'] >= h['submittedAt'] and h.get('deploymentId') == rid
                    and report and report.get('obligationId') == h['id'] and report.get('releaseId') == rid
                    and report.get('phase') == 'submitted' and not report.get('blockedSince')
                    and task['observedAt'] >= report['at'] and same_binding(report, agent)
                    and h.get('targetSessionId') == agent['sessionId'] and h.get('targetDirectory') == agent['directory']
                    and h.get('targetRuntime') == runtime_binding(agent) and header['bindingCurrent']
                    and (not c or report.get('contractId') == c['id'])):
                from workflow_spec import project_ticket
                spec = app.runtime_fleet(fleet).get('workflow', {})
                accepted = bool(spec.get('states') and project_ticket(task, spec).get('acceptsDelivery'))
            record = {'handoff': semantic(h), 'report': semantic(report) if report else None,
                            'control': semantic(control) if control else None, 'superseded': superseded,
                            'accepted': accepted, 'checkpoint': checkpoint(h, report, control)}
            probe = {**record, 'handoff': {k: v for k, v in record['handoff'].items() if k != 'retiredAt'}}
            record['retirementCurrent'] = bool(h.get('retiredAt') and not self.retirement_reason(probe))
            records.append(record)
        return header, records, reports

    def retired_ids(self, fid):
        app = self.app
        with app.triggers.lock, app.store.lock, app.controls.lock:
            return {r['handoff']['id'] for a in app.store.fleet(fid)['agents']
                    for r in self.records(fid, a['id'])[1] if r['retirementCurrent']}

    @staticmethod
    def retirement_reason(record):
        h, cp = record['handoff'], record['checkpoint']
        if h.get('retiredAt'):
            return 'Already retired from the default display.'
        report = record['report'] or {}
        if (report.get('obligationId') == h['id'] and h.get('submittedAt') and report.get('at', 0) >= h['submittedAt']
                and (report.get('blockedSince') or report.get('phase') != 'submitted')):
            return 'A later report conflicts with this submission; reconcile before retirement.'
        if cp['delivery'] == 'uncertain' or h.get('status') in ('sending', 'needs-attention'):
            return 'Uncertain delivery or failure evidence still needs reconciliation.'
        if record['control'] and record['control'].get('status') == 'sending':
            return 'Wait for the active dispatch to settle.'
        if h.get('assignmentContract') and not record['superseded'] and not record['accepted']:
            return 'The current contract and its withdrawal/stop constraints must remain visible.'
        if h.get('status') == 'dismissed' and not record['control']:
            return ''
        if (h.get('receivedAt') and h.get('receiptEvidence') and h.get('submittedAt') and h.get('submissionEvidence')
                and (record['superseded'] or record['accepted'])):
            return ''
        return 'Keep this obligation until withdrawal or completed/superseded work has receipt and submission evidence.'

    def page(self, payload):
        fid, aid = payload.get('fleetId'), payload.get('agentId')
        limit = payload.get('maxChars', 12000)
        if type(limit) is not int or not 2000 <= limit <= 16000:
            raise ValueError('Context maxChars must be between 2000 and 16000.')
        app = self.app
        with app.triggers.lock, app.store.lock, app.controls.lock:
            header, records, reports = self.records(fid, aid)
            items, covered = [], set()
            for record in records:
                h = record['handoff']
                if record['retirementCurrent']:
                    continue
                url = task_key(h.get('ticket') or '')
                c = h.get('assignmentContract')
                item = {'handoffId': h['id'], 'taskUrl': url, 'checkpoint': record['checkpoint'],
                        'recordPath': '/api/context/handoff', 'superseded': record['superseded'],
                        'note': 'Earlier obligation: reconcile evidence only; do not re-execute its old instructions.' if record['superseded'] else
                                'Dependency notification; original assignment remains authoritative.' if h.get('continuationOf') else 'Current obligation; preserve its stops and scope.'}
                if c and not record['superseded'] and not h.get('continuationOf'):
                    item['assignmentContract'] = c
                report = record['report']
                if report and report.get('obligationId') == h['id']:
                    item['report'] = report
                    item['reportBindingCurrent'] = report.get('releaseId') == header['releaseId'] and same_binding(report, {**header['binding'], **header['runtime']})
                    covered.add(url)
                items.append(item)
            for url, report in reports.items():
                if url not in covered and not any(r['retirementCurrent'] and report.get('obligationId') == r['handoff']['id'] for r in records):
                    items.append({'taskUrl': url, 'report': semantic(report), 'note': 'Reported work without a matching visible obligation; reconcile before continuing.'})
            token = fingerprint([header, items])
        start = 0
        cursor = payload.get('cursor')
        if cursor:
            try:
                prior, offset = cursor.split(':')
                start = int(offset)
            except (ValueError, AttributeError):
                raise ValueError('Invalid context cursor.') from None
            if prior != token or not 0 <= start < len(items):
                raise ValueError('Context changed or cursor expired. Start a fresh context page.')
        prefix = json.dumps(header, indent=2, ensure_ascii=True) + '\n'
        budget = limit - 250  # The actual CLI text includes pagination metadata.
        if len(prefix) > budget:
            return {'fingerprint': token, 'text': 'Context header exceeds the requested bound. Read the complete deployed instructions: ' + header['policyDocument'],
                    'overflow': True, 'requiredChars': len(prefix), 'totalItems': len(items), 'nextCursor': None, 'items': []}
        if payload.get('since') == token and not cursor:
            return {'fingerprint': token, 'text': prefix + 'Unchanged context. Fingerprint: ' + token + '\n', 'unchanged': True, 'totalItems': len(items), 'nextCursor': None, 'items': []}
        selected, oversized, text = [], [], prefix
        index = start
        while index < len(items):
            item = items[index]
            rendered = json.dumps(item, indent=2, ensure_ascii=True) + '\n'
            if len(prefix) + len(rendered) > budget:
                short = {k: item[k] for k in ('handoffId', 'taskUrl') if k in item}
                short.update(overflow=True, requiredChars=len(prefix) + len(rendered),
                             recordPath='/api/context/handoff' if item.get('handoffId') else '/api/progress/history',
                             notice='Full obligation exceeds the page bound. Retrieve its evidence before acting; it has not been discarded.')
                rendered = json.dumps(short, ensure_ascii=True) + '\n'
                item = short
            if len(text) + len(rendered) > budget:
                break
            text += rendered
            selected.append(item)
            if item.get('overflow'):
                oversized.append(item)
            index += 1
        if index == start and start < len(items):
            raise ValueError('Context header leaves no room for an obligation reference. Increase maxChars.')
        next_cursor = token + ':' + str(index) if index < len(items) else None
        text += 'Fingerprint: ' + token + '\nRemaining obligations: ' + str(len(items) - index) + '\nNext cursor: ' + (next_cursor or 'none') + '\n'
        return {'fingerprint': token, 'text': text, 'items': selected, 'overflow': bool(oversized),
                'totalItems': len(items), 'remainingItems': len(items) - index,
                'retiredCount': sum(r['retirementCurrent'] for r in records),
                'nextCursor': next_cursor}

    def handoff(self, payload):
        with self.app.triggers.lock, self.app.store.lock, self.app.controls.lock:
            _, records, _ = self.records(payload.get('fleetId'), payload.get('agentId'))
            record = next((r for r in records if r['handoff']['id'] == payload.get('handoffId')), None)
            if not record:
                raise ValueError('No handoff with this identity belongs to this agent.')
            return record

    def preview_retirement(self, payload):
        with self.app.triggers.lock, self.app.store.lock, self.app.controls.lock:
            header, records, _ = self.records(payload.get('fleetId'), payload.get('agentId'))
            requested = payload.get('handoffIds')
            if requested is not None and (not isinstance(requested, list) or len(requested) > 100 or any(not isinstance(x, str) for x in requested)):
                raise ValueError('Choose at most 100 handoff IDs.')
            selected = [r for r in records if not self.retirement_reason(r) and (requested is None or r['handoff']['id'] in requested)][:100]
            if requested is not None and set(requested) != {r['handoff']['id'] for r in selected}:
                raise ValueError('One or more selected handoffs still require current work, receipt, completion or failure evidence.')
            preview = {'id': uuid.uuid4().hex, 'createdAt': time.time(), 'fleetId': header['fleetId'], 'agentId': header['agentId'],
                       'records': selected, 'fingerprint': fingerprint([header, selected]),
                       'effect': 'Hide these resolved notices from the default view. Canonical evidence, tombstones and deduplication identities remain; nothing is sent or deleted.'}
            self.previews = {k: v for k, v in self.previews.items() if time.time() - v['createdAt'] < 600}
            self.previews[preview['id']] = preview
            return deepcopy(preview)

    def retire(self, preview_id):
        app = self.app
        with app.triggers.lock, app.store.lock, app.controls.lock, app.backlog.lock, app.progress.lock:
            preview = self.previews.get(preview_id)
            if not preview or time.time() - preview['createdAt'] > 600:
                raise ValueError('Retirement preview expired. Review again.')
            header, records, _ = self.records(preview['fleetId'], preview['agentId'])
            ids = {r['handoff']['id'] for r in preview['records']}
            selected = [r for r in records if r['handoff']['id'] in ids]
            if fingerprint([header, selected]) != preview['fingerprint'] or any(self.retirement_reason(r) for r in selected):
                raise ValueError('Relevant assignment, stop or evidence changed. Preview retirement again.')
            with closing(app.progress.connect()) as db, db:
                for record in selected:
                    row = db.execute('SELECT data FROM handoffs WHERE fleet=? AND id=?', (preview['fleetId'], record['handoff']['id'])).fetchone()
                    h = json.loads(row['data'])
                    h.update(retiredAt=time.time(), retirement={'previewId': preview_id, 'evidenceUrl': h.get('submissionEvidence') or h.get('receiptEvidence'),
                                                             'basis': 'withdrawn' if h['status'] == 'dismissed' else 'superseded' if record['superseded'] else 'tracker-accepted'})
                    db.execute('UPDATE handoffs SET data=? WHERE fleet=? AND id=?', (json.dumps(h), preview['fleetId'], h['id']))
            del self.previews[preview_id]
            return {'retiredIds': sorted(ids), 'effect': preview['effect']}
