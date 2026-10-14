"""Reviewed handoff repair and external receipts, without dispatch or acceptance."""
from studio_bindings import runtime_binding, same_binding
from copy import deepcopy
import hashlib
import json
import time
import uuid
from studio_progress import link, plain, task_key


def fingerprint(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()


class Reconciliation:
    def __init__(self, app):
        self.app = app
        self.previews = {}

    def source(self, action):
        """Recover only from an exact retained event and its namespaced identity."""
        event = next((e for e in self.app.triggers.data['events'] if e['key'] == action.get('eventKey')), None)
        if not event or event.get('eventId') != action.get('eventId') or event.get('ticket') != action.get('ticket'):
            return None
        fields = event['key'].split(':', 3)
        if len(fields) != 4 or fields[0] != action['fleetId'] or fields[2:] != [event['eventName'], event['eventId']]:
            return None
        if event.get('agentId') and event['agentId'] != fields[1]:
            return None
        fleet = self.app.store.fleet(action['fleetId'])
        agent = next((a for a in fleet['agents'] if a['id'] == fields[1]), None)
        return {'id': agent['id'], 'name': agent['name']} if agent else None

    def release_refresh(self, action):
        """Prove this is an unsent event with identical routing and custody."""
        from studio_triggers import rule_signature
        from studio_workflow import contract_problem
        fid = action['fleetId']
        rid = self.app.deployments.data['active'].get(fid)
        if not rid or not action.get('deploymentId') or rid == action['deploymentId']:
            raise ValueError('This handoff does not need release revalidation.')
        if action.get('controlId') or action['status'] not in ('pending', 'held', 'needs-attention') or not action.get('sourceAgentId'):
            raise ValueError('Only a proven unsent handoff with its original source can be revalidated.')
        source = self.source(action)
        if not source or source['id'] != action['sourceAgentId']:
            raise ValueError('The original event source cannot be verified.')
        if action['id'] not in {h['id'] for h in self.app.progress.pending_handoffs(fid)}:
            raise ValueError('This handoff already has a receipt.')
        old = self.app.deployments.release(action['deploymentId'])
        new = self.app.deployments.release(rid)
        if old['fleetId'] != fid or new['fleetId'] != fid:
            raise ValueError('Handoff release does not belong to this fleet.')
        for section in ('roles', 'handoffs', 'states', 'transitions', 'triggers', 'sources', 'gaps'):
            if old['spec'][section] != new['spec'][section]:
                raise ValueError('Workflow contracts changed; this event cannot be carried forward by a policy-only revalidation.')
        live = self.app.store.fleet(fid)
        for aid in (action['sourceAgentId'], action['targetAgentId']):
            bindings = [next((a for a in f['agents'] if a['id'] == aid), None) for f in (old['fleet'], new['fleet'], live)]
            keys = ('id', 'name', 'sessionId', 'directory', 'roleIds', 'notes', 'enabled')
            if any(not a or not a['enabled'] for a in bindings) or any(not same_binding(a, bindings[0], keys) for a in bindings[1:]):
                raise ValueError('Source or recipient binding changed; preserve the original handoff.')
        target = next(a for a in live['agents'] if a['id'] == action['targetAgentId'])
        if target['sessionId'] != action.get('targetSessionId') or target['directory'] != action.get('targetDirectory') or runtime_binding(target) != action.get('targetRuntime', {}):
            raise ValueError('Original recipient custody differs from the deployment.')
        previous = next((r for r in old['fleet']['triggers'] if r['id'] == action['triggerId']), None)
        rule = next((r for r in self.app.runtime_fleet(live)['triggers'] if r['id'] == action['triggerId']), None)
        if not previous or not rule or not rule['enabled'] or rule_signature({**previous, 'deploymentId': old['id']}) != action.get('ruleSignature') or contract_problem(self.app.runtime_fleet(live), rule):
            raise ValueError('Original or current trigger cannot be verified. Preserve the held event.')
        if {k:v for k,v in rule.items() if k != 'deploymentId'} != previous:
            raise ValueError('The original trigger changed.')
        return {'previousDeploymentId': old['id'], 'deploymentId': rid, 'ruleSignature': rule_signature(rule)}

    def preview(self, payload):
        with self.app.triggers.lock:
            action = next((a for a in self.app.triggers.data['actions'] if a['id'] == payload.get('actionId')), None)
            if not action or not action.get('eventId') or action['status'] in ('dismissed', 'reconciled'):
                raise ValueError('Choose an unresolved handoff retained by the dispatcher.')
            mode = payload.get('mode')
            if mode not in ('repair-source', 'record-receipt', 'refresh-release'):
                raise ValueError('Choose source repair, release revalidation or an external receipt.')
            source = self.source(action)
            note = plain(payload.get('note'), 'Reconciliation note', 1800, True)
            evidence = link(payload.get('evidenceUrl'), 'Receipt evidence')
            if task_key(evidence) != task_key(action.get('ticket', '')):
                raise ValueError('Use evidence on the original handoff ticket.')
            message_id = plain(payload.get('receiptMessageId', ''), 'Recipient message ID', 200)
            receipt = None
            refresh = None
            if mode == 'refresh-release':
                refresh = self.release_refresh(action)
            elif mode == 'repair-source':
                if not source or action.get('sourceAgentId'):
                    raise ValueError('No unambiguous missing source identity can be repaired.')
                if action.get('controlId') or action['status'] not in ('pending', 'held', 'needs-attention'):
                    raise ValueError('Only a proven unsent handoff can be repaired.')
            elif action['id'] not in {h['id'] for h in self.app.progress.pending_handoffs(action['fleetId'])}:
                raise ValueError('This handoff already has a receipt.')
            elif message_id:
                from studio_activity import activity
                data = activity(self.app.codex_root, action['targetSessionId'], self.app.activity_fixture)
                message = next((m for m in data.get('messages', []) if m['id'] == message_id and m['kind'] in ('summary', 'progress')), None)
                if not message:
                    raise ValueError('The selected public recipient message is unavailable. Review its evidence again.')
                receipt = {'sessionId': action['targetSessionId'], 'messageId': message_id,
                           'timestamp': message['timestamp'], 'messageDigest': hashlib.sha256(message['text'].encode()).hexdigest()}
            preview = {'id': uuid.uuid4().hex, 'createdAt': time.time(), 'actionId': action['id'],
                       'fleetId': action['fleetId'], 'actionDigest': fingerprint(action),
                       'releaseId': self.app.deployments.data['active'].get(action['fleetId']),
                       'mode': mode, 'source': source, 'targetName': action['targetName'], 'ticket': action['ticket'],
                       'evidenceUrl': evidence, 'note': note, 'recipientMessage': receipt,
                       'previousStatus': action['status'],
                       'releaseRefresh': refresh,
                       'effect': 'Return this proven-unsent event to normal delivery under the current release, preserving its identity, message, recipient and adoption/availability guards. No receipt or acceptance is recorded.' if refresh else 'Record an operator-reconciled receipt and suppress further delivery of this event; no acceptance or completion is recorded.' if mode == 'record-receipt' else 'Restore the original source identity and leave the unsent handoff awaiting review; no delivery is triggered.'}
            self.previews = {k:v for k,v in self.previews.items() if time.time() - v['createdAt'] < 600}
            self.previews[preview['id']] = preview
            return deepcopy(preview)

    def apply(self, preview_id):
        with self.app.triggers.lock, self.app.store.lock, self.app.controls.lock:
            preview = self.previews.get(preview_id)
            if not preview or time.time() - preview['createdAt'] > 600:
                raise ValueError('Reconciliation preview expired. Review it again.')
            action = next((a for a in self.app.triggers.data['actions'] if a['id'] == preview['actionId']), None)
            if not action or fingerprint(action) != preview['actionDigest'] or self.app.deployments.data['active'].get(action['fleetId']) != preview['releaseId']:
                raise ValueError('Handoff or deployment changed. Review the reconciliation again.')
            if action['status'] == 'sending' or any(r['id'] == action.get('controlId') and r['status'] == 'sending' for r in self.app.controls.receipts.values()):
                raise ValueError('Wait for the active dispatch to settle before reconciling.')
            refresh = self.release_refresh(action) if preview['mode'] == 'refresh-release' else None
            if refresh != preview.get('releaseRefresh'):
                raise ValueError('Release revalidation changed. Review it again.')
            before = deepcopy(self.app.triggers.data)
            record = {k:deepcopy(preview[k]) for k in ('mode', 'evidenceUrl', 'note', 'recipientMessage', 'previousStatus')}
            record.update(at=time.time(), source='operator-reconciliation', previewId=preview_id)
            if preview['source'] and not action.get('sourceAgentId'):
                action['sourceAgentId'] = preview['source']['id']
                record['sourceRecoveredFrom'] = action['eventKey']
            if refresh:
                action.setdefault('releaseRevalidations', []).append({**record, **refresh})
                action.update(deploymentId=refresh['deploymentId'], ruleSignature=refresh['ruleSignature'], status='pending',
                              detail='Unsent event revalidated against unchanged contracts and bindings; normal adoption and availability checks remain.')
            elif preview['mode'] == 'record-receipt':
                action['reconciliation'] = record
                self.app.controls.previews.pop(action.get('controlId'), None)
                action.update(status='reconciled', detail='External receipt reconciled against evidence; no instruction resent.')
            else:
                action['reconciliation'] = record
                action.update(status='needs-attention', detail='Original source identity repaired. Review this unsent handoff before delivery.')
            try:
                self.app.triggers.persist()
            except Exception:
                self.app.triggers.data = before
                raise
            del self.previews[preview_id]
            return deepcopy(action)
