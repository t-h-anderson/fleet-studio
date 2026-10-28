'use strict';
const backlogCache=new Map(),backlogRequests=new Set();
let backlogStage='open',backlogLabel='',backlogAgent='',backlogRole='',backlogReferenceRole='',backlogPage=0,backlogLimit=20;
let backlogView=localStorage.getItem('fleet-backlog-view')||'board';
const backlogData=()=>backlogCache.get(activeId);
function trackerSnapshotNotice(data,label='Tracker'){
 if(!data)return '';
 const state=data.error||data.stale?'stale':data.partial?'partial':'current';
 const last=data.observedAt?'Last successful '+label.toLowerCase()+' read: '+new Date(data.observedAt*1000).toLocaleString()+'.':'No successful '+label.toLowerCase()+' read yet; counts are unknown.';
 return [data.error||'',data.observedAt&&state==='stale'?'Retained '+label.toLowerCase()+' observations are stale.':'',data.partial?'Partial results; unseen records may have changed.':'',(!data.observedAt||state!=='current')?last:''].filter(Boolean).join(' ');
}
const backlogItems=()=>backlogData()?.items||[];
const stageName=id=>backlogData()?.stages?.find(s=>s.id===id)?.name||id;
const agentReferences=t=>[...new Set((t.references||[]).map(r=>r.agentId))];
function agentTicketBadge(id){
 const count=backlogItems().filter(t=>agentReferences(t).includes(id)).length;
 return '<button class="ticket-badge" data-agent-backlog="'+id+'" title="Tickets referenced by this agent; Tracker records ownership">'+icon('folder')+(backlogData()?.observedAt?count+' linked ticket'+(count===1?'':'s'):'Tickets')+'</button>';
}
function roleTicketBadge(id){
 const count=backlogItems().filter(t=>(t.roleIds||[]).includes(id)).length;
 const agentIds=current().agents.filter(a=>(a.roleIds||[]).includes(id)).map(a=>a.id),linked=backlogItems().filter(t=>agentReferences(t).some(a=>agentIds.includes(a))).length;
 return '<button class="ticket-badge" data-role-backlog="'+id+'" title="Queue placement from tracker labels">'+icon('folder')+(backlogData()?.observedAt?count+' in queue':'Backlog')+'</button>'+(linked?'<button class="ticket-badge" data-role-references="'+id+'" title="Tickets referenced by agents in this role">'+linked+' linked</button>':'');
}
function triggerContractMarkup(t){
 const link=current().roleLinks.find(l=>l.id===t.workflowLinkId),sources=current().agents.filter(a=>(a.roleIds||[]).includes(t.sourceRoleId)),targets=current().agents.filter(a=>(a.roleIds||[]).includes(t.targetRoleId));
 return '<div class="trigger-contract">'+(t.kind==='event'?'<p>Event: <code>'+esc(t.eventName)+'</code></p>':'')+'<p>'+sources.length+' upstream agent(s) &middot; '+targets.length+' target agent(s)</p>'+(t.workflowLinkId?'<p>'+(link?'From workflow: <button class="text-button" data-edit-role-link="'+link.id+'">'+esc(link.label)+'</button>':'Source relationship removed.')+'</p>':'')+(monitor.workflowProblems?.[t.id]?'<p class="contract-changed">'+esc(monitor.workflowProblems[t.id])+' Remove this draft and populate it again.</p>':'')+'<button class="text-button" data-role-backlog="'+t.targetRoleId+'">Target backlog</button>'+(t.kind==='event'?'<button class="text-button" data-trigger-event="'+t.id+'">Reporting instructions</button>':'')+'</div>';
}
async function loadBacklog(force=false){
 if(!state||stopped)return;
 const fleetId=activeId;
 if(backlogRequests.has(fleetId))return;
 backlogRequests.add(fleetId);
 try{
  const data=await api(force?'/api/backlog/refresh':'/api/backlog?fleetId='+encodeURIComponent(fleetId),force?{fleetId}:undefined);
  backlogCache.set(fleetId,data);
  if(activeId===fleetId&&!drag&&!roleDrag)renderContent();
  if(data.loading)setTimeout(()=>{if(activeId===fleetId)loadBacklog();},2000);
 }catch(e){if(force)toast(e.message,true);}
 finally{backlogRequests.delete(fleetId);}
}
function ticketCard(t){
 const refs=agentReferences(t).map(id=>current().agents.find(a=>a.id===id)?.name).filter(Boolean);
 return '<button class="work-ticket" data-ticket="'+t.iid+'"><span class="ticket-number">#'+t.iid+'</span><strong>'+esc(t.title)+'</strong><span class="ticket-meta">'+(refs.length?'Referenced by '+esc(refs.join(', ')):t.assignees.length?'Tracker: '+esc(t.assignees.join(', ')):'Unassigned in tracker')+'</span></button>';
}
function selectedTickets(){
 return backlogItems().filter(t=>(backlogStage==='all'||backlogStage==='open'&&t.state!=='closed'||t.stage===backlogStage)&&(!backlogLabel||t.labels.includes(backlogLabel))&&(!backlogAgent||agentReferences(t).includes(backlogAgent))&&(!backlogRole||(t.roleIds||[]).includes(backlogRole))&&(!backlogReferenceRole||agentReferences(t).some(id=>current().agents.some(a=>a.id===id&&(a.roleIds||[]).includes(backlogReferenceRole))))&&(t.title+' #'+t.iid+' '+t.labels.join(' ')).toLowerCase().includes(query.toLowerCase()));
}
function backlogSummary(){
 const data=backlogData();
 if(!data?.config.projectUrl)return '<div class="backlog-strip"><span>Connect a GitLab or GitHub project to see tickets across the fleet.</span><button class="text-button" data-backlog-settings>Connect backlog</button></div>';
 const counts=Object.fromEntries((data.stages||[]).map(s=>[s.id,backlogItems().filter(t=>t.stage===s.id).length]));
 return '<div class="backlog-strip"><strong>Ticket flow</strong>'+(data.stages||[]).map(s=>'<button class="flow-chip '+s.id+'" data-backlog-stage="'+s.id+'">'+esc(s.name)+' <b>'+(data.observedAt?counts[s.id]:'?')+'</b></button>').join('')+'<span class="sync-note">'+(data.loading?'Refreshing tracker…':data.error?'Tracker unavailable':data.observedAt?(data.stale?'Last snapshot · ':'Tracker · ')+ago(data.observedAt):'Loading tracker…')+'</span></div>';
}
function renderBacklog(){
 const data=backlogData(),items=selectedTickets();
 $('visible-count').textContent=items.length;
 const labels=[...new Set(backlogItems().flatMap(t=>t.labels))].sort();
 const settings=current().backlog||data?.config||{};
 const role=current().roles.find(r=>r.id===(backlogRole||backlogReferenceRole));
 const toolbar='<div class="definition-toolbar"><div><strong>Tickets moving through the project</strong><p>Live tracker labels define queue stage. Agent links are display references.</p></div><div><button class="button small" data-backlog-settings>Project & links</button><button class="button small" id="refresh-backlog">'+icon('refresh')+'Refresh tickets</button></div></div>';
 const filters='<div class="backlog-filters"><label>Stage<select id="backlog-stage"><option value="open">All open</option><option value="all">All tickets</option>'+(data?.stages||[]).map(s=>'<option value="'+s.id+'">'+esc(s.name)+'</option>').join('')+'</select></label><label>Label<select id="backlog-label"><option value="">All labels</option>'+labels.map(l=>'<option value="'+esc(l)+'">'+esc(l)+'</option>').join('')+'</select></label><label>Agent reference<select id="backlog-agent"><option value="">All agents</option>'+current().agents.map(a=>'<option value="'+a.id+'">'+esc(a.name)+'</option>').join('')+'</select></label><div class="backlog-view-switch"><button class="button small '+(backlogView==='board'?'primary':'')+'" data-backlog-view="board">Flow board</button><button class="button small '+(backlogView==='table'?'primary':'')+'" data-backlog-view="table">Table</button></div>'+(role?'<button class="flow-chip" id="clear-backlog-role">'+esc(role.name)+(backlogReferenceRole?' references':' queue')+' &times;</button>':'')+'</div>';
 const snapshotNotice=trackerSnapshotNotice(data);
 const notice=snapshotNotice?'<div class="backlog-notice">'+esc(snapshotNotice)+'</div>':'';
 let body='';
 if(!data?.observedAt)body='<div class="activity-empty backlog-empty">'+(!settings.projectUrl?'Connect the project in Project & links.':data?.loading?'Reading project tickets…':data?.error?'Refresh after reconnecting to the tracker.':'Waiting for the first tracker snapshot.')+'</div>';
 else if(backlogView==='board')body='<div class="kanban">'+data.stages.filter(s=>backlogStage==='open'?s.id!=='closed':backlogStage==='all'||s.id===backlogStage).map(s=>{const tickets=items.filter(t=>t.stage===s.id);return '<section class="kanban-lane '+s.id+'"><div class="kanban-heading"><strong>'+esc(s.name)+'</strong><span>'+tickets.length+'</span></div>'+tickets.slice(0,backlogLimit).map(ticketCard).join('')+(tickets.length>backlogLimit?'<button class="text-button" data-more-tickets>Show more</button>':'')+(!tickets.length?'<p class="field-hint">No matching tickets</p>':'')+'</section>';}).join('')+'</div>';
 else{
  backlogPage=Math.min(backlogPage,Math.max(0,Math.ceil(items.length/40)-1));
  body='<div class="table-wrap"><table class="backlog-table"><thead><tr><th>TICKET</th><th>STAGE</th><th>LABELS</th><th>REFERENCES / ASSIGNEES</th><th>UPDATED</th></tr></thead><tbody>'+items.slice(backlogPage*40,backlogPage*40+40).map(t=>'<tr><td><button class="ticket-title" data-ticket="'+t.iid+'">#'+t.iid+' '+esc(t.title)+'</button></td><td><span class="stage-tag '+t.stage+'">'+esc(stageName(t.stage))+'</span></td><td>'+t.labels.map(l=>'<span class="small-label">'+esc(l)+'</span>').join('')+'</td><td>'+agentReferences(t).map(id=>esc(current().agents.find(a=>a.id===id)?.name||'')).join(', ')+(t.assignees.length?'<span class="field-hint">Tracker: '+esc(t.assignees.join(', '))+'</span>':'')+'</td><td>'+esc(new Date(t.updatedAt).toLocaleDateString())+'</td></tr>').join('')+'</tbody></table></div><div class="backlog-pagination"><button class="button small" id="backlog-prev" '+(!backlogPage?'disabled':'')+'>Previous</button><span>'+items.length+' tickets &middot; page '+(backlogPage+1)+' of '+Math.max(1,Math.ceil(items.length/40))+'</span><button class="button small" id="backlog-next" '+((backlogPage+1)*40>=items.length?'disabled':'')+'>Next</button></div>';
 }
 const changes=data?.changes||[];
 $('content').innerHTML=toolbar+backlogSummary()+filters+notice+body+(changes.length?'<div class="backlog-changes"><h3>Movement observed while Studio is running</h3>'+changes.slice(0,8).map(c=>'<button class="text-button" data-ticket="'+c.iid+'">#'+c.iid+' '+esc(stageName(c.from))+' &rarr; '+esc(stageName(c.to))+'</button>').join('')+'</div>':'');
 $('backlog-stage').value=backlogStage;$('backlog-label').value=backlogLabel;$('backlog-agent').value=backlogAgent;
}
function openBacklog(filter={}){
 backlogRole=filter.role||'';backlogReferenceRole=filter.referenceRole||'';backlogAgent=filter.agent||'';backlogStage=filter.stage||'all';backlogLabel='';backlogPage=0;
 setView('backlog');
}
function backlogSettings(){
 const cfg=current().backlog||backlogData()?.config||{projectUrl:current().roleSource?.url.split('/-/')[0]||'',scope:'all',agentTickets:{}};
 $('backlog-dialog-title').textContent='Backlog project & agent links';
 $('backlog-dialog-body').innerHTML='<form id="backlog-settings-form"><div class="field"><label for="backlog-project-url">Project URL</label><input id="backlog-project-url" type="url" value="'+esc(cfg.projectUrl)+'" placeholder="https://gitlab.example.com/group/project or https://github.com/owner/repo"></div><div class="field"><label for="backlog-provider">Tracker</label><select id="backlog-provider"><option value="auto">Detect from URL</option><option value="gitlab">GitLab</option><option value="github">GitHub</option></select></div><div class="field"><label for="backlog-scope">Project scope</label><select id="backlog-scope"><option value="all">All project tickets</option><option value="ai">AI workflow tickets</option></select></div><p class="field-hint">Uses your existing glab or gh login. Reads tickets, label history and CI from the tracker. Self-hosted GitHub needs the GitHub tracker selected.</p><h3>Additional ticket links</h3><p class="field-hint">Link ticket numbers to agents for display. This does not change tracker assignment. Existing local session-status references also appear.</p>'+current().agents.map(a=>'<div class="field"><label for="tickets-'+a.id+'">'+esc(a.name)+'</label><input id="tickets-'+a.id+'" data-agent-ticket-input="'+a.id+'" value="'+esc((cfg.agentTickets?.[a.id]||[]).join(', '))+'" placeholder="1144, 1008"></div>').join('')+'<button class="button primary" type="submit">Save backlog settings</button></form>';
 $('backlog-scope').value=cfg.scope;$('backlog-provider').value=cfg.provider||'auto';
 $('backlog-provider').closest('.field').insertAdjacentHTML('afterend','<div class="field"><label><input id="backlog-recover-proxy" type="checkbox"> Retry tracker reads without an unavailable local proxy</label><p class="field-hint">Optional. Retry once when an unauthenticated loopback proxy is unreachable. Other configured proxies remain in effect. Applies to tickets, history and CI.</p></div>');
 $('backlog-recover-proxy').checked=cfg.recoverLocalProxy===true;
 const fleetId=activeId;
 $('backlog-settings-form').onsubmit=async e=>{e.preventDefault();try{
  const agentTickets={};document.querySelectorAll('[data-agent-ticket-input]').forEach(n=>{const text=n.value.trim();const ids=text?text.split(/[,\s]+/).map(v=>Number(v.replace(/^#/,''))):[];if(ids.some(i=>!Number.isInteger(i)||i<=0))throw new Error('Use positive ticket numbers separated by commas.');agentTickets[n.dataset.agentTicketInput]=ids;});
  // Keep settings this form does not edit, such as legacy status references.
  const backlog={...cfg,projectUrl:$('backlog-project-url').value.trim(),scope:$('backlog-scope').value,agentTickets};
  if($('backlog-recover-proxy').checked||'recoverLocalProxy' in cfg)backlog.recoverLocalProxy=$('backlog-recover-proxy').checked;
  if($('backlog-provider').value==='auto')delete backlog.provider;else backlog.provider=$('backlog-provider').value;
  await mutate(next=>next.fleets.find(f=>f.id===fleetId).backlog=backlog,'Backlog settings saved.');
  backlogCache.delete(fleetId);$('backlog-dialog').close();loadBacklog(true);
 }catch(error){toast(error.message,true);}};
 $('backlog-dialog').showModal();
}
async function showTicket(iid){
 const t=backlogItems().find(t=>t.iid===iid);if(!t)return;
 const fleetId=activeId;
 $('backlog-dialog-title').textContent='#'+t.iid+' '+t.title;
 $('backlog-dialog-body').innerHTML='<div class="ticket-details"><span class="stage-tag '+t.stage+'">'+esc(stageName(t.stage))+'</span><a class="button small" href="'+esc(t.url)+'" target="_blank" rel="noreferrer">Open in tracker</a><p>'+t.labels.map(l=>'<span class="small-label">'+esc(l)+'</span>').join('')+'</p><p>Tracker assignees: '+esc(t.assignees.join(', ')||'None')+'</p>'+(t.references||[]).map(r=>'<p>'+esc(current().agents.find(a=>a.id===r.agentId)?.name||'')+' &middot; '+esc(r.basis)+(r.observedAt?' &middot; '+esc(new Date(r.observedAt*1000).toLocaleString()):'')+'</p>').join('')+'<h3>Workflow label history</h3><div id="ticket-history">Loading tracker history…</div></div>';
 $('backlog-dialog').dataset.ticket=String(iid);$('backlog-dialog').showModal();
 try{const history=await api('/api/backlog/history',{fleetId,iid});if($('backlog-dialog').dataset.ticket!==String(iid)||!$('ticket-history'))return;
 $('ticket-history').innerHTML='<p class="field-hint">'+esc(history.notice)+'</p>'+history.events.map(h=>'<p>'+esc(new Date(h.time).toLocaleString())+' &middot; '+esc(h.action)+' <strong>'+esc(h.label)+'</strong></p>').join('')+(!history.events.length?'<p class="field-hint">No workflow-label events returned.</p>':'');
 }catch(e){if($('ticket-history'))$('ticket-history').textContent=e.message;}
}
const beforeBacklogRenderAll=renderAll;
renderAll=function(){
 beforeBacklogRenderAll();if(!state)return;
 if(view==='backlog'){
  $('fleet-title').textContent='Project backlog';$('fleet-description').textContent='Work, references and handoffs across the fleet.';
  $('search').placeholder='Find a ticket…';$('status-filter').classList.add('hidden');$('workspace-layout').classList.add('board-layout');
  $('panel-footer-text').textContent='Tracker is the source of ticket state. Backlog refreshes every 90 seconds.';
 }
 if(!backlogCache.has(activeId)&&!backlogRequests.has(activeId))loadBacklog();
};
const beforeBacklogRenderContent=renderContent;
renderContent=function(){
 beforeBacklogRenderContent();if(!state||drag||roleDrag)return;
 if(['roster','roles','board'].includes(view))$('content').insertAdjacentHTML('beforeend',backlogSummary());
 if(['roles','board'].includes(view)){
  const unmapped=current().agents.filter(a=>!(a.roleIds||[]).length);
  if(unmapped.length)$('content').insertAdjacentHTML('beforeend','<div class="unmapped-agents"><strong>Agents without shared roles</strong><p>'+unmapped.map(a=>'<button class="text-button" data-edit="'+a.id+'">'+esc(a.name)+' ('+esc(a.role)+')</button>').join(' ')+'</p><button class="button small" id="populate-agent-roles">Create roles from roster labels</button></div>');
 }
};
$('close-backlog-dialog').onclick=()=>$('backlog-dialog').close();
$('content').addEventListener('change',e=>{
 if(e.target.id==='backlog-stage')backlogStage=e.target.value;
 else if(e.target.id==='backlog-label')backlogLabel=e.target.value;
 else if(e.target.id==='backlog-agent')backlogAgent=e.target.value;
 else return;backlogPage=0;renderContent();
});
$('content').addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 if(b.dataset.agentBacklog)openBacklog({agent:b.dataset.agentBacklog});
 if(b.dataset.roleBacklog)openBacklog({role:b.dataset.roleBacklog});
 if(b.dataset.roleReferences)openBacklog({referenceRole:b.dataset.roleReferences});
 if(b.dataset.backlogStage)openBacklog({stage:b.dataset.backlogStage});
 if(b.dataset.backlogView){backlogView=b.dataset.backlogView;localStorage.setItem('fleet-backlog-view',backlogView);renderContent();}
 if(b.dataset.ticket)showTicket(Number(b.dataset.ticket));
 if(b.hasAttribute('data-backlog-settings'))backlogSettings();
 if(b.id==='refresh-backlog')loadBacklog(true);
 if(b.id==='backlog-prev'){backlogPage--;renderContent();}
 if(b.id==='backlog-next'){backlogPage++;renderContent();}
 if(b.id==='clear-backlog-role'){backlogRole='';backlogReferenceRole='';renderContent();}
 if(b.hasAttribute('data-more-tickets')){backlogLimit+=20;renderContent();}
 if(b.id==='populate-agent-roles'){
  b.disabled=true;try{const data=await api('/api/workflow/roles',{fleetId:activeId,revision:state.revision});state=data.state;renderAll();toast(data.report.roles.length+' local role definition(s) added.');}
  catch(error){toast(error.message,true);}finally{b.disabled=false;}
 }
 if(b.dataset.triggerEvent){
  const rule=current().triggers.find(t=>t.id===b.dataset.triggerEvent),sources=current().agents.filter(a=>(a.roleIds||[]).includes(rule.sourceRoleId));
  $('backlog-dialog-title').textContent='Report '+rule.name;
  const command='python fleet_event.py --fleet '+activeId+' --agent "'+(sources[0]?.name||'SOURCE AGENT')+'" --event "'+rule.eventName+'" --id "ticket-NUMBER-reviewed-SHA" --to "RECIPIENT" --ticket "TICKET-OR-COMMENT-URL" --artifact "EXACT-HEAD-AND-EVIDENCE" --context "BOUNDED-NEXT-ACTION"';
  $('backlog-dialog-body').innerHTML='<p>After the upstream agent verifies this handoff, it reports the event using a stable ticket-and-revision ID.</p><pre>'+esc(command)+'</pre><p class="field-hint">Run from the Fleet Studio folder. Use the actual recipient name or ID and current ticket, artifact and next action. Reusing the same event ID and details prevents duplicate sends; held handoffs retry automatically. '+(!sources.length?'Assign an upstream agent before using this trigger. ':'')+(rule.enabled?'This enabled trigger uses '+esc(rule.mode)+' delivery.':'This trigger is disabled until you enable it.')+'</p>';
  $('backlog-dialog').showModal();
 }
});
setInterval(()=>{if(state&&!stopped)loadBacklog();},30000);
