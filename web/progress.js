'use strict';
const progressCache=new Map(),progressRequests=new Set();
let progressDetail=null,progressHistory=[],progressCursor=null,progressTab='attention';
const progressData=()=>progressCache.get(activeId);
const elapsed=at=>!at?'Unknown':Math.max(0,Math.floor((Date.now()/1000-at)/60))<60?Math.max(0,Math.floor((Date.now()/1000-at)/60))+'m':Math.floor((Date.now()/1000-at)/3600)+'h';
const stamp=(source,at)=>'<span class="progress-source">'+esc(source)+' · '+(at?esc(new Date(at*1000).toLocaleString()):'Not observed')+'</span>';
function progressQualification(d){
 const s=d.summary,date=at=>at?esc(new Date(at*1000).toLocaleString()):'not established';
 return '<div class="progress-qualification"><p>Other tracker holds: <strong>'+s.restrictedTasks+'</strong> restricted · <strong>'+s.deferredBlockedTasks+'</strong> deferred / later-stage · <strong>'+s.unclaimedBlockedTasks+'</strong> unclaimed / dependency. These are separate from active execution blockers.</p><p>Issue-closure observation windows: 24h since '+date(s.closureWindow24h)+'; 7d since '+date(s.closureWindow7d)+'. Counts exclude earlier deliveries and initially closed issues; they are not total delivered work.</p></div>';
}
function evidenceLink(url,label='Open evidence'){
 try{if(new URL(url).protocol!=='https:')return '';}catch{return '';}
 return '<a class="text-button" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(label)+'</a>';
}
async function loadProgress(force=false){
 if(!state||stopped||progressRequests.has(activeId))return;
 const fid=activeId;progressRequests.add(fid);
 try{
  const data=await api('/api/progress?fleetId='+encodeURIComponent(fid));
  progressCache.set(fid,data);
  if(fid===activeId&&view==='progress')renderProgress();
 }catch(e){if(force)toast(e.message,true);const old=progressCache.get(fid);progressCache.set(fid,old&&!old.error?{...old,transportError:'Progress refresh failed; showing earlier observations.'}:{error:e.message});if(fid===activeId&&view==='progress')renderProgress();}
 finally{progressRequests.delete(fid);}
}
function progressRelease(d){
 const r=d.deployment,active=r.active,required=r.adoption.filter(a=>a.required),adopted=required.filter(a=>a.acknowledged&&a.bindingMatches).length;
 return '<div class="progress-release"><div><strong>'+(active?'Active workflow '+esc(active.version):'No deployed workflow')+'</strong><span>'+adopted+'/'+required.length+' agents acknowledged · '+(r.paused?'Automation paused':r.enabledTriggers+' active triggers')+(r.draftChanged?' · Undeployed changes':'')+'</span></div><div><button class="button small" data-progress-view="workflow">Review workflow</button>'+(active?'<button class="button small" id="progress-pause">'+(r.paused?'Resume automation':'Pause automation')+'</button>':'')+(active&&!r.reportingEnabled?'<button class="button small" id="progress-reporting">Set up reporting</button>':'')+'<button class="button small" id="progress-settings">Monitoring settings</button></div></div>';
}
function reportMarkup(r){
 return '<div class="progress-report"><strong>'+esc(r.kind.replaceAll('-',' '))+'</strong>'+(r.bindingCurrent===false?' <span class="stage-tag">Earlier assignment</span>':'')+'<p>'+esc(r.summary||r.reason||'No summary supplied')+'</p>'+(r.blockedSince?'<p>Blocked for '+elapsed(r.blockedSince)+': '+esc(r.reason)+'</p>':'')+(r.nextAction?'<p>Next: '+esc(r.nextAction)+'</p>':'')+(r.dependsOn?.length?'<p>Reported dependencies: '+r.dependsOn.map(u=>evidenceLink(u,u)).join(' · ')+'</p>':'')+(r.conflict?'<p class="progress-warning">Tracker closure conflicts with this report.</p>':'')+stamp('Agent report · '+r.agentName,r.at)+evidenceLink(r.evidenceUrl)+'</div>';
}
function assignmentMarkup(a){
 if(!a)return '<p class="assignment-empty">No assignment reported. Check current scope before assigning work.</p>';
 return '<div class="assignment-observation"><strong class="stage-tag">'+esc(a.label)+'</strong>'+(a.stale?' <span class="progress-warning">Earlier evidence</span>':'')+'<p><button class="text-button" data-progress-task="'+esc(a.taskUrl)+'">'+esc(a.title)+'</button></p>'+(a.reason?'<p>'+esc(a.reason)+'</p>':'')+(a.state==='waiting'||a.state==='assigned'?'<p>Waiting on: <strong>'+esc(a.owner||'Owner not reported')+'</strong></p>':'')+'<p>Next: '+esc(a.nextAction)+'</p>'+(a.dependsOn?.length?'<p>Dependencies: '+a.dependsOn.map(u=>evidenceLink(u,u)).join(' · ')+'</p>':'')+(a.waitingObservedSeconds!=null?'<p class="field-hint">Observed wait: '+metricDuration(a.waitingObservedSeconds)+(a.waitingObservationGap?' · Gaps excluded':'')+'. Only intervals between matching reports count.</p>':'')+stamp(a.source,a.evidenceAt)+evidenceLink(a.evidenceUrl)+'</div>';
}
function agentProgress(a){
 const roles=progressData().roles.filter(x=>a.roleIds.includes(x.id)),assignments=a.assignments||[];
 return '<article class="assignment-agent" data-assignment-agent="'+a.id+'"><h3><button class="text-button" data-progress-agent="'+a.id+'">'+esc(a.name)+'</button></h3><p class="field-hint">'+esc(roles.map(r=>r.name).join(', '))+'</p>'+assignmentMarkup(assignments[0])+(assignments.length>1?'<button class="text-button" data-progress-agent="'+a.id+'">Inspect all '+assignments.length+' assignment observations</button>':'')+'<div class="assignment-session"><p>Session: '+esc(a.session.stale?'Unknown / stale':labels[a.session.status]||'Unknown')+' · Goal: '+esc(a.session.goalStatus||'None observed')+'</p><p class="field-hint">'+esc(a.availability?.reason||'Availability unknown')+'</p></div></article>';
}
function attentionProgress(item){
 return '<button class="progress-attention" '+(item.handoffId?'data-progress-handoff="'+item.handoffId+'"':item.taskUrl?'data-progress-task="'+esc(item.taskUrl)+'"':'data-progress-agent="'+item.agentId+'"')+'><span class="stage-tag">'+esc(item.kind)+'</span><strong>'+esc(item.message)+'</strong><span>'+esc(item.nextAction||'Inspect evidence and current scope')+'</span><small>'+(item.at?elapsed(item.at)+' since observation':item.kind==='adoption'?'Review adoption':'Time not established')+'</small></button>';
}
function handoffProgress(h){
 return '<tr><td><button class="text-button" data-progress-handoff="'+h.id+'">'+esc(h.name)+'</button></td><td>'+esc(h.targetName)+'</td><td>'+esc(h.status)+'<small>'+esc(h.detail||'')+'</small></td><td>'+(h.receivedAt?(h.receiptSource==='operator-reconciliation'?'Receipt reconciled ':'Agent received ')+ago(h.receivedAt):h.status==='dismissed'?'Dismissed':'No agent receipt · '+elapsed(h.createdAt))+'</td></tr>';
}
function taskProgress(t){
 return '<tr><td><button class="text-button" data-progress-task="'+esc(t.url)+'">#'+t.iid+' '+esc(t.title)+'</button></td><td>'+esc(progressData().stages.find(s=>s.id===t.stage)?.name||t.stage)+'</td><td>'+elapsed(t.stageObservedAt)+'<small>'+(t.initialStageUnknown?'Entry time unknown; observed duration':'Since observed transition')+(t.observationGap?' · Observation gap':'')+'</small></td><td>'+esc(t.assignees.join(', ')||'Unassigned in tracker')+(t.blockerCategory?'<small>'+esc({execution:'Execution blocker',restricted:'Restricted work',deferred:'Deferred / later-stage',unclaimed:'Unclaimed / dependency hold'}[t.blockerCategory])+'</small>':'')+'</td></tr>';
}
function deployedGraph(d){
 const roles=FleetLayout.visibleRoles(d.roles,false),positions=FleetLayout.arrange(roles,d.roleLinks);
 const arranged=roles.map(r=>({...r,position:positions[r.id]}));
 const geometry=FleetLayout.route(arranged,FleetLayout.bundles(arranged,d.roleLinks)),width=geometry.width,height=geometry.height;
 const known=new Set(roles.map(r=>r.id));
 const edges=d.roleLinks.filter(l=>known.has(l.source)&&known.has(l.target));
 return '<div class="progress-graph-scroll"><div class="progress-graph" style="width:'+width+'px;height:'+height+'px"><svg width="'+width+'" height="'+height+'" aria-hidden="true"><defs><marker id="pg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10z" fill="currentColor"/></marker></defs>'+geometry.routes.map(r=>'<path d="'+r.points.map((p,i)=>(i?'L':'M')+p.x+' '+p.y).join(' ')+'" marker-end="url(#pg-arrow)"/>').join('')+'</svg>'+roles.map(r=>'<button class="progress-role" data-progress-role="'+r.id+'" style="left:'+positions[r.id].x+'px;top:'+positions[r.id].y+'px"><strong>'+esc(r.name)+'</strong><span>'+r.queueCount+' open tasks</span><span class="'+(r.waitingCount?'progress-warning':'')+'">'+r.waitingCount+' handoffs awaiting receipt</span><small>Inspect role and communication</small></button>').join('')+'</div></div>';
}
function renderProgress(){
 const d=progressData();
 if(!d||d.error){$('content').innerHTML='<div class="empty-state"><h3>'+(d?.error?'Progress unavailable':'Loading progress')+'</h3><p>'+esc(d?.error||'Combining tracker facts, agent reports and handoff receipts.')+'</p><button class="button" id="progress-refresh">Refresh</button></div>';if(!d)loadProgress();return;}
 const s=d.summary,needle=query.toLowerCase(),match=x=>JSON.stringify(x).toLowerCase().includes(needle);
 const notice=[d.transportError,d.observationError,!d.tracker.url?'Connect your external tracker in Backlog ? Project & links.':trackerSnapshotNotice(d.tracker)].filter(Boolean).join(' ');
 let body='';
 if(progressTab==='attention')body='<div class="progress-attention-list">'+d.attention.filter(match).map(attentionProgress).join('')+(!d.attention.length?'<p class="field-hint">No attention signals in the available observations.</p>':'')+'</div>';
 if(progressTab==='agents')body='<div class="assignment-grid">'+d.agents.filter(match).map(agentProgress).join('')+'</div>';
 if(progressTab==='handoffs')body='<div class="table-wrap"><table><thead><tr><th>HANDOFF</th><th>RECIPIENT</th><th>DELIVERY</th><th>AGENT RECEIPT</th></tr></thead><tbody>'+d.handoffs.filter(match).map(handoffProgress).join('')+'</tbody></table></div>';
 if(progressTab==='tasks')body='<div class="table-wrap"><table><thead><tr><th>TASK</th><th>TRACKER STAGE</th><th>OBSERVED TIME</th><th>TRACKER ASSIGNEES</th></tr></thead><tbody>'+d.tasks.filter(match).map(taskProgress).join('')+'</tbody></table></div>';
 if(progressTab==='workflow')body=(d.deployment.active?deployedGraph(d):'<p>Deploy a reviewed workflow to inspect its live queues.</p>');
 if(progressTab==='monitoring')body=metricsMarkup(d);
 if(progressTab==='goals')body=goalsMarkup(d);
 const focus=document.activeElement?.id;
 $('content').innerHTML=progressRelease(d)+(notice?'<div class="backlog-notice">'+esc(notice)+'</div>':'')+'<div class="progress-metrics">'+[['Open tracker tasks',d.tracker.observedAt?s.openTasks:'Unknown'],['Active execution blockers',d.tracker.observedAt?s.executionBlockedTasks:'Unknown'],['Awaiting handoff receipt',s.pendingHandoffs],['Observed closures · 24h / 7d',s.completed24h+' / '+s.completed7d]].map(([label,value])=>'<div><span>'+label+'</span><strong>'+value+'</strong></div>').join('')+'</div>'+progressQualification(d)+'<div class="progress-tabs" role="group" aria-label="Progress views">'+[['attention','Needs attention ('+d.attention.length+')'],['agents','Agents'],['handoffs','Handoffs'],['tasks','Tasks'],['monitoring','Monitoring'],['goals','Goals'],['workflow','Deployed workflow']].map(([id,label])=>'<button class="button small '+(progressTab===id?'primary':'')+'" data-progress-tab="'+id+'">'+label+'</button>').join('')+'</div>'+body+'<p class="progress-footnote">'+esc(d.historyNotice)+'</p>'+stamp('Tracker',d.tracker.observedAt);
 $('visible-count').textContent=d.attention.length;
 if(focus)$(focus)?.focus({preventScroll:true});
}
const progressOriginalRenderAll=renderAll;
renderAll=function(){
 progressOriginalRenderAll();if(!state)return;
 document.querySelector('main > .metrics').classList.toggle('hidden',view==='progress');
 $('content').classList.toggle('progress-content',view==='progress');
 if(view==='progress'){
  $('fleet-title').textContent='Progress';$('fleet-description').textContent='Work, waiting reasons and the next useful action.';
  $('view-eyebrow').textContent=current().name;$('panel-title').textContent='Fleet progress';
  $('workspace-layout').classList.add('board-layout');$('status-filter').classList.add('hidden');
  $('search').placeholder='Find work, agents or blockers';$('panel-footer-text').textContent='Reports describe work. Tracker records establish task state.';
  if(!progressCache.has(activeId))loadProgress();
 }
};
const detailDialog=document.createElement('dialog');detailDialog.id='progress-detail';detailDialog.className='editor-dialog wide-dialog';
detailDialog.innerHTML='<div class="dialog-header"><h2 id="progress-detail-title">Progress details</h2><button class="icon-button" id="close-progress-detail" aria-label="Close details">'+icon('close')+'</button></div><div class="dialog-fields" id="progress-detail-body"></div>';
document.body.append(detailDialog);$('close-progress-detail').onclick=()=>detailDialog.close();
async function openProgressDetail(detail,append=false){
 const fid=activeId;progressDetail={...detail,fleetId:fid};
 const d=progressData();if(!d||d.error)return;
 let title='',body='',task=detail.taskUrl,agent=detail.agentId;
 if(detail.handoffId){
  const h=d.handoffs.find(h=>h.id===detail.handoffId);if(!h)return;
  title=h.name;task=h.ticket;
  body='<p>Delivery: <strong>'+esc(h.status)+'</strong> · '+esc(h.detail||'')+'</p><p>Agent receipt: '+(h.receivedAt?esc(new Date(h.receivedAt*1000).toLocaleString()):'Not reported')+'</p><p>Recipient: '+esc(h.targetName)+'</p>'+stamp('Delivery observation',h.observedAt)+evidenceLink(task,'Open tracker task');
  if(h.releaseRevalidations?.length)body+='<p>Unsent event revalidated across '+h.releaseRevalidations.length+' policy release(s); original event identity retained.</p>'+h.releaseRevalidations.map(r=>evidenceLink(r.evidenceUrl,'Revalidation evidence')).join('');
  if(h.receiptSource)body+='<p>Receipt source: '+esc(h.receiptSource)+'</p>'+evidenceLink(h.receiptEvidence,'Inspect receipt evidence');
  if(h.reconciliation)body+='<p>'+esc(h.reconciliation.note)+'</p><p>Original delivery state: '+esc(h.reconciliation.previousStatus)+'. Technical acceptance remains separate.</p>';
  if(h.awaitingReceipt&&h.status!=='sending')body+='<button class="button small" data-reconcile-handoff="'+h.id+'">Reconcile receipt or source</button>';
  if(['pending','held','needs-attention'].includes(h.status))body+='<button class="button small" data-review-progress-handoff="'+h.id+'">Review delivery</button>';
  const rule=current().triggers.find(t=>t.id===h.triggerId);if(rule?.workflowLinkId)body+='<button class="button small" data-progress-edit-link="'+rule.workflowLinkId+'">Edit handoff draft</button>';
 }
 if(agent){
  const a=d.agents.find(a=>a.id===agent);if(!a)return;
  title=a.name;body='<p><strong>'+esc(a.availability?.reason||'Availability unknown')+'</strong></p><p>Session: '+esc(a.session.stale?'Unknown / stale':labels[a.session.status]||'Unknown')+'</p>'+stamp('Session observation',a.session.observedAt)+(a.adoption?'<p>Workflow: '+(a.adoption.acknowledged?'Acknowledged':'Awaiting acknowledgement')+' ? Entry point: '+esc(a.adoption.entryPoint)+' ? Binding: '+(a.adoption.bindingMatches?'Current':'Needs deployment')+'</p><button class="button small" data-progress-view="workflow">Review workflow adoption</button>':'')+'<h3>Assignment observations</h3>'+((a.assignments||[]).length?a.assignments.map(assignmentMarkup).join(''):assignmentMarkup(null))+'<h3>Reported milestones</h3>'+a.reports.map(reportMarkup).join('')+'<button class="button small" data-progress-activity="'+a.id+'">Inspect agent activity</button><button class="button small" data-progress-message="'+a.id+'">Review instruction</button>';
 }
 if(agent)body+=agentGoalMarkup(d,agent);
 if(detail.taskUrl){
  const t=d.tasks.find(t=>t.url===detail.taskUrl);title=t?'#'+t.iid+' '+t.title:'Reported task';
  body=(t?'<p>Tracker state: <strong>'+esc(t.state)+' / '+esc(d.stages.find(s=>s.id===t.stage)?.name||t.stage)+'</strong></p><p>Tracker assignees: '+esc(t.assignees.join(', ')||'Unassigned')+'</p><p>Observed in stage: '+elapsed(t.stageObservedAt)+(t.initialStageUnknown?' (entry time unknown)':'')+(t.observationGap?' · Observation gap':'')+'</p>'+stamp('Tracker',t.observedAt):'<p>No matching tracker observation.</p>')+evidenceLink(detail.taskUrl,'Open tracker task')+d.agents.flatMap(a=>a.reports).filter(r=>r.taskUrl===detail.taskUrl).map(reportMarkup).join('');
  if(t)body+=stateTimingMarkup(t,d)+'<div class="progress-actions">'+t.roleIds.map(id=>'<button class="button small" data-progress-role="'+id+'">Inspect responsible role</button>').join('')+'</div>';
  if(d.metrics)body+='<h3>Local test evidence</h3>'+testResultsMarkup(d.metrics.tests.filter(r=>r.taskUrl===detail.taskUrl));
 }
 if(detail.roleId){
  const r=d.roles.find(r=>r.id===detail.roleId);if(!r)return;
  title=r.name;body='<p>'+esc(r.responsibilities)+'</p><p>'+esc(r.authority)+'</p><p>'+r.queueCount+' tasks · '+r.waitingCount+' pending receipts</p><button class="button small" data-progress-edit-role="'+r.id+'">Edit role draft</button><h3>Communication contracts</h3>'+d.roleLinks.filter(l=>l.source===r.id||l.target===r.id).map(l=>'<div class="progress-report"><strong>'+esc(l.label)+'</strong><p>Trigger: '+esc(l.trigger)+'</p><p>Artifact: '+esc(l.artifact)+'</p><p>Condition: '+esc(l.condition)+'</p><button class="text-button" data-progress-edit-link="'+l.id+'">Edit handoff draft</button></div>').join('');
 }
 $('progress-detail-title').textContent=title;$('progress-detail-body').innerHTML=body+'<h3>Observation history</h3><div id="progress-history">Loading history…</div>';
 if(!detailDialog.open)detailDialog.showModal();
 const params=new URLSearchParams({fleetId:fid});if(task)params.set('taskUrl',task);if(agent)params.set('agentId',agent);if(append&&progressCursor)params.set('before',progressCursor);
 try{
  const history=await api('/api/progress/history?'+params);
  if(activeId!==fid||JSON.stringify(progressDetail)!==JSON.stringify({...detail,fleetId:fid}))return;
  progressHistory=append?[...progressHistory,...history.items]:history.items;progressCursor=history.nextBefore;
  $('progress-history').innerHTML=progressHistory.map(h=>'<div class="progress-history-item"><strong>'+esc(h.kind||h.name||(h.from+' → '+h.to))+'</strong>'+stamp(h.category,h.at)+(h.evidenceUrl?evidenceLink(h.evidenceUrl):'')+'</div>').join('')+(!progressHistory.length?'<p>No retained observations for this selection.</p>':'')+(progressCursor?'<button class="button small" id="progress-more-history">More history</button>':'');
 }catch(e){$('progress-history').textContent=e.message;}
}
function progressSettings(){
 const d=progressData();$('progress-detail-title').textContent='Monitoring settings';
 $('progress-detail-body').innerHTML='<form id="progress-settings-form">'+[['handoffMinutes','Handoff receipt attention after (minutes)'],['quietMinutes','No meaningful report attention after (minutes)'],['retentionDays','Observation history retention (days)']].map(([k,label])=>'<div class="field"><label for="progress-'+k+'">'+label+'</label><input id="progress-'+k+'" name="'+k+'" type="number" min="1" max="'+(k==='retentionDays'?3650:10080)+'" value="'+d.settings[k]+'" required></div>').join('')+'<p>These thresholds flag attention. They never reassign work or stop an agent.</p><button class="button primary" type="submit">Save monitoring settings</button></form>';
 detailDialog.showModal();const fid=activeId;
 $('progress-settings-form').onsubmit=async e=>{e.preventDefault();const values=Object.fromEntries(new FormData(e.target));for(const k in values)values[k]=Number(values[k]);try{await mutate(next=>{next.fleets.find(f=>f.id===fid).progress=values;});detailDialog.close();await loadProgress(true);}catch(error){toast(error.message,true);}};
}
async function progressClick(e){
 const b=e.target.closest('button');if(!b)return;
 try{
  if(b.dataset.progressTab){progressTab=b.dataset.progressTab;renderProgress();}
  if(b.dataset.progressAgent)await openProgressDetail({agentId:b.dataset.progressAgent});
  if(b.dataset.progressTask)await openProgressDetail({taskUrl:b.dataset.progressTask});
  if(b.dataset.progressHandoff)await openProgressDetail({handoffId:b.dataset.progressHandoff});
  if(b.dataset.reconcileHandoff)await reconcileHandoff(b.dataset.reconcileHandoff);
  if(b.dataset.progressRole)await openProgressDetail({roleId:b.dataset.progressRole});
  if(b.dataset.progressView){detailDialog.close();setView(b.dataset.progressView);}
  if(b.id==='progress-refresh')await loadProgress(true);
  if(b.id==='progress-settings')progressSettings();
  if(b.id==='progress-reporting')await previewWorkflow({kind:'reporting'});
  if(b.id==='progress-goal-policy')await previewWorkflow({kind:'bounded-goals'});
  if(b.dataset.goalLink)goalLinkForm(b.dataset.goalLink);
  if(b.dataset.goalAction)goalActionForm(b.dataset.goalAgent,b.dataset.goalAction);
  if(b.id==='progress-pause'){await api('/api/workflow/pause',{fleetId:activeId,paused:!progressData().deployment.paused});await loadProgress(true);}
  if(b.id==='progress-more-history')await openProgressDetail(progressDetail,true);
  if(b.dataset.progressEditRole){detailDialog.close();setView('roles');openEditor('role',b.dataset.progressEditRole);}
  if(b.dataset.progressEditLink){detailDialog.close();setView('board');openEditor('rolelink',b.dataset.progressEditLink);}
  if(b.dataset.progressActivity){detailDialog.close();activityAgent=b.dataset.progressActivity;setView('activity');loadActivity();}
  if(b.dataset.progressMessage){detailDialog.close();await showControl('message',[b.dataset.progressMessage],'Inspect the current task evidence and report the next authorized action. Preserve existing stops and ownership.');}
  if(b.dataset.reviewProgressHandoff){const p=await api('/api/triggers/review',{actionId:b.dataset.reviewProgressHandoff});detailDialog.close();await showControl('message',p.targets.map(t=>t.agentId),'',p);}
 }catch(error){toast(error.message,true);}
}
$('content').addEventListener('click',progressClick);detailDialog.addEventListener('click',progressClick);
setInterval(()=>{if(state&&!stopped&&view==='progress')loadProgress();},5000);
