'use strict';
let boardMode=localStorage.getItem('fleet-board-mode')||'roles',roleDrag=null,controlPreview=null,policyDraft=null;
if(boardMode==='roles')zoom=.8;
let groupRoleHandoffs=localStorage.getItem('fleet-group-handoffs')!=='false',focusedRoleId='',selectedRolePairId='',roleBoardContext='';
let showInternalRoles=localStorage.getItem('fleet-internal-roles')==='true';
const boardRoles=roles=>FleetLayout.visibleRoles(roles,showInternalRoles);
const roleFields=['name','responsibilities','authority'];
const linkedRoles=(agent)=>current().roles.filter(r=>(agent.roleIds||[]).includes(r.id));
const assignedAgents=(role)=>current().agents.filter(a=>(a.roleIds||[]).includes(role.id));
const roleDraft=(role)=>!role.baseline||roleFields.some(k=>role[k]!==role.baseline[k]);
function roleBadge(role){if(current().workflow)return '<span class="role-state draft">Workflow definition</span>';return '<span class="role-state '+(roleDraft(role)?'draft':'')+'">'+(role.baseline?(roleDraft(role)?'Policy draft':'Linked policy'):'Local definition')+'</span>';}
function roleChoices(selectedIds=[]){return '<div class="role-choices">'+current().roles.map(r=>'<label class="checkbox-field"><input type="checkbox" data-role-choice="'+r.id+'" '+(selectedIds.includes(r.id)?'checked':'')+'>'+esc(r.name)+'</label>').join('')+'</div>';}
function renderRoles(){
 const f=current(),roles=f.roles.filter(r=>(r.name+' '+r.responsibilities).toLowerCase().includes(query.toLowerCase()));
 $('visible-count').textContent=roles.length;
 $('content').innerHTML='<div class="definition-toolbar"><div><strong>Define once. Assign to agents.</strong><p>Shared responsibilities live here. Agent notes add individual scope.</p></div><div><button class="button small" id="new-role">'+icon('plus')+'New role</button>'+(f.roleSource&&!f.workflow?'<button class="button small" id="refresh-roles">'+icon('refresh')+'Refresh policy</button><button class="button small" id="policy-proposal">Policy proposal</button>':'')+'</div></div>'+
 (f.roleSource&&!f.workflow?'<div class="policy-reference"><a href="'+esc(f.roleSource.url)+'" target="_blank" rel="noreferrer">Roles and responsibilities</a><span>Committed '+esc(f.roleSource.commit.slice(0,9))+' · edits remain drafts until published and refreshed</span></div>':'')+
 '<div class="roles-grid">'+roles.map(r=>'<article class="role-card"><div class="role-card-heading"><h3>'+esc(r.name)+'</h3>'+roleBadge(r)+'</div>'+(r.goal?'<p class="role-goal">'+esc(r.goal)+'</p>':'<p class="role-goal muted">No goal yet. Edit the role to say what outcome it exists for.</p>')+'<p>'+esc(r.responsibilities||'Add the shared responsibilities for this role.')+'</p><details><summary>Authority and handoff</summary><p>'+esc(r.authority||'Not defined yet.')+'</p></details><div class="assigned-chips">'+(assignedAgents(r).map(a=>'<span>'+esc(a.name)+'</span>').join('')||'<span class="muted">No agents assigned</span>')+'</div><div class="role-ticket-summary">'+roleTicketBadge(r.id)+'</div><div class="role-card-actions"><button class="button small" data-edit-role="'+r.id+'">Edit / assign</button><button class="button small" data-apply-role="'+r.id+'" '+(!assignedAgents(r).some(a=>a.sessionId)||roleDraft(r)&&!!f.roleSource&&!f.workflow?'disabled':'')+'>Send assignments</button></div></article>').join('')+'</div>';
}
function renderTriggers(){
 const f=current(),rules=(f.triggers||[]).filter(t=>(t.name+' '+t.message).toLowerCase().includes(query.toLowerCase()));
 const roleName=(id)=>f.roles.find(r=>r.id===id)?.name||'Unassigned';
 $('visible-count').textContent=rules.length;
 const automation=monitor.automation||{actions:[],states:{}};
 $('content').innerHTML='<div class="definition-toolbar"><div><strong>Events, conditions and timers</strong><p>Enabled automatic triggers queue instructions in existing sessions. Managed workflows use the deployed version; edits below are drafts until redeployed.</p></div><div><button class="button small" id="populate-triggers">Populate from workflow</button><button class="button small" id="new-trigger">'+icon('plus')+'New trigger</button></div></div>'+
 '<div class="trigger-list">'+(rules.length?rules.map(t=>{
 const schedule=automation.states[t.id]?.nextDue;
 return '<article class="trigger-card"><div><h3>'+esc(t.name)+'</h3><span class="role-state '+(!t.enabled?'draft':'')+'">'+(t.enabled?'Enabled · '+t.mode:'Disabled')+'</span></div><p><strong>'+esc({event:'Upstream event',condition:'Observed condition',timer:'Timer',idle:'Recurring idle check'}[t.kind])+'</strong> · '+esc(['timer','idle'].includes(t.kind)?'Every '+t.minutes+' minutes'+(t.kind==='idle'?' while idle':''):roleName(t.sourceRoleId)+' · '+(t.kind==='event'?t.eventName:t.condition))+' → '+esc(roleName(t.targetRoleId))+'</p><p>'+esc(t.message)+'</p>'+(['timer','idle'].includes(t.kind)&&t.enabled&&schedule?'<p class="field-hint">Next due '+esc(new Date(schedule*1000).toLocaleString())+'</p>':'')+(t.kind==='idle'?Object.entries(automation.states[t.id]?.agents||{}).map(([id,s])=>'<p class="field-hint">'+esc(f.agents.find(a=>a.id===id)?.name||id)+': '+esc(s.detail||(s.nextDue>Date.now()/1000?'Next check '+new Date(s.nextDue*1000).toLocaleTimeString():'Waiting for a fresh idle observation'))+'</p>').join(''):'')+(t.kind==='event'?'<p class="field-hint">'+(t.recipientMode==='all'?'Role broadcast; an explicit recipient can narrow it.':'One recipient; --to is required when several agents share this role.')+'</p>':'')+triggerContractMarkup(t)+'<button class="button small" data-edit-trigger="'+t.id+'">Edit trigger</button></article>';
 }).join(''):'<div class="activity-empty">Create a trigger and choose when it should send.</div>')+'</div>'+
 '<div class="actions-section"><h3>Handoffs and recent trigger actions</h3>'+(automation.actions.filter(a=>a.fleetId===activeId).map(a=>'<div class="action-row"><div><strong>'+esc(a.name)+'</strong><p>'+esc(a.status)+' · '+esc(a.cause)+(a.targetName?' &rarr; '+esc(a.targetName):'')+'</p><p>'+esc(a.ticket||'')+'</p><p>'+esc(a.detail||'')+'</p></div>'+(['pending','held','needs-attention'].includes(a.status)?'<button class="button small" data-review-trigger="'+a.id+'">Review / send</button><button class="text-button" data-dismiss-trigger="'+a.id+'">Dismiss</button>':'')+'</div>').join('')||'<p class="field-hint">No trigger has fired yet. The server runs triggers while the browser is closed. Held handoffs retry automatically; idle checks skip busy or stopped agents.</p>')+
 '<h3>Recent agent controls</h3>'+((monitor.controls||[]).filter(c=>c.fleetId===activeId).slice(-10).reverse().map(c=>'<div class="action-row"><div><strong>'+esc(c.action)+' · '+esc(c.status)+'</strong>'+c.targets.map(t=>'<p>'+esc(t.name)+': '+esc(t.status)+' — '+esc(t.detail||'')+'</p>').join('')+'</div></div>').join('')||'<p class="field-hint">No instructions have been sent from Fleet Studio.</p>')+'</div>';
}
const originalRenderAll=renderAll;
renderAll=function(){
 originalRenderAll();
 if(!state)return;
 $('board-mode').value=boardMode;
 $('show-internal-roles').checked=showInternalRoles;
 $('group-role-handoffs').checked=groupRoleHandoffs;
 $('group-handoffs-control').classList.toggle('hidden',boardMode!=='roles');
 $('internal-roles-control').classList.toggle('hidden',boardMode!=='roles');
 if(view==='roles'||view==='triggers'){
  $('fleet-title').textContent=view==='roles'?'Roles':'Triggers & actions';
  $('fleet-description').textContent=view==='roles'?'Shared definitions, project policy and agent assignments.':'Control when work moves through your fleet.';
  $('search').placeholder=view==='roles'?'Find a role…':'Find a trigger…';
  $('panel-footer-text').textContent=view==='roles'?'Role edits are saved as drafts. Managed workflow changes take effect after deployment.':'Automatic sends run only while Fleet Studio is running.';
  $('workspace-layout').classList.add('board-layout');
 }
};
const originalRenderBoard=renderBoard;
function roleBoardGraph(roles,links){return {agents:roles,links:groupRoleHandoffs?FleetLayout.bundles(roles,links):links};}
function roleInFocus(id){
 if(selectedRolePairId){const pair=FleetLayout.bundles(current().roles,current().roleLinks).find(p=>p.id===selectedRolePairId);return !pair||[pair.source,pair.target].includes(id);}
 return !focusedRoleId||id===focusedRoleId||current().roleLinks.some(l=>(l.source===focusedRoleId&&l.target===id)||(l.target===focusedRoleId&&l.source===id));
}
renderBoard=function(){
 if(boardMode==='agents'){focusedRoleId='';selectedRolePairId='';roleBoardContext='';return originalRenderBoard();}
 if(roleDrag)return;
 const activeElement=document.activeElement,focusAttribute=['data-focus-role','data-role-node','data-inspect-role-pair'].find(attr=>activeElement?.hasAttribute(attr));
 const focusValue=focusAttribute?activeElement.getAttribute(focusAttribute):null;
 const f=current(),roles=boardRoles(f.roles).filter(r=>(r.name+' '+r.responsibilities+' '+assignedAgents(r).map(a=>a.name).join(' ')).toLowerCase().includes(query.toLowerCase()));
 if(roleBoardContext!==activeId){focusedRoleId='';selectedRolePairId='';roleBoardContext=activeId;}
 if(focusedRoleId&&!roles.some(r=>r.id===focusedRoleId))focusedRoleId='';
 const graph=roleBoardGraph(roles,f.roleLinks),geometry=workflowGeometry(graph);
 if(selectedRolePairId&&!graph.links.some(l=>l.id===selectedRolePairId))selectedRolePairId='';
 $('visible-count').textContent=roles.length;
 const width=geometry.width,height=geometry.height;
 const old=$('board-viewport'),left=old?.scrollLeft||0,top=old?.scrollTop||0;
 const count=f.roleLinks.filter(l=>roles.some(r=>r.id===l.source)&&roles.some(r=>r.id===l.target)).length;
 const focused=roles.find(r=>r.id===focusedRoleId);
 $('content').innerHTML='<div class="board-reading-guide"><span><strong>'+geometry.routes.length+' connections</strong> &middot; '+count+' handoffs'+(groupRoleHandoffs?' &middot; Click a connection to inspect its actions. Arrows show the available directions.':' &middot; Each action has its own line.')+'</span>'+(focused?'<button class="button small" id="clear-role-focus">'+esc(focused.name)+' &middot; Clear focus</button>':'<span>Use the focus button on a role to follow its connections.</span>')+'</div>'+
 '<div class="board-viewport" id="board-viewport"><div style="width:'+(width*zoom)+'px;height:'+(height*zoom)+'px"><div id="board" class="board" style="width:'+width+'px;height:'+height+'px;transform:scale('+zoom+')"><svg class="board-links" id="board-links">'+workflowMarkup(graph)+'</svg>'+roles.map(r=>'<div class="board-node role-node '+(!roleInFocus(r.id)?'is-muted ':'')+(r.id===focusedRoleId?'is-focused':'')+'" data-role-node="'+r.id+'" tabindex="0" aria-label="'+esc(r.name)+'. Drag or use arrow keys to move. Press Enter to focus connections." style="left:'+r.position.x+'px;top:'+r.position.y+'px"><div class="node-top"><div><button class="agent-name" data-edit-role="'+r.id+'">'+esc(r.name)+'</button><div class="node-role">'+(r.baseline?'Project role':'Local role')+' &middot; '+assignedAgents(r).length+' assigned</div></div></div><div class="role-node-agents">'+(typeof stuckCount==='function'&&stuckCount(assignedAgents(r))?'<span class="stuck-badge" title="Agents in this role are looping, stalled, admin-heavy or burning tokens">'+stuckCount(assignedAgents(r))+' stuck</span>':'')+(assignedAgents(r).map(a=>'<span class="'+(typeof healthChipClass==='function'?healthChipClass(a.id):'')+'" title="'+esc(a.name+(typeof healthTitle==='function'?healthTitle(a.id):''))+'">'+esc(a.name)+'</span>').join('')||'<span class="muted">Assign agents in Roles</span>')+'</div><div class="node-tickets">'+roleTicketBadge(r.id)+'</div><button class="icon-button role-focus" data-focus-role="'+r.id+'" aria-label="Focus connections for '+esc(r.name)+'" title="Focus connections" aria-pressed="'+(r.id===focusedRoleId)+'">'+icon('nodes')+'</button><span class="node-connector top"></span><span class="node-connector bottom"></span></div>').join('')+'</div></div></div>'+
 workflowRoutingNote(graph)+handoffsMarkup({agents:roles,links:f.roleLinks}).replaceAll('data-edit-link','data-edit-role-link')+(f.roles.some(FleetLayout.internalRole)?'<div class="board-note">Reviewer subagents are managed by their parent agents. Their responsibilities remain in <button class="text-button" id="board-role-definitions">Roles</button>; show them above to inspect internal review handoffs.</div>':'');
 $('board-viewport').scrollLeft=left;$('board-viewport').scrollTop=top;
 $('zoom-value').textContent=Math.round(zoom*100)+'%';
 if(focusAttribute&&focusValue)document.querySelector('['+focusAttribute+'="'+CSS.escape(focusValue)+'"]')?.focus({preventScroll:true});
};
function fitBoard(){
 const board=$('board'),viewport=$('board-viewport');if(!board||!viewport)return;
 zoom=Math.max(.2,Math.min(1,Math.floor(Math.min((viewport.clientWidth-20)/parseFloat(board.style.width),(viewport.clientHeight-20)/parseFloat(board.style.height))*100)/100));
 renderBoard();$('board-viewport').scrollLeft=0;$('board-viewport').scrollTop=0;
}
function focusRole(id){focusedRoleId=focusedRoleId===id?'':id;selectedRolePairId='';renderBoard();document.querySelector('[data-role-node="'+id+'"]')?.focus({preventScroll:true});}
function inspectRolePair(id){
 const f=current(),pair=FleetLayout.bundles(boardRoles(f.roles),f.roleLinks).find(p=>p.id===id);if(!pair)return;
 const name=id=>f.roles.find(r=>r.id===id)?.name||'Role';
 selectedRolePairId=id;renderBoard();
 $('handoff-group-title').textContent=name(pair.source)+(pair.reverse?' \u2194 ':' \u2192 ')+name(pair.target);
 $('handoff-group-body').innerHTML='<p>'+pair.members.length+' separate handoff'+(pair.members.length===1?'':'s')+' share this connection. Each keeps its own direction, artifact and condition.</p>'+pair.members.map(l=>'<article class="handoff-detail"><div class="eyebrow">'+esc(name(l.source))+' &rarr; '+esc(name(l.target))+'</div><h3>'+esc(l.label)+'</h3><p><strong>When:</strong> '+esc(l.trigger||'Not specified')+'</p><p><strong>Artifact:</strong> '+esc(l.artifact||'Not specified')+'</p><p><strong>Required condition:</strong> '+esc(l.condition||'Not specified')+'</p><button class="button small" data-inspector-edit="'+l.id+'">Edit handoff</button></article>').join('');
 $('handoff-group-dialog').showModal();
}
function textarea(label,id,value,limit=12000){return '<div class="field"><label for="'+id+'">'+label+'</label><textarea id="'+id+'" maxlength="'+limit+'">'+esc(value||'')+'</textarea></div>';}
function roleOptions(selected='',empty=true){return (empty?'<option value="">Choose role…</option>':'')+current().roles.map(r=>'<option value="'+r.id+'" '+(selected===r.id?'selected':'')+'>'+esc(r.name)+'</option>').join('');}
const originalOpenEditor=openEditor;
openEditor=function(type,id=null,preSession=null){
 if(!['role','rolelink','trigger'].includes(type)){
  originalOpenEditor(type,id,preSession);
  if(type==='agent'){
   $('f-role').closest('.field').classList.add('hidden');
   const block=document.createElement('div');block.className='shared-role-assignment';
   block.innerHTML='<h3>Shared roles</h3>'+roleChoices(editing.item.roleIds||[])+'<div id="assigned-role-details"></div>';
   $('editor-fields').insertBefore(block,$('f-notes').closest('.field'));
   $('f-notes').previousElementSibling.textContent='Agent-specific responsibilities / scope';
   $('f-notes').placeholder='Shared responsibilities come from the roles above. Add individual scope or exceptions to discuss here.';
   function details(){const ids=[...block.querySelectorAll('[data-role-choice]:checked')].map(n=>n.dataset.roleChoice);$('assigned-role-details').innerHTML=current().roles.filter(r=>ids.includes(r.id)).map(r=>'<details open><summary>'+esc(r.name)+'</summary><p>'+esc(r.responsibilities)+'</p><p><strong>Authority:</strong> '+esc(r.authority)+'</p></details>').join('');}
   block.onchange=details;details();
   if(id&&editing.item.sessionId){
    const actions=document.createElement('div');actions.className='agent-control-buttons';
    actions.innerHTML='<button type="button" class="button small" id="save-send-assignment">Save & send assignment</button><button type="button" class="button small" id="save-rename-session">Save & rename conversation</button><button type="button" class="button small" id="compose-instruction">Send instruction…</button>';
    $('editor-fields').append(actions);
    $('save-send-assignment').onclick=async()=>{await saveEditor(new Event('submit'));if(!editing)showControl('assignment',[id]);};
    $('save-rename-session').onclick=async()=>{await saveEditor(new Event('submit'));if(!editing)showControl('rename',[id]);};
    $('compose-instruction').onclick=()=>{const message=window.prompt('Instruction to send to '+editing.item.name+':');if(message?.trim())showControl('message',[id],message);};
   }
  }
  return;
 }
 const f=current(),list=type==='role'?f.roles:type==='rolelink'?f.roleLinks:(f.triggers||[]);
 const item=id?clone(list.find(r=>r.id===id)):type==='role'?{id:uid(),name:'',responsibilities:'',authority:'',baseline:null,position:{x:80,y:80}}:type==='rolelink'?{id:uid(),source:'',target:'',label:'',trigger:'',artifact:'',condition:''}:{id:uid(),name:'',kind:'event',sourceRoleId:'',targetRoleId:'',eventName:'packet-ready',condition:'turn-completed',minutes:30,enabled:false,mode:'automatic',message:''};
 editing={type,id,item};
 $('editor-title').textContent=(id?'Edit ':'New ')+{role:'role',rolelink:'workflow relationship',trigger:'trigger'}[type];
 $('editor-eyebrow').textContent=type==='role'?'SHARED DEFINITION':'WORKFLOW DESIGN';
 $('delete-item').classList.toggle('hidden',!id);$('delete-item').disabled=false;$('delete-item').textContent='Remove '+(type==='rolelink'?'relationship':type);
 if(type==='role'){
  $('editor-fields').innerHTML=field('Role name','name',item.name,'required maxlength="80"')+'<div class="field"><label for="f-role-kind">Role kind</label><select id="f-role-kind"><option value="agent">Fleet agent</option><option value="human">Human</option><option value="subagent">Parent-managed subagent</option></select></div><div class="form-row"><div class="field"><label for="f-role-backends">Allowed backends</label><select id="f-role-backends"><option value="">Any backend</option><option value="codex">Codex only</option><option value="claude-code">Claude Code only</option></select></div><div class="field"><label for="f-role-location">Location</label><select id="f-role-location"><option value="any">Local or remote</option><option value="local">This computer</option><option value="remote">Remote node</option></select></div></div>'+textarea('Goal: the outcome this role exists for','f-goal',item.goal,4000)+textarea('Success criteria (one per line)','f-successCriteria',(item.successCriteria||[]).join('\n'),4000)+field('Done when','doneWhen',item.doneWhen||'','maxlength="2000" placeholder="e.g. The change is merged and the ticket closed"')+'<p class="field-hint">The goal leads each agent\'s brief. Keep it to one or two sentences about the outcome, not the process.</p>'+textarea('Shared responsibilities','f-responsibilities',item.responsibilities)+textarea('Authority and handoff','f-authority',item.authority)+'<div class="field"><label>Distribute this role to agents</label><div class="role-choices">'+f.agents.map(a=>'<label class="checkbox-field"><input type="checkbox" data-agent-assignment="'+a.id+'" '+((a.roleIds||[]).includes(item.id)?'checked':'')+'>'+esc(a.name)+'</label>').join('')+'</div><p class="field-hint">Saving changes the design. Use Send assignments to instruct linked sessions.</p></div>'+(item.baseline&&!f.workflow?'<p class="field-hint">Linked to committed project policy. Edited definitions remain policy drafts until published and refreshed.</p>':'');
  $('f-role-kind').value=item.kind||(FleetLayout.internalRole(item)?'subagent':item.key==='project-owner'?'human':'agent');
  $('f-role-backends').value=item.runtime?.backends?.length===1?item.runtime.backends[0]:'';$('f-role-location').value=item.runtime?.location||'any';
 }else if(type==='rolelink'){
  $('editor-fields').innerHTML='<div class="form-row"><div class="field"><label for="f-source">From role</label><select id="f-source" required>'+roleOptions(item.source)+'</select></div><div class="field"><label for="f-target">To role</label><select id="f-target" required>'+roleOptions(item.target)+'</select></div></div>'+field('Action on connection','label',item.label,'required maxlength="100"')+textarea('When / trigger description','f-trigger',item.trigger,2000)+textarea('Handoff artifact','f-artifact',item.artifact,2000)+textarea('Required condition / acceptance','f-condition',item.condition,2000)+'<p class="field-hint">This relationship defines the contract. Use Triggers & actions to enable event, condition or timer delivery.</p>';
 }else{
  $('editor-fields').innerHTML=field('Trigger name','name',item.name,'required maxlength="100"')+'<div class="form-row"><div class="field"><label for="f-kind">Trigger type</label><select id="f-kind"><option value="event">Upstream reported event</option><option value="condition">Observed condition</option><option value="timer">Timer</option><option value="idle">Recurring idle check</option></select></div><div class="field"><label for="f-mode">Delivery</label><select id="f-mode"><option value="automatic">Send automatically</option><option value="review">Pending for review</option></select></div></div><div class="form-row"><div class="field"><label for="f-sourceRoleId">Upstream role</label><select id="f-sourceRoleId">'+roleOptions(item.sourceRoleId)+'</select></div><div class="field"><label for="f-targetRoleId">Target role</label><select id="f-targetRoleId" required>'+roleOptions(item.targetRoleId)+'</select></div></div>'+field('Reported event name','eventName',item.eventName,'maxlength="100"')+'<div class="field"><label for="f-condition">Observed condition</label><select id="f-condition"><option value="turn-completed">Turn completed successfully</option><option value="idle">Open & idle</option><option value="attention">Needs attention</option><option value="closed">Closed</option></select></div>'+field('Repeat interval (minutes)','minutes',item.minutes,'type="number" min="1" max="10080"')+'<div class="field"><label for="f-recipientMode">Event recipients</label><select id="f-recipientMode"><option value="single">One agent (reporter selects recipient)</option><option value="all">All agents with the target role</option></select></div>'+textarea('Instruction to send','f-message',item.message,12000)+'<label class="checkbox-field"><input type="checkbox" id="f-enabled" '+(item.enabled?'checked':'')+'>Enable this trigger</label><p class="field-hint">A completed turn does not prove packet acceptance. For formal handoffs, use a reported event. Automatic sends respect session availability, goal stops and pending queues; held handoffs retry automatically. Recurring idle checks wait for an empty queue and honor the interval per agent; finish a check when no work is eligible.</p>';
  $('f-kind').value=item.kind;$('f-mode').value=item.mode;$('f-condition').value=item.condition;$('f-recipientMode').value=item.recipientMode||'single';
  const toggles=()=>{const kind=$('f-kind').value;$('f-sourceRoleId').closest('.field').classList.toggle('hidden',['timer','idle'].includes(kind));$('f-eventName').closest('.field').classList.toggle('hidden',kind!=='event');$('f-condition').closest('.field').classList.toggle('hidden',kind!=='condition');$('f-minutes').closest('.field').classList.toggle('hidden',!['timer','idle'].includes(kind));$('f-recipientMode').closest('.field').classList.toggle('hidden',kind!=='event');};
  $('f-kind').onchange=toggles;toggles();
 }
 $('editor').showModal();
};const originalSaveEditor=saveEditor;
saveEditor=async function(event){
 event.preventDefault();
 if(!editing)return;
 if(editing.type==='agent'){
  editing.item.roleIds=[...document.querySelectorAll('[data-role-choice]:checked')].map(n=>n.dataset.roleChoice);
  return originalSaveEditor(event);
 }
 if(!['role','rolelink','trigger'].includes(editing.type))return originalSaveEditor(event);
 const action=clone(editing),item=action.item;
 let assigned=[];
 if(action.type==='role'){item.kind=$('f-role-kind').value;{const backend=$('f-role-backends').value,location=$('f-role-location').value;if(backend||location!=='any')item.runtime={backends:backend?[backend]:[],location};else delete item.runtime;}roleFields.forEach(k=>item[k]=$('f-'+k).value.trim());item.goal=$('f-goal').value.trim();item.successCriteria=$('f-successCriteria').value.split('\n').map(v=>v.trim()).filter(Boolean);item.doneWhen=$('f-doneWhen').value.trim();assigned=[...document.querySelectorAll('[data-agent-assignment]:checked')].map(n=>n.dataset.agentAssignment);}
 if(action.type==='rolelink'){['source','target','label','trigger','artifact','condition'].forEach(k=>item[k]=$('f-'+k).value.trim());}
 if(action.type==='trigger'){['name','kind','mode','sourceRoleId','targetRoleId','eventName','condition','message','recipientMode'].forEach(k=>item[k]=$('f-'+k).value.trim());item.minutes=Number($('f-minutes').value);item.enabled=$('f-enabled').checked;}
 $('save-editor').disabled=true;
 try{
  await mutate(next=>updateFleet(next,f=>{
   const key={role:'roles',rolelink:'roleLinks',trigger:'triggers'}[action.type];f[key]??=[];
   if(action.id)f[key][f[key].findIndex(r=>r.id===item.id)]=item;else f[key].push(item);
   if(action.type==='role')f.agents.forEach(a=>{a.roleIds=(a.roleIds||[]).filter(id=>id!==item.id);if(assigned.includes(a.id))a.roleIds.push(item.id);});
  }),action.type==='role'?'Role and assignments saved.':action.type==='trigger'?'Trigger saved.':'Workflow relationship saved.');
  $('editor').close();editing=null;
 }catch(e){toast(e.message,true);}finally{$('save-editor').disabled=false;}
};
$('editor-form').onsubmit=saveEditor;
const originalDeleteItem=deleteItem;
deleteItem=async function(){
 if(!editing||!['role','rolelink','trigger'].includes(editing.type))return originalDeleteItem();
 if(!window.confirm('Remove this '+editing.type+' from the fleet design?'))return;
 const action=clone(editing);
 try{await mutate(next=>updateFleet(next,f=>{
  const key={role:'roles',rolelink:'roleLinks',trigger:'triggers'}[action.type];f[key]=f[key].filter(r=>r.id!==action.id);
  if(action.type==='role'){f.agents.forEach(a=>a.roleIds=(a.roleIds||[]).filter(id=>id!==action.id));f.roleLinks=f.roleLinks.filter(l=>l.source!==action.id&&l.target!==action.id);f.triggers=(f.triggers||[]).filter(t=>t.sourceRoleId!==action.id&&t.targetRoleId!==action.id);}
 }),'Removed from design.');$('editor').close();editing=null;}catch(e){toast(e.message,true);}
};
$('delete-item').onclick=deleteItem;
async function showControl(action,agentIds,message='',prepared=null){
 try{
  controlPreview=prepared||await api('/api/control/preview',{fleetId:activeId,agentIds,action,message});
  $('control-title').textContent=action==='rename'?'Rename Codex conversation':action==='assignment'?'Send role assignments':'Send instruction';
  $('control-preview').innerHTML=controlPreview.targets.map(t=>'<div><h3>'+esc(t.name)+'</h3><p class="field-hint">'+esc(t.sessionId)+'</p><p>'+esc(t.effect||'')+'</p><pre>'+esc(t.text)+'</pre></div>').join('');
  $('send-control').textContent=action==='rename'?'Rename conversation':'Apply reviewed action';
  $('send-control').disabled=false;$('control-dialog').showModal();
 }catch(e){toast(e.message,true);}
}
$('close-control').onclick=()=>$('control-dialog').close();
$('send-control').onclick=async()=>{
 if(!controlPreview)return;
 $('send-control').disabled=true;
 try{await api('/api/control/send',{previewId:controlPreview.id});$('control-dialog').close();toast('Command submitted. Track acknowledgement in Triggers & actions.');refresh();}
 catch(e){toast(e.message,true);$('send-control').disabled=false;}
};
async function showPolicy(){
 try{
  policyDraft=await api('/api/roles/proposal',{fleetId:activeId});
  $('policy-notice').textContent=policyDraft.notice;
  $('policy-source').href=policyDraft.url;
  $('policy-diff').textContent=policyDraft.patch||'No role-policy changes against the committed source.';
  $('download-policy').disabled=!policyDraft.changed;
  $('policy-dialog').showModal();
 }catch(e){toast(e.message,true);}
}
function downloadText(filename,value){const url=URL.createObjectURL(new Blob([value],{type:'text/plain;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url);}
$('close-policy').onclick=()=>$('policy-dialog').close();
$('download-policy').onclick=()=>downloadText('role-policy-'+policyDraft.baseCommit.slice(0,9)+'.patch',policyDraft.patch);
$('download-policy-document').onclick=()=>downloadText('ROLES_AND_RESPONSIBILITIES.proposed.md',policyDraft.document);
$('board-mode').onchange=()=>{boardMode=$('board-mode').value;zoom=boardMode==='roles'?.8:1;localStorage.setItem('fleet-board-mode',boardMode);renderAll();};
$('group-role-handoffs').onchange=()=>{groupRoleHandoffs=$('group-role-handoffs').checked;selectedRolePairId='';localStorage.setItem('fleet-group-handoffs',String(groupRoleHandoffs));renderBoard();};
$('fit-board').onclick=fitBoard;
$('zoom-value').onclick=()=>{zoom=1;renderBoard();};
$('close-handoff-group').onclick=()=>$('handoff-group-dialog').close();
$('handoff-group-dialog').addEventListener('close',()=>{
 const pairId=selectedRolePairId;selectedRolePairId='';
 if(view==='board'&&boardMode==='roles'){
  renderBoard();
  if(pairId&&!$('editor').open)document.querySelector('path[data-inspect-role-pair="'+CSS.escape(pairId)+'"]')?.focus({preventScroll:true});
 }
});
$('handoff-group-body').addEventListener('click',e=>{const b=e.target.closest('[data-inspector-edit]');if(b){$('handoff-group-dialog').close();openEditor('rolelink',b.dataset.inspectorEdit);}});
$('show-internal-roles').onchange=()=>{showInternalRoles=$('show-internal-roles').checked;localStorage.setItem('fleet-internal-roles',String(showInternalRoles));renderBoard();};
const oldAddLink=$('add-link').onclick;
$('add-link').onclick=()=>boardMode==='roles'?openEditor('rolelink'):oldAddLink();
$('arrange').onclick=async()=>{
 const fleetId=activeId,mode=boardMode,includeInternal=showInternalRoles;
 $('arrange').disabled=true;
 try{await mutate(next=>{
  const f=next.fleets.find(f=>f.id===fleetId),all=mode==='roles'?f.roles:f.agents,nodes=mode==='roles'?FleetLayout.visibleRoles(all,includeInternal):all;
  const positions=FleetLayout.arrange(nodes,mode==='roles'?f.roleLinks:f.links);
  nodes.forEach(n=>n.position=positions[n.id]);
  // Keep hidden internal cards clear, ready to be shown without stacking.
  const placed=[...nodes];
  for(const n of all.filter(n=>!nodes.includes(n))){
   if(placed.some(p=>FleetLayout.intersects(FleetLayout.rect(n,25),FleetLayout.rect(p,25)))){
    n.position={x:80,y:Math.max(100,...placed.map(p=>p.position.y+260))};
    if(n.position.y>4000)throw new Error('No space remains for hidden roles. Show all roles and arrange a smaller workflow.');
   }
   placed.push(n);
  }
 },'Workflow arranged.');
 fitBoard();
 }catch(e){toast(e.message,true);}finally{$('arrange').disabled=false;}
};
$('content').addEventListener('click',async e=>{
 const b=e.target.closest('button,[data-edit-role-link],[data-inspect-role-pair]');if(!b)return;
 if(b.dataset.inspectRolePair)inspectRolePair(b.dataset.inspectRolePair);
 if(b.dataset.focusRole)focusRole(b.dataset.focusRole);
 if(b.id==='clear-role-focus'){focusedRoleId='';renderBoard();}
 if(b.id==='board-role-definitions')setView('roles');
 if(b.id==='new-role')openEditor('role');
 if(b.id==='new-trigger')openEditor('trigger');
 if(b.dataset.editRole)openEditor('role',b.dataset.editRole);
 if(b.dataset.editRoleLink)openEditor('rolelink',b.dataset.editRoleLink);
 if(b.dataset.editTrigger)openEditor('trigger',b.dataset.editTrigger);
 if(b.dataset.applyRole){const agents=assignedAgents(current().roles.find(r=>r.id===b.dataset.applyRole)).filter(a=>a.sessionId);showControl('assignment',agents.map(a=>a.id));}
 if(b.id==='policy-proposal')showPolicy();
 if(b.id==='refresh-roles'){
  b.disabled=true;
  try{const data=await api('/api/roles/refresh',{fleetId:activeId,revision:state.revision});state=data.state;renderAll();toast('Committed role definitions refreshed.');}catch(error){toast(error.message,true);b.disabled=false;}
 }
 if(b.dataset.dismissTrigger){try{await api('/api/triggers/dismiss',{actionId:b.dataset.dismissTrigger});refresh();}catch(error){toast(error.message,true);}}
 if(b.dataset.reviewTrigger){
  const action=monitor.automation.actions.find(a=>a.id===b.dataset.reviewTrigger),rule=current().triggers.find(t=>t.id===action.triggerId);
  if(!rule)return toast('This trigger was removed.',true);
  const ids=current().agents.filter(a=>(a.roleIds||[]).includes(rule.targetRoleId)).map(a=>a.id);
  try{const preview=await api('/api/triggers/review',{actionId:action.id});showControl('message',ids,action.message,preview);}catch(error){toast(error.message,true);}
 }
});
$('content').addEventListener('pointerdown',e=>{
 const node=e.target.closest('[data-role-node]');if(!node||e.target.closest('button')||e.button!==0)return;
 const role=current().roles.find(r=>r.id===node.dataset.roleNode);
 roleDrag={id:role.id,node,origin:{...role.position},position:{...role.position},x:e.clientX,y:e.clientY,pointer:e.pointerId,fleetId:activeId};
 node.setPointerCapture(e.pointerId);e.preventDefault();
});
$('content').addEventListener('pointermove',e=>{
 if(!roleDrag||roleDrag.saving||roleDrag.pointer!==e.pointerId)return;
 roleDrag.position={x:Math.max(0,Math.min(4000,Math.round(roleDrag.origin.x+(e.clientX-roleDrag.x)/zoom))),y:Math.max(0,Math.min(4000,Math.round(roleDrag.origin.y+(e.clientY-roleDrag.y)/zoom)))};
 roleDrag.node.style.left=roleDrag.position.x+'px';roleDrag.node.style.top=roleDrag.position.y+'px';
 const roles=clone(boardRoles(current().roles)).filter(r=>document.querySelector('[data-role-node="'+r.id+'"]'));roles.find(r=>r.id===roleDrag.id).position=roleDrag.position;
 $('board-links').innerHTML=workflowMarkup(roleBoardGraph(roles,current().roleLinks));
});
async function finishRoleDrag(e){
 if(!roleDrag||roleDrag.saving||e.pointerId!==roleDrag.pointer)return;
 if(roleDrag.position.x===roleDrag.origin.x&&roleDrag.position.y===roleDrag.origin.y){roleDrag=null;return;}
 roleDrag.saving=true;const change={id:roleDrag.id,position:roleDrag.position,fleetId:roleDrag.fleetId};
 try{await mutate(next=>{next.fleets.find(f=>f.id===change.fleetId).roles.find(r=>r.id===change.id).position=change.position;});}
 catch(error){toast(error.message,true);}finally{roleDrag=null;renderContent();}
}
$('content').addEventListener('pointerup',finishRoleDrag);$('content').addEventListener('pointercancel',finishRoleDrag);
$('content').addEventListener('keydown',e=>{
 const pair=e.target.closest('[data-inspect-role-pair]');if(pair&&['Enter',' '].includes(e.key)){e.preventDefault();inspectRolePair(pair.dataset.inspectRolePair);return;}
 if(e.key==='Escape'&&focusedRoleId){focusedRoleId='';renderBoard();return;}
 const node=e.target.closest('[data-role-node]');if(!node||e.target.closest('button'))return;
 if(e.key==='Enter'){e.preventDefault();focusRole(node.dataset.roleNode);return;}
 const delta={ArrowUp:[0,-20],ArrowDown:[0,20],ArrowLeft:[-20,0],ArrowRight:[20,0]}[e.key];if(!delta)return;
 e.preventDefault();const id=node.dataset.roleNode;
 mutate(next=>updateFleet(next,f=>{const r=f.roles.find(r=>r.id===id);r.position={x:Math.max(0,Math.min(4000,r.position.x+delta[0])),y:Math.max(0,Math.min(4000,r.position.y+delta[1]))};})).then(()=>document.querySelector('[data-role-node="'+id+'"]')?.focus({preventScroll:true})).catch(error=>toast(error.message,true));
});let geometryCache={key:'',value:null};
function workflowGeometry(graph){
 const nodes=graph.agents.map(n=>({...n,position:nodePosition(n)}));
 const key=JSON.stringify([nodes.map(n=>[n.id,n.position.x,n.position.y]),graph.links.map(l=>[l.id,l.source,l.target,l.label,l.members?.map(m=>[m.id,m.source,m.target,m.label])])]);
 if(key!==geometryCache.key)geometryCache={key,value:FleetLayout.route(nodes,graph.links)};
 return geometryCache.value;
}
function workflowRoutingNote(graph){
 const count=workflowGeometry(graph).routes.filter(r=>r.unrouted).length;
 return count?'<div class="board-note">'+count+' connection(s) have no clear path. Move cards apart or use Arrange. Their contracts remain in the handoff list.</div>':'';
}
function workflowMarkup(graph,attribute='data-edit-role-link'){
 const geometry=workflowGeometry(graph),paths=[],labels=[];
 const name=id=>graph.agents.find(n=>n.id===id)?.name||'Role';
 for(const route of geometry.routes){
  if(route.unrouted)continue;
  const {link,points,label}=route,path=points.map((p,i)=>(i?'L ':'M ')+p.x+' '+p.y).join(' ');
  const attr=link.members?'data-inspect-role-pair="'+esc(link.id)+'"':attribute+'="'+esc(link.id)+'"';
  const title=link.members?link.members.map(l=>name(l.source)+' \u2192 '+name(l.target)+': '+l.label).join('\n'):link.label;
  const roleGraph=attribute==='data-edit-role-link',focused=roleGraph&&(focusedRoleId||selectedRolePairId);
  const related=!focused||(selectedRolePairId?link.id===selectedRolePairId:link.source===focusedRoleId||link.target===focusedRoleId);
  const cls=(!related?' is-muted':'')+(focused&&related?' is-highlighted':'');
  paths.push('<g class="workflow-connection'+cls+'"><path class="workflow-path" data-route="'+esc(link.id)+'" d="'+path+'" marker-end="url(#workflow-arrow)"'+(link.reverse?' marker-start="url(#workflow-arrow)"':'')+'></path><path class="link-hit" d="'+path+'" '+attr+(link.members?' tabindex="0" role="button" aria-label="'+esc('Inspect '+link.members.length+' handoffs between '+name(link.source)+' and '+name(link.target))+'"':'')+'><title>'+esc(title)+'</title></path></g>');
  if(label){const b=label.box,cx=b.x+b.w/2,cy=b.y+b.h/2;
   labels.push('<g class="workflow-label'+cls+'" '+attr+'><title>'+esc(title)+'</title>'+(label.anchor.x!==cx||label.anchor.y!==cy?'<path class="label-leader" d="M '+label.anchor.x+' '+label.anchor.y+' L '+cx+' '+cy+'"></path>':'')+'<rect x="'+b.x+'" y="'+b.y+'" width="'+b.w+'" height="'+b.h+'" rx="5"></rect><text x="'+(b.x+10)+'" y="'+(b.y+17)+'">'+label.lines.map((line,i)=>'<tspan x="'+(b.x+10)+'" dy="'+(i?15:0)+'">'+esc(line)+'</tspan>').join('')+'</text></g>');
  }
 }
 return '<defs><marker id="workflow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" style="fill:#78987a;stroke:none"></path></marker></defs>'+paths.join('')+labels.join('');
}
linksMarkup=function(graph){return workflowMarkup(graph,'data-edit-link');};
