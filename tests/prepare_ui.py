"""Build a synthetic fleet; never reads live configuration or sends instructions."""
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleet_studio import Application
from workflow_spec import apply_spec

root=Path(sys.argv[1]).resolve()
root.mkdir(parents=True,exist_ok=True)
legacy={'version':1,'windowName':'test-fleet','codexPath':sys.executable,'agents':[]}
for name in ('Coordinator','Writer','Integrator'):
    folder=root/name
    folder.mkdir()
    legacy['agents'].append({'name':name,'directory':str(folder),'sessionId':str(uuid.uuid4()),'enabled':True})
(root/'fleet.json').write_text(json.dumps(legacy),encoding='utf-8')
app=Application(root,launch_enabled=False)
state=app.store.snapshot();f=state['fleets'][0]
spec=json.loads((Path(__file__).resolve().parents[1]/'workflows/sample.workflow.json').read_text())
f=apply_spec(f,spec)
for a in f['agents']:
    a['roleIds']=[next(r['id'] for r in f['roles'] if r['name']==a['name'])]
f['backlog']={'projectUrl':'https://gitlab.example/team/project','scope':'all','agentTickets':{}}
state['fleets'][0]=f;app.store.save(state)
p=app.deployments.preview({'fleetId':f['id'],'revision':app.store.snapshot()['revision'],'kind':'deploy'})
app.deployments.apply(p['id']);f=app.store.fleet(f['id']);rid=app.deployments.data['active'][f['id']]
for a in f['agents']:
    app.deployments.acknowledge({'fleetId':f['id'],'agentId':a['id'],'releaseId':rid})
now=time.time();writer=f['agents'][1];integrator=f['agents'][2]
url=f['backlog']['projectUrl']+'/-/issues/101'
payload={'fleetId':f['id'],'agentId':writer['id'],'sessionId':writer['sessionId'],'releaseId':rid,'eventId':'blocked-101','kind':'blocked',
         'taskUrl':url,'evidenceUrl':url+'#note_1','summary':'Review input contract <script>window.injected=true</script>',
         'reason':'Waiting for reviewed input','blockerOwner':'Coordinator','nextAction':'Coordinator supplies the input contract'}
app.progress.report(payload)
app.progress.report({**payload,'eventId':'test-101','kind':'test-result','suite':'Contract checks',
                     'testStatus':'passed','revision':'a'*40,'passed':12,'failed':0,'skipped':1,'durationSeconds':2.5})
rule=next(t for t in f['triggers'] if t['sourceRoleId']==writer['roleIds'][0] and t['targetRoleId']==integrator['roleIds'][0])
app.progress.observe_handoffs([{'id':'synthetic-handoff','fleetId':f['id'],'eventId':'packet-101','name':'Submit reviewed packet','createdAt':now-4000,
    'status':'queued','ticket':url,'targetAgentId':integrator['id'],'targetName':integrator['name'],'targetSessionId':integrator['sessionId'],
    'targetDirectory':integrator['directory'],'triggerId':rule['id'],'deploymentId':rid,'sourceAgentId':writer['id'],'detail':'Accepted by queue; agent receipt pending.'}])
legacy_key=f['id']+':'+writer['id']+':packet-ready:legacy-event'
app.triggers.data['events'].append({'key':legacy_key,'eventId':'legacy-event','eventName':'packet-ready','agent':writer['name'],'ticket':url,'createdAt':now-300})
app.triggers.data['actions'].append({'id':'legacy-receipt','fleetId':f['id'],'eventId':'legacy-event','eventKey':legacy_key,'name':'Legacy decision',
    'createdAt':now-300,'status':'needs-attention','ticket':url,'targetAgentId':integrator['id'],'targetName':integrator['name'],
    'targetSessionId':integrator['sessionId'],'targetDirectory':integrator['directory'],'triggerId':rule['id'],
    'deploymentId':rid,'mode':'review','message':'Existing decision','detail':'Source identity missing.'})
app.triggers.persist()
issues=[{'iid':101,'title':'Confirm the input contract','state':'opened','labels':['AI Blocked'],'updated_at':'2026-10-13T12:00:00Z','assignees':[]},
        {'iid':102,'title':'Deliver reviewed helper','state':'closed','labels':[],'updated_at':'2026-10-13T12:00:00Z','assignees':[]}]
(root/'issues.json').write_text(json.dumps(issues),encoding='utf-8')
(root/'fixture.json').write_text(json.dumps({'fleetId':f['id'],'writerId':writer['id'],'integratorId':integrator['id'],'payload':payload}),encoding='utf-8')
# Synthetic turn telemetry: the writer repeats a failing test, the integrator mostly reports.
from studio_automation import PRESETS
from studio_telemetry import call_metadata
def turn(agent, at, calls, tokens, started=None):
    classes = {k: 0 for k in ('work', 'explore', 'admin', 'wait', 'other')}
    for c in calls:
        classes[c['cls']] += 1
    return {'id': agent['id'][:8] + str(int(at)), 'session': agent['sessionId'], 'startedAt': started or at - 120, 'lastAt': at, 'status': 'completed',
            'fromStudio': False, 'tokens': {'input': tokens, 'output': tokens // 10, 'cacheRead': tokens * 4, 'cacheWrite': 0},
            'calls': calls, 'callCount': len(calls), 'classes': classes, 'errors': sum(c['error'] for c in calls), 'replies': []}
def call(tool, arguments, at, error=False):
    return {**call_metadata(tool, arguments), 'at': at, 'error': error, 'seq': 0}
coordinator = f['agents'][0]
looping = [call('Bash', {'command': 'pytest tests/test_contract.py'}, now - 900 + i * 60, error=True) for i in range(6)]
app.telemetry.store(f['id'], writer['id'], [turn(writer, now - 600, [call('Read', {'file_path': 'src/contract.py'}, now - 1000)] + looping, 42000)])
admin = [call('Bash', {'command': 'python fleet_progress.py --kind progress-updated --id x' + str(i)}, now - 1500 + i * 30) for i in range(7)]
admin += [call('Bash', {'command': 'glab issue note 101 -m "still checking"'}, now - 1200), call('Edit', {'file_path': 'docs/merge.md'}, now - 1100)]
# This turn began before the synthetic handoff, so it is not an observed receipt.
app.telemetry.store(f['id'], integrator['id'], [turn(integrator, now - 900, admin, 18000, started=now - 5000)])
work = [call('Edit', {'file_path': 'plan/q4.md'}, now - 200), call('Bash', {'command': 'git commit -m "Plan"'}, now - 150)]
app.telemetry.store(f['id'], coordinator['id'], [turn(coordinator, now - 120, work, 9000)])
state = app.store.snapshot()
state['fleets'][0]['automations'] = [{**p, 'id': str(uuid.uuid4()), 'enabled': False} for p in PRESETS]
app.store.save(state)
app.automation.data['fires'].append({'id': 'synthetic-fire', 'fleetId': f['id'], 'ruleId': 'synthetic', 'ruleName': 'Break loops',
    'agentId': writer['id'], 'agentName': writer['name'], 'action': 'reprompt', 'fallback': False, 'at': now - 3000, 'episode': now - 3100,
    'healthState': 'looping', 'evidence': 'Ran `pytest` 4 times, failing each time.', 'status': 'sent', 'detail': 'Queued.', 'outcome': 'recovered'})
app.automation.persist()
