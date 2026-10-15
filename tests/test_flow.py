"""Workflow import, ticket projection and transcript-filtering regressions."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
import uuid
import test_studio as fixtures
from fleet_studio import Application
from studio_backlog import Backlog, clean_config, fetch_issues, local_references, stage_of
from studio_workflow import populate_roles, populate_triggers, contract_problem
from studio_activity import public_messages, activity

class FlowTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown

    def test_missing_specialist_roles_are_local_and_idempotent(self):
        f=self.store.snapshot()['fleets'][0]
        f['agents'][0].update(role='Review',roleIds=[])
        f['agents'][1].update(role='Data',roleIds=[])
        original=deepcopy(f['roles'])
        self.assertEqual(populate_roles(f),['Audit / review','Material data'])
        self.assertEqual(populate_roles(f),[])
        self.assertEqual(f['roles'][:len(original)],original)
        self.assertTrue(all(not r['baseline'] for r in f['roles'][len(original):]))
        self.assertNotEqual(f['agents'][0]['roleIds'],[r['id'] for r in f['roles'] if r['key']=='subagent-reviewers'])

    def test_workflow_drafts_are_disabled_linked_and_never_overwrite(self):
        f=self.store.snapshot()['fleets'][0]
        result=populate_triggers(f)
        self.assertTrue(result['created'])
        self.assertTrue(result['skippedInternal'])
        self.assertTrue(all(not t['enabled'] and t['mode']=='automatic' for t in f['triggers']))
        self.assertTrue(all(not contract_problem(f,t) for t in f['triggers']))
        f['triggers'][0]['message']='User edited instruction'
        saved=deepcopy(f['triggers'])
        self.assertEqual(populate_triggers(f)['created'],[])
        self.assertEqual(f['triggers'],saved)
        link=next(l for l in f['roleLinks'] if l['id']==f['triggers'][0]['workflowLinkId'])
        link['condition']='New custody condition'
        self.assertIn('changed',contract_problem(f,f['triggers'][0]))

    def test_changed_contract_cannot_automatically_dispatch(self):
        app=Application(self.root,launch_enabled=False)
        state=app.store.snapshot(); f=state['fleets'][0]
        populate_triggers(f);f['triggers'][0]['enabled']=True
        link=next(l for l in f['roleLinks'] if l['id']==f['triggers'][0]['workflowLinkId'])
        link['label']='Changed action'
        app.store.save(state);f=app.store.fleet(f['id'])
        with patch.object(app.controls,'send') as send:
            app.triggers.fire(f,f['triggers'][0],'test event',{})
            send.assert_not_called()
        self.assertEqual(app.triggers.snapshot()['actions'][0]['status'],'held')
        self.assertIn('source workflow',app.triggers.snapshot()['actions'][0]['detail'])

    def test_backlog_paginates_all_states_and_preserves_label_priority(self):
        def issue(i,labels=(),state='opened'):
            return {'iid':i,'title':'Ticket '+str(i),'state':state,'labels':list(labels),'updated_at':'2026-10-01T12:00:00Z','assignees':[]}
        calls=[]
        def get(url,suffix):
            calls.append(suffix)
            return [issue(i,['AI Ready']) for i in range(1,101)] if 'page=1&' in suffix else [issue(101,['AI Awaiting Merge','AI Blocked']),issue(102,[], 'closed')]
        rows,partial=fetch_issues({'projectUrl':'https://gitlab.example/a/b','scope':'all'},get)
        self.assertEqual(len(rows),102);self.assertFalse(partial);self.assertEqual(len(calls),2)
        self.assertTrue(all('state=all' in c for c in calls))
        self.assertEqual(next(r for r in rows if r['iid']==101)['stage'],'blocked')
        self.assertEqual(next(r for r in rows if r['iid']==102)['stage'],'closed')

    def test_reference_links_do_not_claim_or_reroute_tickets(self):
        app=Application(self.root,launch_enabled=False)
        state=app.store.snapshot();f=state['fleets'][0]
        f['agents'][0].update(role='Review',roleIds=[]);populate_roles(f)
        f['backlog']={'projectUrl':'https://gitlab.example/a/b','scope':'all','agentTickets':{f['agents'][0]['id']:[1],f['agents'][1]['id']:[1]}}
        app.store.save(state)
        cfg=f['backlog']; refs=local_references(f,cfg)
        self.assertEqual(len(refs[1]),2)
        app.backlog.cache[f['id']]={'key':json.dumps([cfg['projectUrl'],cfg['scope'],False]),'items':[{'iid':1,'title':'Active','stage':'active'}],
          'changes':[],'observedAt':time.time(),'attemptedAt':time.time(),'loading':False,'error':'','partial':False}
        ticket=app.backlog.snapshot(f['id'])['items'][0]
        writer=next(r['id'] for r in f['roles'] if r['key']=='writer')
        self.assertEqual(ticket['roleIds'],[writer]);self.assertEqual(len(ticket['references']),2)

    def test_stale_backlog_retains_previous_success(self):
        app=Application(self.root,launch_enabled=False);f=app.store.snapshot()['fleets'][0]
        cfg={'projectUrl':'https://gitlab.example/a/b','scope':'all','agentTickets':{}}
        item={'items':[{'iid':1}],'changes':[],'observedAt':time.time()-300,'loading':True}
        app.backlog.cache[f['id']]=item
        app.backlog._refresh(f['id'],cfg,item)
        self.assertEqual(item['items'],[{'iid':1}]);self.assertIn('disabled',item['error']);self.assertFalse(item['loading'])

    def test_backlog_rejects_unsafe_project_urls_and_bad_links(self):
        for url in ['http://example.com/a/b','https://secret@example.com/a/b','https://example.com/a/b?token=secret']:
            with self.assertRaises(ValueError):clean_config({'projectUrl':url},set())
        with self.assertRaises(ValueError):clean_config({'agentTickets':{'absent':[2]}},set())

    def test_legacy_references_require_explicit_mapping_and_survive_rename(self):
        f=self.store.snapshot()['fleets'][0];a=f['agents'][0]
        a['name']='Renamed worker'
        status=self.root/'worker.md'
        status.write_text('https://gitlab.example/team/project/-/issues/99',encoding='utf-8')
        config={'projectUrl':'https://gitlab.example/team/project','agentTickets':{}}
        self.assertEqual(local_references(f,config),{})
        config['legacyReferences']={a['id']:str(status)}
        self.assertEqual(local_references(f,config)[99][0]['agentId'],a['id'])
        config['projectUrl']='https://gitlab.example/other/project'
        self.assertEqual(local_references(f,config),{})

    def test_activity_excludes_tools_and_internal_reasoning(self):
        def response(payload):return {'type':'response_item','timestamp':'2026-10-13T12:00:00Z','payload':payload}
        rows=[response({'type':'message','id':'input','role':'user','content':[{'type':'input_text','text':'Review ticket 10'}]}),
              response({'type':'message','id':'progress','role':'assistant','phase':'commentary','content':[{'type':'output_text','text':'Checking the diff.'}]}),
              response({'type':'message','id':'final','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'Tests passed.'}]}),
              response({'type':'reasoning','id':'summary','summary':[{'type':'summary_text','text':'Checking edge cases'}],'encrypted_content':'SECRET','content':'RAW'}),
              response({'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'HIDDEN'}]}),
              response({'type':'message','role':'system','content':[{'type':'input_text','text':'SYSTEM'}]}),
              response({'type':'function_call_output','output':'TOOL'}),
              {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'Reasoning','raw_content':'RAW'}}}]
        messages=public_messages(rows+rows[:1]+[None, {'type':'response_item','payload':None}, response({'type':'message','role':'user','content':None}), response({'type':'reasoning','summary':[None,5]})])
        self.assertEqual([m['kind'] for m in messages],['input','progress','summary','thinking'])
        self.assertNotIn('SECRET',json.dumps(messages));self.assertNotIn('RAW',json.dumps(messages));self.assertNotIn('TOOL',json.dumps(messages))

    def test_activity_uses_registered_transcript_and_tolerates_partial_lines(self):
        sid=self.legacy['agents'][0]['sessionId'];p=self.root/'session.jsonl'
        line={'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'Done'}]}}
        p.write_text(json.dumps(line)+'\n{"partial":',encoding='utf-8')
        with sqlite3.connect(self.root/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads(id,rollout_path)');db.execute('INSERT INTO threads VALUES(?,?)',(sid,str(p)))
        db.close()
        self.assertEqual(activity(self.root,sid)['messages'][0]['text'],'Done')
        with sqlite3.connect(self.root/'state_5.sqlite') as db:db.execute('UPDATE threads SET rollout_path=?',(str(self.root.parent/'outside.jsonl'),))
        db.close()
        self.assertIn('outside',activity(self.root,sid)['error'])

    def test_workflow_source_fields_reject_malformed_types(self):
        from studio_triggers import validate_triggers
        f=self.store.snapshot()['fleets'][0];populate_triggers(f);template=f['triggers'][0]
        for fields in [{'workflowLinkId':[]},{'workflowSignature':{}},{'workflowLinkId':'','workflowSignature':'a'*64}]:
            with self.assertRaises(ValueError):validate_triggers([{**template,**fields}],{r['id'] for r in f['roles']},set())
