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

    def test_state_time_accumulates_revisits_but_excludes_gaps_and_transition_intervals(self):
        app,f=self.app();self.observe(app,f)
        self.now+=90;self.observe(app,f)
        self.now+=90;self.observe(app,f,'blocked')
        self.now+=90;self.observe(app,f,'blocked')
        self.now+=90;self.observe(app,f)
        self.now+=90;self.observe(app,f)
        self.now+=600;self.observe(app,f)
        task=self.snapshot(app,f)['tasks'][0]
        self.assertEqual(task['stateSeconds'],{'active':180,'blocked':90})
        self.assertTrue(task['observationGap'])
        app.progress=Progress(app,lambda:self.now)
        self.assertEqual(self.snapshot(app,f)['tasks'][0]['stateSeconds'],task['stateSeconds'])

    def test_backlog_trend_is_hourly_and_marks_incomplete_snapshots(self):
        app,f=self.app();self.observe(app,f)
        self.now+=5;self.observe(app,f,'blocked',partial=True)
        points=self.snapshot(app,f)['metrics']['backlogTrend']
        self.assertEqual(len(points),1)
        self.assertEqual(points[0]['open'],1);self.assertTrue(points[0]['partial'])
        self.now+=3600;self.observe(app,f,'closed','closed')
        self.assertEqual([p['open'] for p in self.snapshot(app,f)['metrics']['backlogTrend']],[1,0])
        state=app.store.snapshot();state['fleets'][0]['backlog']['projectUrl']='https://gitlab.example/other/repo';app.store.save(state)
        self.assertEqual(self.snapshot(app,f)['metrics']['backlogTrend'],[])

    def test_tokens_use_baselines_deduplicate_sessions_and_detect_resets(self):
        app,f=self.app();a=f['agents'][0]
        duplicate=deepcopy(f);duplicate['agents'][1]['sessionId']=a['sessionId']
        def observe(total):
            monitor={'observedAt':self.now,'sessions':{a['sessionId']:{'sessionId':a['sessionId'],'status':'working','observedAt':self.now,'usage':{'tokensUsed':total}}}}
            app.monitor.update(monitor)
            app.progress.metrics.observe_tokens([duplicate],monitor)
        observe(1000)
        self.assertEqual(self.snapshot(app,f)['metrics']['tokens']['observed'],0)
        self.now+=10;observe(1500);observe(1500)
        self.now+=10;observe(50)
        self.now+=10;observe(75)
        app.progress=Progress(app,lambda:self.now)
        tokens=self.snapshot(app,f)['metrics']['tokens']
        self.assertEqual(tokens['observed'],525)
        self.assertEqual(tokens['total'],75)
        self.assertEqual(tokens['agents'][0]['resets'],1)
        self.now+=30
        self.assertIsNone(self.snapshot(app,f)['metrics']['tokens']['total'])

    def test_test_results_preserve_blockers_and_require_specific_evidence(self):
        app,f=self.app()
        app.progress.report({**self.payload,'kind':'blocked','reason':'Host unavailable','nextAction':'Owner recovers host'})
        result={**self.payload,'eventId':'unit-run','kind':'test-result','suite':'Unit checks','testStatus':'passed',
                'revision':'a'*40,'passed':12,'failed':0,'durationSeconds':2.5}
        with patch.object(app.controls,'send') as send:
            self.assertFalse(app.progress.report(result)['duplicate'])
            self.assertTrue(app.progress.report(result)['duplicate'])
            send.assert_not_called()
        d=self.snapshot(app,f)
        self.assertEqual(d['metrics']['tests'][0]['passed'],12)
        self.assertEqual(d['agents'][0]['reports'][0]['kind'],'blocked')
        for change in ({'revision':'main'},{'failed':1},{'passed':True},{'durationSeconds':float('nan')}):
            with self.assertRaises(ValueError):app.progress.report({**result,**change,'eventId':'bad'})
        with closing(app.progress.connect()) as db,db:app.progress.metrics.prune(db,f['id'],self.now+1)
        self.assertEqual(self.snapshot(app,f)['metrics']['tests'],[])
        self.assertTrue(app.progress.report(result)['duplicate'])

    def test_ci_no_test_report_is_not_a_pass_and_outage_retains_last_snapshot(self):
        app,f=self.app();metrics=app.progress.metrics
        item=metrics.ci_snapshot(f);metrics.ci[f['id']]=item
        responses=[[{'id':4,'status':'success','sha':'a'*40,'ref':'main','web_url':'https://gitlab.example/pipelines/4'}],
                   {'duration':5,'queued_duration':3},{'total':{'total_count':0}}]
        with patch('studio_trackers.gitlab_get',side_effect=responses):metrics._refresh_ci(f['id'],item)
        self.assertEqual(item['pipelines'][0]['status'],'success')
        self.assertIsNone(item['testSummary'])
        self.assertIn('No published test report',item['testNotice'])
        responses[2]={'total':{'total_count':10,'success_count':9,'failed_count':1,'skipped_count':0,'error_count':0,'time':5}}
        with patch('studio_trackers.gitlab_get',side_effect=responses):metrics._refresh_ci(f['id'],item)
        self.assertEqual(item['testSummary']['failed_count'],1)
        self.assertEqual(item['pipelines'][0]['queued_duration'],3)
        with patch('studio_trackers.gitlab_get',side_effect=ValueError('unavailable')):metrics._refresh_ci(f['id'],item)
        self.assertEqual(item['pipelines'][0]['id'],4)
        self.assertTrue(metrics.ci_snapshot(f)['stale'])
        app.progress=Progress(app,lambda:self.now)
        self.assertEqual(app.progress.metrics.ci_snapshot(f)['pipelines'][0]['id'],4)

    def test_missing_usage_is_unknown_not_zero(self):
        app,f=self.app()
        self.assertIsNone(self.snapshot(app,f)['metrics']['tokens']['total'])
        from studio_runtime import session_usage
        usage=session_usage(self.root,[f['agents'][0]['sessionId']])
        self.assertEqual(usage[f['agents'][0]['sessionId']],{})

    def test_blocker_qualification_does_not_count_restricted_or_unclaimed_work_as_execution(self):
        from studio_metrics import blocker_category
        base={'state':'opened','stage':'blocked','labels':['AI Blocked']}
        self.assertEqual(blocker_category(base),'unclaimed')
        self.assertEqual(blocker_category({**base,'labels':['AI Banned','AI Claimed']}),'restricted')
        self.assertEqual(blocker_category({**base,'labels':['AI Blocked','Deferred']}),'deferred')
        self.assertEqual(blocker_category({**base,'labels':['AI Blocked','AI Claimed']}),'execution')
        self.assertEqual(blocker_category(base,True),'execution')
        self.assertIsNone(blocker_category({**base,'state':'closed'},True))

    def test_idle_blocked_goal_is_held_and_observation_window_survives_restart(self):
        app,f=self.app();self.observe(app,f);first=self.now;a=f['agents'][0]
        app.monitor.update(observedAt=self.now,sessions={a['sessionId']:{'sessionId':a['sessionId'],'status':'idle','goalStatus':'blocked','goalKnown':True,'queueCount':0,'pids':[123],'observedAt':self.now}})
        d=self.snapshot(app,f)
        self.assertEqual(d['agents'][0]['availability']['state'],'held')
        self.assertIn('No current structured blocker reason',d['agents'][0]['availability']['reason'])
        self.assertTrue(any(a['kind']=='goal-stop' for a in d['attention']))
        self.assertEqual(d['summary']['closureWindow7d'],first)
        app.progress=Progress(app,lambda:self.now);self.now+=90;self.observe(app,f,'closed','closed')
        self.assertEqual(self.snapshot(app,f)['summary']['observationStartedAt'],first)

    def test_native_usage_reads_only_linked_sessions_and_handles_older_schema(self):
        import sqlite3
        from studio_runtime import session_usage
        with closing(sqlite3.connect(self.root/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT, tokens_used INTEGER, model TEXT)')
            db.executemany('INSERT INTO threads VALUES(?,?,?)',[('linked',123456,'test-model'),('unlinked',999999,'other')])
        with closing(sqlite3.connect(self.root/'goals_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_goals (thread_id TEXT, tokens_used INTEGER, token_budget INTEGER)')
            db.execute('INSERT INTO thread_goals VALUES(?,?,?)',('linked',100,200))
        result=session_usage(self.root,['linked','missing'])
        self.assertNotIn('unlinked',result)
        self.assertEqual(result['linked'],{'tokensUsed':123456,'model':'test-model','goalTokens':100,'goalBudget':200})
        self.assertEqual(result['missing'],{})
        with closing(sqlite3.connect(self.root/'state_5.sqlite')) as db, db:
            db.execute('ALTER TABLE threads DROP COLUMN tokens_used')
        self.assertIsNone(session_usage(self.root,['linked'])['linked']['tokensUsed'])
