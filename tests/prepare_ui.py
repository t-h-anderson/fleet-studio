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
         'reason':'Waiting for reviewed input','nextAction':'Coordinator supplies the input contract'}
app.progress.report(payload)
rule=next(t for t in f['triggers'] if t['sourceRoleId']==writer['roleIds'][0] and t['targetRoleId']==integrator['roleIds'][0])
app.progress.observe_handoffs([{'id':'synthetic-handoff','fleetId':f['id'],'eventId':'packet-101','name':'Submit reviewed packet','createdAt':now-4000,
    'status':'queued','ticket':url,'targetAgentId':integrator['id'],'targetName':integrator['name'],'targetSessionId':integrator['sessionId'],
    'targetDirectory':integrator['directory'],'triggerId':rule['id'],'deploymentId':rid,'sourceAgentId':writer['id'],'detail':'Accepted by queue; agent receipt pending.'}])
issues=[{'iid':101,'title':'Confirm the input contract','state':'opened','labels':['AI Blocked'],'updated_at':'2026-10-13T12:00:00Z','assignees':[]},
        {'iid':102,'title':'Deliver reviewed helper','state':'closed','labels':[],'updated_at':'2026-10-13T12:00:00Z','assignees':[]}]
(root/'issues.json').write_text(json.dumps(issues),encoding='utf-8')
(root/'fixture.json').write_text(json.dumps({'fleetId':f['id'],'writerId':writer['id'],'integratorId':integrator['id'],'payload':payload}),encoding='utf-8')
