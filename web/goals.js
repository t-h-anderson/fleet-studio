'use strict';
function goalCard(g){
 const n=g.native,b=g.blocker;
 return '<article class="progress-report"><strong>'+esc(g.agentName)+' · '+esc(n.status)+'</strong><p>'+esc(n.objective)+'</p><small>Goal ID: '+esc(n.id)+'</small><p>'+(g.current?'Current native goal':!g.bindingCurrent?'Earlier agent assignment':!g.observationFresh?'Native observation unavailable or stale':'Goal replaced or removed; completion is not established')+(g.staleGoal?' · Needs evidence review':'')+'</p>'+evidenceLink(g.taskUrl,'Linked ticket')+'<p>Done when: '+esc(g.completionCriteria||'Not linked yet')+'</p>'+(b?'<p>Blocker: '+esc(b.blockerCategory)+' · '+esc(b.reason)+'</p><p>Owner: '+esc(b.blockerOwner)+' · Resume when: '+esc(b.resumeCondition)+'</p><p>Next: '+esc(b.nextAction)+'</p>'+(b.dependsOn||[]).map(u=>evidenceLink(u,'Dependency')).join(' ')+(b.resolvedAt?'<p>Dependency resolution reported; native goal state remains separate.</p>'+evidenceLink(b.resolutionEvidence,'Resolution evidence'):''):'')+'<p>Native usage: '+esc(n.tokensUsed??'Unknown')+' tokens · Budget: '+esc(n.tokenBudget??'Not set')+'</p>'+stamp('Last meaningful goal report',g.lastReportAt)+evidenceLink(g.evidenceUrl)+(g.current&&n.status!=='complete'?'<div class="progress-actions"><button class="button small" data-goal-link="'+g.agentId+'">Link goal / review criterion</button>'+['complete','cancel',...(['blocked','paused'].includes(n.status)?['resume']:[])].map(action=>'<button class="button small" data-goal-agent="'+g.agentId+'" data-goal-action="'+action+'">Review '+action+'</button>').join('')+'</div>':'')+'</article>';
}
function agentGoalMarkup(d,agent){
 const a=d.agents.find(a=>a.id===agent),rows=(d.goals?.records||[]).filter(g=>g.agentId===agent);
 const current=rows.filter(g=>g.current);
 return '<h3>'+esc(a?.name||'Agent')+': Bounded goals</h3>'+current.map(goalCard).join('')+(!current.length&&a?.session.goal?.id?'<p>'+esc(a.session.goal.objective)+'</p><button class="button small" data-goal-link="'+agent+'">Link current goal</button>':'')+(!current.length&&!a?.session.goal?.id?'<p>'+(a?.session.goalKnown&&!a.session.stale?'No native goal set.':'Native goal observation unavailable or stale.')+'</p>':'')+(rows.some(g=>!g.current)?'<details><summary>Earlier goal observations</summary>'+rows.filter(g=>!g.current).map(goalCard).join('')+'</details>':'');
}
function goalsMarkup(d){
 const records=d.goals?.records||[],actions=d.goals?.actions||[];
 return '<div class="goal-view"><p>Goals describe bounded work packets. Roles continue in the workflow. Stops remain until an authorized decision; age only flags a review.</p>'+(!d.deployment.boundedGoalsEnabled&&d.deployment.active?'<button class="button small" id="progress-goal-policy">Review bounded-goal policy</button>':'')+d.agents.map(a=>agentGoalMarkup(d,a.id)).join('')+(!records.length?'<p>No native goals have been observed yet. Do not create goals merely to populate monitoring.</p>':'')+'<h3>Reviewed action history</h3>'+actions.map(r=>'<article class="progress-report"><strong>'+esc(r.agentName)+' · '+esc(r.action)+' · '+esc(r.status)+'</strong><p>'+esc(r.goal.objective)+'</p><p>'+esc(r.note)+'</p><p>'+esc(r.detail||'Awaiting acknowledgement')+'</p>'+evidenceLink(r.evidenceUrl,'Decision evidence')+stamp('Operator decision',r.startedAt)+'</article>').join('')+(!actions.length?'<p>No goal actions recorded.</p>':'')+'</div>';
}
function goalContext(agent){
 const d=progressData(),a=d.agents.find(a=>a.id===agent),r=d.goals?.records.find(g=>g.agentId===agent&&g.current),n=a?.session.goal;
 if(!n?.id)throw Error('Refresh to observe the current goal identity.');
 return {d,a,r,n};
}
function goalField(name,label,value='',textarea=false){
 return '<div class="field"><label for="goal-'+name+'">'+label+'</label>'+(textarea?'<textarea required maxlength="1800" id="goal-'+name+'" name="'+name+'">'+esc(value)+'</textarea>':'<input required id="goal-'+name+'" name="'+name+'" type="url" value="'+esc(value)+'">')+'</div>';
}
function goalLinkForm(agent){
 const {d,a,r,n}=goalContext(agent),fid=activeId;
 $('progress-detail-title').textContent='Link '+a.name+' goal';
 $('progress-detail-body').innerHTML='<p>'+esc(n.objective)+'</p><p>This records an operator link and completion criterion. Native goal state is unchanged.</p><form id="goal-link-form">'+goalField('taskUrl','Tracker ticket',r?.taskUrl)+goalField('evidenceUrl','Evidence supporting this work packet',r?.evidenceUrl)+goalField('completionCriteria','What exactly completes this goal?',r?.completionCriteria,true)+'<button class="button primary" type="submit">Save goal link</button></form>';
 if(!detailDialog.open)detailDialog.showModal();
 $('goal-link-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{
  await api('/api/goals/link',{fleetId:fid,agentId:agent,sessionId:a.session.sessionId,releaseId:d.deployment.active.id,goalId:n.id,...Object.fromEntries(new FormData(e.target))});
  detailDialog.close();await loadProgress(true);toast('Goal evidence linked.');
 }catch(error){toast(error.message,true);}finally{button.disabled=false;}};
}
function goalActionForm(agent,action){
 const {a,r,n}=goalContext(agent),fid=activeId;
 const confirm={complete:'I verified that this goal’s completion criterion is met.',cancel:'I have authority to cancel this work packet; cancellation is not completion.',resume:'I verified the resume condition and have authority to resume; explicit stops and restrictions are resolved.'}[action];
 $('progress-detail-title').textContent='Review '+action+' · '+a.name;
 $('progress-detail-body').innerHTML='<p>'+esc(n.objective)+'</p><p>Done when: '+esc(r?.completionCriteria||'Link a completion criterion first')+'</p>'+(r?.blocker?'<p>Blocker owner: '+esc(r.blocker.blockerOwner)+' · Resume condition: '+esc(r.blocker.resumeCondition)+'</p>':'')+'<p>Close this agent session at a safe boundary and empty its queue before preview. Keep it closed until apply finishes.</p><form id="goal-action-form">'+goalField('evidenceUrl','Decision evidence on the linked ticket',r?.evidenceUrl)+goalField('note','Decision, supporting evidence and authority','',true)+'<div class="field"><label><input type="checkbox" name="confirmed" required> '+esc(confirm)+'</label></div><button class="button primary" type="submit">Preview goal action</button></form>';
 if(!detailDialog.open)detailDialog.showModal();
 $('goal-action-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{
  const values=Object.fromEntries(new FormData(e.target));values.confirmed=values.confirmed==='on';
  const p=await api('/api/goals/preview',{fleetId:fid,agentId:agent,action,...values});
  $('progress-detail-body').innerHTML='<p><strong>'+esc(p.effect)+'</strong></p><p>'+esc(p.goal.objective)+'</p><p>'+esc(p.note)+'</p>'+evidenceLink(p.evidenceUrl,'Decision evidence')+'<p>'+esc(p.caution)+'</p><button class="button primary" id="goal-apply">Apply reviewed '+esc(action)+'</button>';
  $('goal-apply').onclick=async e=>{e.target.disabled=true;try{const result=await api('/api/goals/apply',{previewId:p.id});$('progress-detail-body').innerHTML='<p><strong>'+esc(result.status)+'</strong></p><p>'+esc(result.detail)+'</p>';await loadProgress(true);}catch(error){toast(error.message,true);/* Outcome may be uncertain: a fresh review is required. */}};
 }catch(error){toast(error.message,true);button.disabled=false;}};
}
