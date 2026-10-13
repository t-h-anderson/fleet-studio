"""Reviewed handoff repair and external receipts, without dispatch or acceptance."""
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

    def preview(self, payload):
        with self.app.triggers.lock:
            action = next((a for a in self.app.triggers.data['actions'] if a['id'] == payload.get('actionId')), None)
            if not action or not action.get('eventId') or action['status'] in ('dismissed', 'reconciled'):
                raise ValueError('Choose an unresolved handoff retained by the dispatcher.')
            mode = payload.get('mode')
            if mode not in ('repair-source', 'record-receipt'):
                raise ValueError('Choose source repair or an external receipt.')
            source = self.source(action)
            note = plain(payload.get('note'), 'Reconciliation note', 1800, True)
            evidence = link(payload.get('evidenceUrl'), 'Receipt evidence')
            if task_key(evidence) != task_key(action.get('ticket', '')):
                raise ValueError('Use evidence on the original handoff ticket.')
            message_id = plain(payload.get('receiptMessageId', ''), 'Recipient message ID', 200)
            receipt = None
            if mode == 'repair-source':
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
                       'effect': 'Record an operator-reconciled receipt and suppress further delivery of this event; no acceptance or completion is recorded.' if mode == 'record-receipt' else 'Restore the original source identity and leave the unsent handoff awaiting review; no delivery is triggered.'}
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
            before = deepcopy(self.app.triggers.data)
            record = {k:deepcopy(preview[k]) for k in ('mode', 'evidenceUrl', 'note', 'recipientMessage', 'previousStatus')}
            record.update(at=time.time(), source='operator-reconciliation', previewId=preview_id)
            if preview['source'] and not action.get('sourceAgentId'):
                action['sourceAgentId'] = preview['source']['id']
                record['sourceRecoveredFrom'] = action['eventKey']
            action['reconciliation'] = record
            if preview['mode'] == 'record-receipt':
                self.app.controls.previews.pop(action.get('controlId'), None)
                action.update(status='reconciled', detail='External receipt reconciled against evidence; no instruction resent.')
            else:
                action.update(status='needs-attention', detail='Original source identity repaired. Review this unsent handoff before delivery.')
            try:
                self.app.triggers.persist()
            except Exception:
                self.app.triggers.data = before
                raise
            del self.previews[preview_id]
            return deepcopy(action)
