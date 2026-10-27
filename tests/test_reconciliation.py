"""Reconciliation preserves delivery identity and never sends or accepts work."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import test_design as fixtures
from studio_triggers import Triggers


class ReconciliationTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    rule = fixtures.DesignTests.rule
    save_rules = fixtures.DesignTests.save_rules

    def legacy_handoff(self):
        app,f=self.app();f=self.save_rules(app,f,[self.rule(f,'event')])
        payload={'fleetId':f['id'],'agentId':f['agents'][0]['id'],'eventName':'packet-ready','eventId':'original-event',
                 'ticket':'https://gitlab.example/team/project/-/issues/1#note_1','artifact':'commit 123', 'context':'Original decision'}
        with patch.object(app.controls,'send') as send:
            app.triggers.report(payload);send.assert_not_called()
        action=app.triggers.data['actions'][0]
        action.pop('sourceAgentId');action['status']='needs-attention'
        app.triggers.data['events'][0].pop('agentId')
        app.triggers.persist()
        request={'actionId':action['id'],'mode':'record-receipt','note':'Recipient acknowledged the original decision in the linked note.',
                 'evidenceUrl':'https://gitlab.example/team/project/-/issues/1#note_2'}
        return app,f,action,request

    def test_external_receipt_recovers_exact_source_without_resend_and_survives_restart(self):
        app,f,a,request=self.legacy_handoff();event_key=a['eventKey'];before=deepcopy(app.triggers.data['seenEvents'])
        with patch.object(app.controls,'send') as send:
            p=app.reconciliation.preview(request)
            self.assertEqual(p['source']['id'],f['agents'][0]['id'])
            result=app.reconciliation.apply(p['id'])
            self.assertEqual(result['status'],'reconciled')
            app.triggers=Triggers(app);app.triggers.tick(app.store.snapshot(),{})
            send.assert_not_called()
        self.assertEqual(app.triggers.data['actions'][0]['eventKey'],event_key)
        self.assertEqual(app.triggers.data['seenEvents'],before)
        self.assertEqual(app.progress.pending_handoffs(f['id']),[])
        self.assertEqual(result['reconciliation']['source'],'operator-reconciliation')

    def test_repair_stays_held_for_review_and_refuses_dispatched_actions(self):
        app,f,a,request=self.legacy_handoff();request['mode']='repair-source'
        a['controlId']='old-control'
        with self.assertRaisesRegex(ValueError,'proven unsent'):app.reconciliation.preview(request)
        a.pop('controlId')
        p=app.reconciliation.preview(request)
        with patch.object(app.controls,'send') as send:
            app.reconciliation.apply(p['id']);app.triggers.tick(app.store.snapshot(),{})
            send.assert_not_called()
        self.assertEqual(a['status'],'needs-attention')
        self.assertEqual(a['sourceAgentId'],f['agents'][0]['id'])

    def test_preview_rejects_wrong_ticket_missing_evidence_and_changed_delivery(self):
        app,f,a,request=self.legacy_handoff()
        for change in ({'evidenceUrl':''},{'note':''},{'evidenceUrl':'https://gitlab.example/team/project/-/issues/99'}):
            with self.assertRaises(ValueError):app.reconciliation.preview({**request,**change})
        p=app.reconciliation.preview(request);a['controlId']='new-dispatch'
        with self.assertRaisesRegex(ValueError,'changed'):app.reconciliation.apply(p['id'])

    def test_recipient_message_must_be_public_and_from_original_session(self):
        app,f,a,request=self.legacy_handoff();request['receiptMessageId']='message-1'
        with patch('studio_activity.activity',return_value={'messages':[{'id':'message-1','kind':'input','text':'Receive it','timestamp':'today'}]}):
            with self.assertRaisesRegex(ValueError,'public recipient'):app.reconciliation.preview(request)
        with patch('studio_activity.activity',return_value={'messages':[{'id':'message-1','kind':'summary','text':'I received the decision','timestamp':'2026-10-13T16:00:00Z'}]}) as activity:
            p=app.reconciliation.preview(request)
            self.assertEqual(activity.call_args.args[1],a['targetSessionId'])
            self.assertEqual(p['recipientMessage']['messageId'],'message-1')

    def test_reconciled_queued_receipt_is_not_reopened_by_control_polling(self):
        app,f,a,request=self.legacy_handoff();a.update(controlId='queued-control',status='queued')
        app.controls.receipts['queued-control']={'id':'queued-control','status':'completed','targets':[]}
        app.triggers.persist()
        p=app.reconciliation.preview(request);app.reconciliation.apply(p['id'])
        app.triggers.tick(app.store.snapshot(),{})
        self.assertEqual(a['status'],'reconciled')

    def deployed_handoff(self):
        app, f = self.app(); f = self.save_rules(app, f, [self.rule(f, 'event')])
        p = app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(p['id'])
        app.triggers.report({'fleetId': f['id'], 'agentId': f['agents'][0]['id'], 'eventName': 'packet-ready',
                             'eventId': 'original', 'ticket': 'https://gitlab.example/p/-/issues/1',
                             'artifact': 'Same reviewed packet', 'context': 'Existing bounded handoff'})
        a = app.triggers.data['actions'][0]
        app.progress.observe_handoffs([a])
        p = app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'reporting'})
        app.deployments.apply(p['id'])
        app.triggers.tick(app.store.snapshot(), {})
        request = {'actionId': a['id'], 'mode': 'refresh-release', 'note': 'Original handoff remains current under the new reporting policy.',
                   'evidenceUrl': a['ticket'] + '#note_2'}
        return app, f, a, request

    def test_policy_release_revalidation_preserves_event_and_availability_guards(self):
        app, f, a, request = self.deployed_handoff()
        before = deepcopy(a); seen = deepcopy(app.triggers.data['seenEvents'])
        with patch.object(app.controls, 'send') as send:
            p = app.reconciliation.preview(request)
            app.reconciliation.apply(p['id'])
            app.triggers.tick(app.store.snapshot(), fixtures.DesignTests.monitor(self, f))
            send.assert_called_once()  # Revalidation makes the unsent handoff deliverable again.
        for key in ('id', 'eventKey', 'eventId', 'message', 'targetSessionId', 'targetDirectory', 'sourceAgentId'):
            self.assertEqual(a[key], before[key])
        self.assertEqual(app.triggers.data['seenEvents'], seen)
        self.assertEqual(a['releaseRevalidations'][0]['previousDeploymentId'], before['deploymentId'])
        self.assertNotEqual(a['deploymentId'], before['deploymentId'])
        self.assertFalse(app.progress.pending_handoffs(f['id'])[0].get('receivedAt'))

    def test_release_revalidation_rejects_any_dispatch_and_changed_custody(self):
        app, f, a, request = self.deployed_handoff()
        a['controlId'] = 'unconfirmed'
        with self.assertRaisesRegex(ValueError, 'proven unsent'):
            app.reconciliation.preview(request)
        a.pop('controlId')
        p = app.reconciliation.preview(request)
        state = app.store.snapshot();state['fleets'][0]['agents'][0]['notes'] = 'Changed custody';app.store.save(state)
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            app.reconciliation.apply(p['id'])

    def test_release_revalidation_refuses_changed_routing_contract(self):
        app, f, a, request = self.deployed_handoff()
        state = app.store.snapshot();state['fleets'][0]['triggers'][0]['message'] = 'Different work';app.store.save(state)
        p = app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(p['id'])
        with self.assertRaisesRegex(ValueError, 'contracts changed'):
            app.reconciliation.preview(request)
