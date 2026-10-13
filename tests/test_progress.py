"""Progress evidence, persistence and adoption regressions. No real agent sends."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import test_studio as fixtures
from fleet_studio import Application, Server
from studio_progress import Progress, clean_settings
from workflow_spec import apply_spec, export_spec


class ProgressTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown

    def app(self):
        app = Application(self.root, launch_enabled=False)
        state = app.store.snapshot()
        f = state['fleets'][0]
        for a in f['agents']:
            a['roleIds'] = [f['roles'][1]['id']]
        f['backlog'] = {'projectUrl':'https://gitlab.example/team/project', 'scope':'all', 'agentTickets':{}}
        state['fleets'][0] = apply_spec(f, export_spec(f))
        app.store.save(state)
        preview = app.deployments.preview({'fleetId':f['id'],'revision':app.store.snapshot()['revision'],'kind':'deploy'})
        app.deployments.apply(preview['id'])
        self.now = 1800000000.0
        app.progress = Progress(app, lambda:self.now)
        f = app.store.fleet(f['id'])
        self.payload = {'fleetId':f['id'],'agentId':f['agents'][0]['id'],'sessionId':f['agents'][0]['sessionId'],
                        'releaseId':app.deployments.data['active'][f['id']],'eventId':'start-1','kind':'task-started',
                        'taskUrl':f['backlog']['projectUrl']+'/-/issues/1','evidenceUrl':f['backlog']['projectUrl']+'/-/issues/1#note_2',
                        'summary':'Implement a bounded change'}
        return app, f

    def observe(self, app, f, stage='active', state='opened', partial=False):
        labels = {'active':['AI Claimed'],'blocked':['AI Blocked'],'ready':['AI Ready'],'closed':[]}[stage]
        rows = [{'iid':1,'url':self.payload['taskUrl'],'title':'Bounded change','state':state,'labels':labels,
                 'stage':stage,'updatedAt':'2026-10-13T12:00:00Z','assignees':['Shared account']}]
        app.progress.observe_backlog(f, {'items':rows,'observedAt':self.now,'partial':partial})

    def snapshot(self, app, f):
        with patch.object(app.backlog, 'snapshot', return_value={'observedAt':self.now,'stale':False,'partial':False}):
            return app.progress.snapshot(f['id'])

    def test_reports_deduplicate_without_dispatch_or_tracker_mutation(self):
        app,f=self.app()
        with patch.object(app.controls,'send') as send, patch.object(app.triggers,'report') as report:
            self.assertFalse(app.progress.report(self.payload)['duplicate'])
            self.assertTrue(app.progress.report(self.payload)['duplicate'])
            with self.assertRaisesRegex(ValueError,'different report'):
                app.progress.report({**self.payload,'summary':'Changed payload'})
            send.assert_not_called();report.assert_not_called()
        app.progress=Progress(app,lambda:self.now)
        self.assertTrue(app.progress.report(self.payload)['duplicate'])

    def test_reports_reject_stale_release_rebound_session_and_external_task(self):
        app,f=self.app()
        for change in [{'releaseId':'0'*32},{'sessionId':'wrong'}, {'taskUrl':'https://elsewhere.example/project/-/issues/1'}]:
            with self.assertRaises(ValueError):app.progress.report({**self.payload,**change})
        state=app.store.snapshot();state['fleets'][0]['agents'][0]['roleIds']=[];app.store.save(state)
        with self.assertRaisesRegex(ValueError,'binding'):app.progress.report(self.payload)

    def test_blocker_requires_reason_and_next_action_and_survives_progress(self):
        app,f=self.app()
        with self.assertRaisesRegex(ValueError,'reason'):app.progress.report({**self.payload,'kind':'blocked'})
        app.progress.report({**self.payload,'kind':'blocked','reason':'Waiting for input','nextAction':'Owner supplies input'})
        self.now+=600
        app.progress.report({**self.payload,'eventId':'update-2','kind':'progress-updated'})
        d=self.snapshot(app,f);signal=next(a for a in d['attention'] if a['kind']=='blocked')
        self.assertEqual(signal['at'],self.now-600)
        self.assertEqual(signal['nextAction'],'Owner supplies input')
        app.progress.report({**self.payload,'eventId':'unblock-3','kind':'unblocked'})
        self.assertFalse(any(a['kind']=='blocked' for a in self.snapshot(app,f)['attention']))

    def test_reported_finish_does_not_establish_tracker_closure(self):
        app,f=self.app();self.observe(app,f)
        app.progress.report({**self.payload,'kind':'work-finished'})
        d=self.snapshot(app,f)
        self.assertEqual(d['summary']['openTasks'],1)
        self.assertEqual(d['summary']['completed24h'],0)
        self.now+=90;self.observe(app,f,'closed','closed')
        self.assertEqual(self.snapshot(app,f)['summary']['completed24h'],1)

    def test_initial_time_gaps_and_release_changes_do_not_invent_durations(self):
        app,f=self.app();self.observe(app,f)
        d=self.snapshot(app,f);self.assertTrue(d['tasks'][0]['initialStageUnknown'])
        self.now+=90;self.observe(app,f,'blocked')
        t=self.snapshot(app,f)['tasks'][0]
        self.assertFalse(t['initialStageUnknown']);self.assertFalse(t['observationGap'])
        self.now+=600;self.observe(app,f,'ready')
        self.assertTrue(self.snapshot(app,f)['tasks'][0]['observationGap'])
        preview=app.deployments.preview({'fleetId':f['id'],'revision':app.store.snapshot()['revision'],'kind':'deploy'})
        app.deployments.apply(preview['id'])
        self.assertIsNone(self.snapshot(app,f)['tasks'][0]['stageObservedAt'])

    def test_restart_retains_tracker_snapshot_and_quiet_threshold_is_configurable(self):
        app,f=self.app();self.observe(app,f)
        app.progress.report(self.payload)
        self.now+=3599
        self.assertFalse(any(a['kind']=='quiet' for a in self.snapshot(app,f)['attention']))
        self.now+=1
        self.assertTrue(any(a['kind']=='quiet' for a in self.snapshot(app,f)['attention']))
        app.progress=Progress(app,lambda:self.now)
        self.assertEqual(app.progress.cached_backlog(f)['items'][0]['iid'],1)
        state=app.store.snapshot();state['fleets'][0]['progress']={'quietMinutes':120};app.store.save(state)
        self.assertFalse(any(a['kind']=='quiet' for a in self.snapshot(app,f)['attention']))

    def handoff(self, f):
        a=f['agents'][0]
        return {'id':'handoff-1','fleetId':f['id'],'eventId':'packet-1','name':'Review packet','createdAt':self.now,
                'status':'queued','ticket':self.payload['taskUrl'],'targetAgentId':a['id'],'targetName':a['name'],
                'targetSessionId':a['sessionId'],'targetDirectory':a['directory'],'triggerId':'trigger-1'}

    def test_receipt_is_distinct_from_queue_and_checks_original_binding(self):
        app,f=self.app();h=self.handoff(f);app.progress.observe_handoffs([h])
        self.now+=1800
        d=self.snapshot(app,f);self.assertEqual(d['summary']['pendingHandoffs'],1)
        self.assertTrue(d['handoffs'][0]['overdue'])
        payload={**self.payload,'kind':'handoff-received','handoffId':h['id']}
        with self.assertRaises(ValueError):app.progress.report({**payload,'agentId':f['agents'][1]['id'],'sessionId':f['agents'][1]['sessionId']})
        app.progress.report(payload)
        app.progress.observe_handoffs([h])
        d=self.snapshot(app,f);self.assertEqual(d['summary']['pendingHandoffs'],0)
        self.assertEqual(d['handoffs'][0]['status'],'queued')

    def test_retention_preserves_unresolved_handoffs_and_report_identities(self):
        app,f=self.app();app.progress.report(self.payload)
        app.progress.observe_handoffs([self.handoff(f)])
        self.now+=100*86400
        with patch.object(app.backlog,'snapshot'):
            app.progress.tick(app.store.snapshot(),{})
        self.assertEqual(app.progress.history(f['id'])['items'],[])
        self.assertEqual(self.snapshot(app,f)['summary']['pendingHandoffs'],1)
        self.assertTrue(app.progress.report(self.payload)['duplicate'])

    def test_adoption_checks_both_ends_and_new_binding(self):
        app,f=self.app();ids=[a['id'] for a in f['agents']];rid=self.payload['releaseId']
        self.assertIn('not adopted',app.deployments.adoption_hold(f['id'],ids))
        app.deployments.acknowledge({'fleetId':f['id'],'agentId':ids[0],'releaseId':rid})
        self.assertIn('not adopted',app.deployments.adoption_hold(f['id'],ids))
        app.deployments.acknowledge({'fleetId':f['id'],'agentId':ids[1],'releaseId':rid})
        self.assertEqual(app.deployments.adoption_hold(f['id'],ids),'')
        state=app.store.snapshot();state['fleets'][0]['agents'][0]['notes']='New scope';app.store.save(state)
        self.assertIn('not adopted',app.deployments.adoption_hold(f['id'],ids))

    def test_http_reporting_requires_authentication_and_views_are_read_only(self):
        app,f=self.app();server=Server(('127.0.0.1',0),app)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            request=Request(base+'/api/progress/report',data=json.dumps(self.payload).encode(),headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as error:urlopen(request)
            self.assertEqual(error.exception.code,403)
            request.add_header('X-Fleet-Token',app.token)
            with urlopen(request) as response:self.assertFalse(json.load(response)['duplicate'])
            with patch.object(app.backlog,'snapshot',return_value={}):
                with urlopen(base+'/api/progress?fleetId='+f['id']) as response:
                    self.assertEqual(json.load(response)['agents'][0]['reports'][0]['kind'],'task-started')
            with urlopen(base+'/api/progress/history?fleetId='+f['id']) as response:
                self.assertEqual(len(json.load(response)['items']),1)
        finally:
            server.shutdown();thread.join();server.server_close()

    def test_invalid_thresholds_and_unsafe_evidence_are_rejected(self):
        for value in ({'quietMinutes':True},{'retentionDays':0},{'surprise':1}):
            with self.assertRaises(ValueError):clean_settings(value)
        app,f=self.app()
        with self.assertRaises(ValueError):app.progress.report({**self.payload,'evidenceUrl':'javascript:alert(1)'})

    def test_session_changes_are_observations_not_completed_work(self):
        app,f=self.app();a=f['agents'][0]
        monitor={'sessions':{a['sessionId']:{'sessionId':a['sessionId'],'status':'working','goalStatus':None,'queueCount':0,'observedAt':self.now}}}
        app.progress.observe_sessions([f],monitor)
        app.progress.observe_sessions([f],monitor)
        self.assertEqual(len(app.progress.history(f['id'])['items']),1)
        self.now+=10;monitor['sessions'][a['sessionId']].update(status='idle',observedAt=self.now)
        app.progress.observe_sessions([f],monitor)
        self.assertEqual(len(app.progress.history(f['id'])['items']),2)
        self.assertEqual(self.snapshot(app,f)['summary']['completed24h'],0)

    def test_closed_tracker_task_conflicts_with_active_report(self):
        app,f=self.app();app.progress.report(self.payload)
        self.observe(app,f,'closed','closed')
        d=self.snapshot(app,f)
        self.assertTrue(any(a['kind']=='conflict' for a in d['attention']))
        self.assertEqual(d['summary']['completed24h'],0)  # Initial closure is not an observed transition.

    def test_partial_snapshot_preserves_unseen_tasks_and_new_source_hides_old_counts(self):
        app,f=self.app();self.observe(app,f)
        self.now+=90
        app.progress.observe_backlog(f,{'items':[],'observedAt':self.now,'partial':True})
        self.assertEqual(self.snapshot(app,f)['summary']['openTasks'],1)
        state=app.store.snapshot();state['fleets'][0]['backlog']['projectUrl']='https://gitlab.example/other/project';app.store.save(state)
        self.assertEqual(self.snapshot(app,f)['summary']['openTasks'],0)

    def test_handoff_observations_outlive_trigger_display_history(self):
        app,f=self.app()
        app.triggers.data['actions']=[{**self.handoff(f),'id':'handoff-'+str(i)} for i in range(110)]
        app.triggers.persist()
        app.triggers.data['actions']=[];app.triggers.persist()
        self.assertEqual(len(app.progress.pending_handoffs(f['id'])),110)
