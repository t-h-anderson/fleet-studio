'use strict';
const $=(id)=>document.getElementById(id);
const paths={
grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',nodes:'M9 3h6v5H9zM2 16h6v5H2zM16 16h6v5h-6zM12 8v4M5 16v-4h14v4',
terminal:'M3 4h18v16H3zM7 9l3 3-3 3M13 15h4',plus:'M12 5v14M5 12h14',minus:'M5 12h14',search:'M21 21l-5-5M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',play:'M8 4l12 8-12 8z',more:'M5 12h.01M12 12h.01M19 12h.01',
agents:'M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M19 8a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3M12 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',activity:'M2 12h5l3-8 4 16 3-8h5',shield:'M12 3l8 4v5c0 5-8 9-8 9s-8-4-8-9V7zM8 12l3 3 5-6',
settings:'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',refresh:'M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2',close:'M6 6l12 12M6 18L18 6',edit:'M15 4l5 5M4 20l5-1L21 7l-5-5L4 14z',
link:'M10 13a5 5 0 0 0 7 0l4-4a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-4 4a5 5 0 0 0 7 7l2-2',upload:'M12 16V3M7 8l5-5 5 5M4 16v5h16v-5',download:'M12 3v13M7 11l5 5 5-5M4 17v4h16v-4',folder:'M3 5h6l2 3h10v12H3z',arrow:'M5 12h14M14 7l5 5-5 5',heart:'M12 21s-8-5-8-11a4 4 0 0 1 8-2 4 4 0 0 1 8 2c0 6-8 11-8 11z',rules:'M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2',chart:'M4 20V10M10 20V4M16 20v-7M22 20H2',flag:'M5 21V4h11l-2 4 2 4H5',user:'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',workflow:'M4 5h6v4H4zM14 15h6v4h-6zM7 9v4a2 2 0 0 0 2 2h5',trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'};
function icon(name){return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="'+(paths[name]||paths.grid)+'"></path></svg>';}
function icons(){document.querySelectorAll('[data-icon]').forEach((n)=>n.innerHTML=icon(n.dataset.icon));}
function esc(value){return String(value??'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function uid(){return crypto.randomUUID();}
function clone(value){return structuredClone(value);}
const labels={working:'Working',idle:'Open & idle',closed:'Closed',attention:'Needs attention',unknown:'Unknown',draft:'Not linked'};
let state=null,token='',monitor={sessions:{},events:[],jobs:[]},catalogue=[];
let activeId=localStorage.getItem('fleet-active')||'',view=localStorage.getItem('fleet-view')||'health';
// The old Triggers view is now part of the Workflow screen.
if(view==='triggers')view='board';
if(!['roster','board','sessions','roles','backlog','activity','workflow','profiles','progress','health','automation','throughput'].includes(view))view='health';
let query='',filter='all',selected=new Set(),editing=null,zoom=1,drag=null,pendingJob=null,stopped=false,saveQueue=Promise.resolve();
function current(){return state.fleets.find((f)=>f.id===activeId)||state.fleets[0];}
function sessionKey(a){const sid=a.sessionId??a.id??'',host=a.hostId||'local',backend=a.backend||'codex';return host==='local'&&backend==='codex'?sid:[host,backend,sid].join(':');}
function sessionFor(a){return monitor.sessions[sessionKey(a)]||{};}
function statusOf(a){return a.sessionId?(sessionFor(a).status||'unknown'):'draft';}
function displayedRole(a){const roles=(current().roles||[]).filter(r=>(a.roleIds||[]).includes(r.id));return roles.length?roles.map(r=>r.name).join(', '):a.role;}
function launchable(a){return a.enabled&&a.sessionId&&a.directory;}
function initials(n){return n.split(/\s+/).slice(0,2).map((s)=>s[0]||'').join('').toUpperCase();}
function color(n){return Array.from(n).reduce((s,c)=>s+c.charCodeAt(0),0)%5;}
function folderName(p){return p.split(/[\\/]/).filter(Boolean).pop()||'Choose a workspace';}
function ago(t){if(!t)return '—';const s=Math.max(0,Date.now()/1000-Number(t));return s<60?'Just now':s<3600?Math.floor(s/60)+'m ago':s<86400?Math.floor(s/3600)+'h ago':Math.floor(s/86400)+'d ago';}
function avatar(n){return '<div class="avatar color-'+color(n)+'">'+esc(initials(n))+'</div>';}
function statusLabel(a){const s=statusOf(a),p=sessionFor(a).pids||[];return '<span class="status-label '+s+'" title="'+esc(p.length?'PID '+p.join(', '):labels[s])+'"><span class="status-dot '+s+'"></span>'+labels[s]+'</span>';}
function toast(message,error=false){const n=document.createElement('div');n.className='toast'+(error?' error':'');n.textContent=message;const b=document.createElement('button');b.textContent='×';b.setAttribute('aria-label','Dismiss');b.onclick=()=>n.remove();n.append(b);$('toasts').append(n);while($('toasts').children.length>3)$('toasts').firstElementChild.remove();setTimeout(()=>n.remove(),error?13000:6000);}
async function api(path,body){const options={headers:{}};if(body!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-Fleet-Token':token};options.body=JSON.stringify(body);}const r=await fetch(path,options),data=await r.json();if(!r.ok)throw new Error(data.error||'Request failed.');return data;}
function mutate(change,message){const op=saveQueue.catch(()=>{}).then(async()=>{const next=clone(state);change(next);state=(await api('/api/state',next)).state;if(!state.fleets.some((f)=>f.id===activeId))activeId=state.defaultFleetId;renderAll();if(message)toast(message);return state;});saveQueue=op;return op;}
// Rules and limits that name a removed role or agent go with it; the server rejects dangling references.
// A rule scoped to a removed role or agent is removed, never widened to the whole fleet.
function forgetRole(f,roleId){
 f.automations=(f.automations||[]).filter(r=>r.roleId!==roleId).map(r=>r.handoffRoleId===roleId?{...r,handoffRoleId:''}:r);
 if(f.throttles?.roleTokensPerHour){delete f.throttles.roleTokensPerHour[roleId];if(!Object.keys(f.throttles.roleTokensPerHour).length)delete f.throttles.roleTokensPerHour;}
}
function forgetAgent(f,agentId){f.automations=(f.automations||[]).filter(r=>r.agentId!==agentId);}
function updateFleet(next,change){change(next.fleets.find((f)=>f.id===activeId));}
function filteredAgents(){return current().agents.filter((a)=>(a.name+' '+displayedRole(a)+' '+a.directory).toLowerCase().includes(query.toLowerCase())&&(filter==='all'||(filter==='disabled'?!a.enabled:statusOf(a)===filter)));}
function setView(v){view=v;localStorage.setItem('fleet-view',v);query='';filter='all';$('search').value='';$('status-filter').value='all';selected.clear();renderAll();}
function setFleet(id){activeId=id;localStorage.setItem('fleet-active',id);query='';filter='all';$('search').value='';$('status-filter').value='all';selected.clear();renderAll();}
function renderAll(){
 if(!state)return;const f=current();activeId=f.id;
 $('fleet-list').innerHTML=state.fleets.map((v)=>'<button class="fleet-item '+(v.id===activeId?'active':'')+'" data-fleet="'+v.id+'"><span class="fleet-mark"></span><span class="fleet-name">'+esc(v.name)+'</span><span class="fleet-count">'+v.agents.length+'</span></button>').join('');
 document.querySelectorAll('.nav-item').forEach((n)=>n.classList.toggle('active',n.dataset.view===view));
 $('breadcrumb-fleet').textContent=f.name;$('fleet-title').textContent=view==='sessions'?'Session library':f.name;
 $('fleet-description').textContent=view==='sessions'?'Find a saved conversation and bring it into your fleet.':f.description||'A workspace for your agents.';
 $('view-eyebrow').textContent={roster:'FLEET OVERVIEW',board:'FLEET DESIGN',sessions:'LOCAL CONVERSATIONS',roles:'SHARED RESPONSIBILITIES',triggers:'WORKFLOW AUTOMATION',backlog:'WORK THROUGH THE FLEET',activity:'LIVE CONVERSATIONS'}[view];
 $('panel-title').textContent={roster:'Your agents',board:'Design board',sessions:'Saved sessions',roles:'Role definitions',triggers:'Triggers & actions',backlog:'Project backlog',activity:'Agent activity'}[view];
 $('board-toolbar').classList.toggle('hidden',view!=='board');$('status-filter').classList.toggle('hidden',['sessions','roles','backlog','activity','workflow'].includes(view));$('workspace-layout').classList.toggle('board-layout',view==='board');
 $('search').placeholder=view==='sessions'?'Find a session…':'Find an agent…';
 $('panel-footer-text').textContent=view==='board'?'Positions and handoffs are saved automatically.':'Live status refreshes every 5 seconds';
 renderMetrics();renderContent();renderInsights();
}
function renderMetrics(){
 if(!state)return;const agents=current().agents,c={working:0,idle:0,closed:0,attention:0,draft:0,unknown:0};agents.forEach((a)=>c[statusOf(a)]++);
 $('metric-total').textContent=agents.length;$('metric-enabled').textContent=agents.filter((a)=>a.enabled).length+' included in fleet launch';$('metric-working').textContent=c.working;$('metric-idle').textContent=c.idle;$('metric-closed').textContent=c.closed+c.draft;
 $('metric-closed-note').textContent=c.draft?c.draft+' agent(s) need a linked session':'Resume when you need them';
 const busy=pendingJob||(monitor.jobs||[]).some((j)=>j.status==='running');$('open-fleet').disabled=!!busy||!agents.some(launchable);$('open-selected').disabled=!!busy;$('open-fleet').innerHTML=icon('play')+(busy?'Opening…':'Open fleet');
 const old=monitor.observedAt&&Date.now()/1000-monitor.observedAt>20;$('live-pill').innerHTML='<span class="status-dot '+(!old&&monitor.observedAt?'working':'unknown')+'"></span><span>'+(!monitor.observedAt?'Connecting':old?'Status delayed':'Live · every 5s')+'</span>';
 const errors=monitor.errors||[];$('monitor-banner').classList.toggle('hidden',!errors.length&&!old);$('monitor-banner').textContent=errors.length?errors.join(' · '):old?'Status is delayed. Displayed observations may be out of date.':'';
}
function renderContent(){
 if(!state||drag)return;const focused=document.activeElement?.dataset.node;if(view==='board')renderBoard();else if(view==='sessions')renderSessions();else if(view==='roles')renderRoles();else if(view==='backlog')renderBacklog();else if(view==='activity')renderAgentActivity();else if(view==='workflow')renderWorkflow();else if(view==='progress')renderProgress();else renderTable();
 if(focused)document.querySelector('[data-node="'+focused+'"]')?.focus({preventScroll:true});
 const valid=new Set(current().agents.map((a)=>a.id));selected=new Set([...selected].filter((id)=>valid.has(id)));$('selection-bar').classList.toggle('hidden',!selected.size||view!=='roster');$('selection-count').textContent=selected.size+' selected';
}
function empty(title,description,button){return '<div class="empty-state">'+icon('nodes')+'<h3>'+esc(title)+'</h3><p>'+esc(description)+'</p>'+(button?'<button class="button primary" data-action="add">'+icon('plus')+'Add an agent</button>':'')+'</div>';}
function renderTable(){
 const agents=filteredAgents();$('visible-count').textContent=agents.length;
 if(!agents.length){$('content').innerHTML=empty(current().agents.length?'No agents match':'A fresh space for your fleet',current().agents.length?'Try another name, role or status.':'Add an agent, choose a saved session, and make this fleet your own.',!current().agents.length);return;}
 const all=agents.filter(launchable),checked=all.length&&all.every((a)=>selected.has(a.id));
 $('content').innerHTML='<div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="select-all" aria-label="Select all launchable agents" '+(checked?'checked':'')+'></th><th>AGENT / WORKSPACE</th><th>ROLE</th><th>STATUS</th><th class="model-column">MODEL</th><th>ACTIVITY</th><th>TICKETS</th><th></th></tr></thead><tbody>'+agents.map((a)=>{
 const s=sessionFor(a).session||catalogue.find((s)=>sessionKey(s)===sessionKey(a))||{};
 return '<tr data-agent="'+a.id+'"><td><input type="checkbox" data-select="'+a.id+'" aria-label="Select '+esc(a.name)+'" '+(selected.has(a.id)?'checked ':'')+(!launchable(a)?'disabled':'')+'></td><td><div class="agent-cell">'+avatar(a.name)+'<div><button class="agent-name" data-edit="'+a.id+'">'+esc(a.name)+'</button><div class="agent-folder" title="'+esc(a.directory)+'">'+(!a.enabled?'Excluded · ':'')+((a.backend||'codex')!=='codex'||(a.hostId||'local')!=='local'?esc(runtimeLabel(a))+' · ':'')+esc(folderName(a.directory))+'</div></div></div></td><td><span class="role-pill">'+esc(displayedRole(a))+'</span></td><td>'+statusLabel(a)+'</td><td class="model-column"><span class="model-name">'+esc(s.model||'—')+'</span></td><td class="muted">'+ago(s.updatedAt)+'</td><td>'+agentTicketBadge(a.id)+'</td><td><div class="row-actions"><button class="icon-button" data-agent-activity="'+a.id+'" aria-label="Activity for '+esc(a.name)+'" title="Read agent activity">'+icon('activity')+'</button><button class="icon-button" data-open="'+a.id+'" aria-label="Open '+esc(a.name)+'" title="Open in PowerShell" '+(!launchable(a)?'disabled':'')+'>'+icon('play')+'</button><button class="icon-button" data-edit="'+a.id+'" aria-label="Edit '+esc(a.name)+'" title="Edit agent">'+icon('more')+'</button></div></td></tr>';
 }).join('')+'</tbody></table></div>';
}
function renderSessions(){
 const q=query.toLowerCase(),rows=catalogue.filter((s)=>(s.name+' '+s.directory).toLowerCase().includes(q));$('visible-count').textContent=rows.length;
 if(!rows.length){$('content').innerHTML=empty('No matching sessions','Refresh the library or try a different search.',false);return;}
 const linked=new Set(current().agents.map(sessionKey));
 $('content').innerHTML='<div class="table-wrap"><table><thead><tr><th>CONVERSATION</th><th>MODEL</th><th>ACTIVITY</th><th>TICKETS</th><th></th></tr></thead><tbody>'+rows.map((s)=>'<tr><td><div class="session-name" title="'+esc(s.name)+'">'+esc(s.name)+'</div><div class="session-directory" title="'+esc(s.directory)+'">'+esc(s.directory)+'</div></td><td class="model-name">'+esc(s.model||'—')+'</td><td class="muted">'+ago(s.updatedAt)+'</td><td><button class="button small session-add" data-add-session="'+esc(sessionKey(s))+'" '+(linked.has(sessionKey(s))?'disabled':'')+'>'+icon('plus')+(linked.has(sessionKey(s))?'In this fleet':'Add to fleet')+'</button></td></tr>').join('')+'</tbody></table></div>';
}
function nodePosition(a){return drag&&drag.id===a.id?drag.position:a.position;}
function linksMarkup(f){
 return '<defs><marker id="arrow-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" style="fill:#a6bea0;stroke:none"></path></marker></defs>'+f.links.map((l)=>{
 const s=f.agents.find((a)=>a.id===l.source),t=f.agents.find((a)=>a.id===l.target);if(!s||!t)return '';
 const sp=nodePosition(s),tp=nodePosition(t),forward=sp.y<=tp.y,x1=sp.x+122,y1=sp.y+(forward?127:0),x2=tp.x+122,y2=tp.y+(forward?0:127),bend=Math.max(45,Math.abs(y2-y1)/2)*(forward?1:-1);
 const p='M '+x1+' '+y1+' C '+x1+' '+(y1+bend)+' '+x2+' '+(y2-bend)+' '+x2+' '+y2;
 return '<g><path d="'+p+'" marker-end="url(#arrow-head)"></path><path class="link-hit" d="'+p+'" data-edit-link="'+l.id+'"></path><text x="'+((x1+x2)/2+8)+'" y="'+((y1+y2)/2-5)+'" data-edit-link="'+l.id+'">'+esc(l.label||'Handoff')+'</text></g>';
 }).join('');
}
function handoffsMarkup(f){
 const links=f.links.filter((l)=>f.agents.some((a)=>a.id===l.source)&&f.agents.some((a)=>a.id===l.target));
 if(!links.length)return '';
 return '<div class="handoff-strip" aria-label="Handoffs"><span>Handoffs</span>'+links.map((l,i)=>{
  const source=f.agents.find((a)=>a.id===l.source),target=f.agents.find((a)=>a.id===l.target);
  return '<button class="button small" data-edit-link="'+l.id+'"><span class="handoff-number">'+(i+1)+'</span>'+esc(source.name)+' &rarr; '+esc(target.name)+'<span>'+esc(l.label||'Edit handoff')+'</span></button>';
 }).join('')+'</div>';
}
function renderBoard(){
 const f={...current(),agents:filteredAgents()};$('visible-count').textContent=f.agents.length;if(!f.agents.length){$('content').innerHTML=empty(current().agents.length?'No agents match':'Design your first agent',current().agents.length?'Try another name, role or status.':'Define a role now. Link a saved Codex session when it is ready to run.',!current().agents.length);return;}
 const geometry=workflowGeometry(f),maxX=geometry.width,maxY=geometry.height,old=$('board-viewport'),sx=old?old.scrollLeft:0,sy=old?old.scrollTop:0;
 $('content').innerHTML='<div class="board-viewport" id="board-viewport"><div style="width:'+(maxX*zoom)+'px;height:'+(maxY*zoom)+'px"><div class="board" id="board" style="width:'+maxX+'px;height:'+maxY+'px;transform:scale('+zoom+')"><svg class="board-links" id="board-links">'+linksMarkup(f)+'</svg>'+f.agents.map((a)=>'<div class="board-node" data-node="'+a.id+'" tabindex="0" role="group" aria-label="'+esc(a.name)+'. Drag or use arrow keys to move." style="left:'+a.position.x+'px;top:'+a.position.y+'px"><span class="node-connector top"></span><div class="node-top">'+avatar(a.name)+'<div><button class="agent-name" data-edit="'+a.id+'">'+esc(a.name)+'</button><div class="node-role">'+esc(displayedRole(a))+(!a.enabled?' · Excluded':'')+'</div></div><button class="icon-button" data-edit="'+a.id+'" aria-label="Edit '+esc(a.name)+'">'+icon('more')+'</button></div><div class="node-footer">'+statusLabel(a)+'<button class="icon-button" data-open="'+a.id+'" title="Open in PowerShell" aria-label="Open '+esc(a.name)+'" '+(!launchable(a)?'disabled':'')+'>'+icon('play')+'</button></div><div class="node-tickets">'+agentTicketBadge(a.id)+'</div><span class="node-connector bottom"></span></div>').join('')+'</div></div></div>'+workflowRoutingNote(f)+handoffsMarkup(f);
 $('board-viewport').scrollLeft=sx;$('board-viewport').scrollTop=sy;$('zoom-value').textContent=Math.round(zoom*100)+'%';
}
function renderInsights(){
 if(!state)return;const agents=current().agents,keys=['working','idle','closed','attention','draft','unknown'],c={};keys.forEach((s)=>c[s]=agents.filter((a)=>statusOf(a)===s).length);
 $('pulse-track').innerHTML=keys.filter((s)=>c[s]).map((s)=>'<span class="pulse-segment '+s+'" style="flex:'+c[s]+'" title="'+labels[s]+': '+c[s]+'"></span>').join('');
 $('pulse-legend').innerHTML=keys.filter((s)=>c[s]||['working','idle','closed'].includes(s)).map((s)=>'<div class="legend-row"><span><i class="status-dot '+s+'"></i>'+labels[s]+'</span><strong>'+c[s]+'</strong></div>').join('');
 const events=(monitor.events||[]).filter((e)=>!e.fleetId||e.fleetId===activeId).slice(0,7);$('event-count').textContent=events.length;
 $('activity-list').innerHTML=events.length?events.map((e)=>'<div class="activity-item"><span class="event-dot"></span><div><div class="event-title">'+esc(e.title)+'</div><div class="event-detail">'+esc(e.detail)+'</div><div class="event-time">'+ago(e.time)+'</div></div></div>').join(''):'<div class="activity-empty">Fleet changes and session status will appear here.</div>';
}

// Focus anywhere in a form counts as editing, so a click on its button is not lost to a redraw;
// so do typed values not yet saved, even after focus moves elsewhere (the Workflow inspector counts as a form).
function editingContent(){
 if(document.activeElement?.closest('#content input,#content textarea,#content select,#content form'))return true;
 const fields='#content form input,#content form textarea,#design-inspector input,#design-inspector textarea';
 return [...document.querySelectorAll(fields)].some(n=>n.type==='checkbox'||n.type==='radio'?n.checked!==n.defaultChecked:n.value!==n.defaultValue)||
  [...document.querySelectorAll('#design-inspector select')].some(n=>[...n.options].some(o=>o.selected!==o.defaultSelected));
}
// Views whose timed redraws wait while someone is editing on them.
const EDITABLE_VIEWS=['health','automation','throughput','board','profiles'];
async function refresh(full=false){
 if(stopped)return;
 try{
  monitor=await api('/api/status');if(full||!catalogue.length||view==='sessions')catalogue=(await api('/api/sessions')).sessions;
  if(pendingJob){const j=(monitor.jobs||[]).find((j)=>j.id===pendingJob);if(j&&j.status!=='running'){pendingJob=null;toast(j.message,j.status==='failed');}}
  // A timed refresh must not redraw a form someone is filling in; saves and navigation still do.
  renderMetrics();if(!(EDITABLE_VIEWS.includes(view)&&editingContent()))renderContent();renderInsights();
 }catch(error){$('live-pill').innerHTML='<span class="status-dot attention"></span><span>Disconnected</span>';$('monitor-banner').textContent='The local app is not responding. Use the Fleet Studio shortcut to reopen it.';$('monitor-banner').classList.remove('hidden');}
}
async function launch(ids){
 if(!ids.length)return toast('Link a session and workspace before opening this agent.',true);
 try{const r=await api('/api/launch',{fleetId:activeId,agentIds:ids});pendingJob=r.job.id;renderMetrics();toast('Opening '+ids.length+' agent(s). Already-open sessions will be skipped.');}
 catch(e){toast(e.message,true);}
}
function field(label,name,value,extra=''){return '<div class="field"><label for="f-'+name+'">'+label+'</label><input id="f-'+name+'" name="'+name+'" value="'+esc(value)+'" '+extra+'></div>';}
function openEditor(type,id=null,preSession=null){
 const f=current();
 editing={type,id,item:type==='agent'?(id?clone(f.agents.find((a)=>a.id===id)):{id:uid(),name:preSession?preSession.name.slice(0,80):'',role:'Agent',directory:preSession?preSession.directory:'',sessionId:preSession?preSession.id:'',backend:preSession?.backend||'codex',hostId:preSession?.hostId||'local',enabled:true,notes:'',position:{x:80+(f.agents.length%3)*320,y:60+Math.floor(f.agents.length/3)*200}}):type==='fleet'?(id?clone(f):{id:uid(),name:'',description:'',windowName:'',agents:[],links:[]}):(id?clone(f.links.find((l)=>l.id===id)):{id:uid(),source:'',target:'',label:''})};
 $('editor-title').textContent=(id?'Edit ':'New ')+{agent:'agent',fleet:'fleet',link:'handoff'}[type];
 $('editor-eyebrow').textContent=type==='link'?'ROLES & RELATIONSHIPS':'FLEET CONFIGURATION';
 $('delete-item').classList.toggle('hidden',!id);$('delete-item').textContent=type==='fleet'?'Delete fleet':type==='link'?'Remove handoff':'Remove agent';$('delete-item').disabled=type==='fleet'&&state.fleets.length===1;
 const item=editing.item;
 if(type==='agent'){
  $('editor-fields').innerHTML='<div class="form-row">'+field('Agent name','name',item.name,'required maxlength="80"')+field('Role','role',item.role,'required maxlength="60" list="role-options"')+'</div><datalist id="role-options">'+['Coordination','Writer','Review','Integration','Preparation','Data','Operations','Agent'].map((r)=>'<option value="'+r+'">').join('')+'</datalist>'+runtimeFields(item)+field('Workspace folder','directory',item.directory,'list="folder-options" placeholder="Folder on the agent\'s host"')+'<datalist id="folder-options">'+[...new Set(catalogue.map((s)=>s.directory))].map((p)=>'<option value="'+esc(p)+'">').join('')+'</datalist><div class="field"><label for="session-search">Link a saved session</label><input id="session-search" type="search" placeholder="Search conversations or folders…"><div id="selected-session"></div><div id="session-picker" class="session-picker"></div><p class="field-hint">Leave unlinked to plan a role. Link a session and workspace to open it.</p></div><div class="field"><label for="f-notes">Role notes</label><textarea id="f-notes" name="notes" maxlength="4000" placeholder="Responsibilities, scope, or handoff expectations…">'+esc(item.notes)+'</textarea></div><label class="checkbox-field"><input id="f-enabled" type="checkbox" '+(item.enabled?'checked':'')+'>Include in fleet launch</label>';
  $('session-search').addEventListener('input',renderPicker);renderPicker();
  api('/api/sessions').then((data)=>{catalogue=data.sessions;if(editing&&editing.type==='agent')renderPicker();}).catch((e)=>toast(e.message,true));
 }else if(type==='fleet'){
  $('editor-fields').innerHTML=field('Fleet name','name',item.name,'required maxlength="80" placeholder="e.g. Payments service"')+field('Description','description',item.description,'maxlength="600" placeholder="What this fleet works on"')+field('Terminal window name','windowName',item.windowName,'maxlength="80" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="Generated from the fleet name"')+'<p class="field-hint">Agents open together in this named Windows Terminal window or tmux session.</p>'+hostsField()+'<label class="checkbox-field"><input id="f-default" type="checkbox" '+(item.id===state.defaultFleetId?'checked':'')+'>Default fleet for the app and desktop launcher</label>';
 }else{
  const opts='<option value="">Choose an agent…</option>'+f.agents.map((a)=>'<option value="'+a.id+'">'+esc(a.name)+'</option>').join('');
  $('editor-fields').innerHTML='<div class="form-row"><div class="field"><label for="f-source">From</label><select id="f-source" required>'+opts+'</select></div><div class="field"><label for="f-target">To</label><select id="f-target" required>'+opts+'</select></div></div>'+field('Handoff label','label',item.label,'maxlength="60" placeholder="e.g. Ready for review"')+'<p class="field-hint">A planning connection between roles. No message or task is sent.</p>';
  $('f-source').value=item.source;$('f-target').value=item.target;
 }
 $('editor').showModal();$('editor-fields').querySelector('input,select')?.focus();
}
const backendNames={codex:'Codex','claude-code':'Claude Code'};
function runtimeLabel(a){return (backendNames[a.backend||'codex']||a.backend)+((a.hostId||'local')==='local'?'':' @ '+a.hostId);}
function runtimeFields(item){
 const hosts=[{id:'local',name:'This computer'},...(state.hosts||[])];
 return '<div class="form-row"><div class="field"><label for="f-backend">Agent backend</label><select id="f-backend">'+Object.entries(backendNames).map(([k,v])=>'<option value="'+k+'"'+((item.backend||'codex')===k?' selected':'')+'>'+v+'</option>').join('')+'</select></div><div class="field"><label for="f-host">Runs on</label><select id="f-host">'+hosts.map((h)=>'<option value="'+esc(h.id)+'"'+((item.hostId||'local')===h.id?' selected':'')+'>'+esc(h.name||h.id)+'</option>').join('')+'</select></div></div>';
}
function hostsField(){
 const lines=(state.hosts||[]).map((h)=>h.id+' '+h.url+' '+h.tokenEnv).join('\n');
 return '<div class="field"><label for="f-hosts">Remote fleet nodes (all fleets)</label><textarea id="f-hosts" rows="3" placeholder="build-box https://build-box.example:8790 BUILD_BOX_TOKEN">'+esc(lines)+'</textarea><p class="field-hint">One per line: ID, node URL, and the environment variable holding its token. Tokens are never stored here.</p></div>';
}
function parseHosts(text){
 return text.split('\n').map((l)=>l.trim()).filter(Boolean).map((l)=>{const [id,url,tokenEnv,...rest]=l.split(/\s+/);if(!id||!url||!tokenEnv||rest.length)throw new Error('Each remote node needs an ID, URL and token variable.');return {id,name:id,url,tokenEnv};});
}
function renderPicker(){
 if(!editing||editing.type!=='agent')return;
 const id=editing.item.sessionId,s=catalogue.find((s)=>sessionKey(s)===sessionKey(editing.item)),q=($('session-search')?.value||'').toLowerCase();
 $('selected-session').innerHTML=id?'<div class="selected-session">'+esc(s?s.name:'Linked session')+'<button type="button" class="text-button" id="unlink-session">Unlink</button><code>'+esc(id)+'</code></div>':'';
 const linked=new Set(current().agents.filter((a)=>a.id!==editing.item.id).map(sessionKey));
 const matches=catalogue.filter((s)=>!linked.has(sessionKey(s))&&(s.name+' '+s.directory).toLowerCase().includes(q)).slice(0,15);
 $('session-picker').innerHTML=matches.map((s)=>'<button type="button" class="session-option '+(sessionKey(editing.item)===sessionKey(s)?'selected':'')+'" data-pick-session="'+esc(sessionKey(s))+'">'+esc(s.name)+'<small>'+esc(runtimeLabel(s)+' · '+s.directory)+'</small></button>').join('')||'<div class="field-hint" style="padding:12px">No matching saved sessions.</div>';
}
async function saveEditor(event){
 event.preventDefault();if(!editing)return;const action=clone(editing),item=action.item;
 if(action.type==='agent'){item.name=$('f-name').value.trim();item.role=$('f-role').value.trim();item.directory=$('f-directory').value.trim();item.notes=$('f-notes').value.trim();item.enabled=$('f-enabled').checked;item.backend=$('f-backend').value;item.hostId=$('f-host').value;}
 else if(action.type==='fleet'){item.name=$('f-name').value.trim();item.description=$('f-description').value.trim();item.windowName=$('f-windowName').value.trim()||'codex-'+item.name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');}
 else{item.source=$('f-source').value;item.target=$('f-target').value;item.label=$('f-label').value.trim();}
 const makeDefault=action.type==='fleet'&&$('f-default').checked;let hosts=null;if(action.type==='fleet'){try{hosts=parseHosts($('f-hosts').value);}catch(e){toast(e.message,true);return;}}$('save-editor').disabled=true;
 try{
  await mutate((next)=>{
   if(hosts)next.hosts=hosts;if(action.type==='fleet'){if(action.id)next.fleets[next.fleets.findIndex((f)=>f.id===item.id)]=item;else next.fleets.push(item);if(makeDefault)next.defaultFleetId=item.id;}
   else updateFleet(next,(f)=>{const list=action.type==='agent'?f.agents:f.links;if(action.id)list[list.findIndex((a)=>a.id===item.id)]=item;else list.push(item);});
  },action.type==='link'?'Handoff saved.':action.type==='agent'?'Agent saved.':'Fleet saved.');
  if(action.type==='fleet')setFleet(item.id);$('editor').close();editing=null;
 }catch(e){toast(e.message,true);}finally{$('save-editor').disabled=false;}
}
async function deleteItem(){
 if(!editing?.id)return;const action=clone(editing);
 const prompt=action.type==='agent'?'Remove this agent from the fleet? Its Codex conversation will be kept.':action.type==='fleet'?'Delete this fleet configuration? Its Codex conversations will be kept.':'Remove this handoff connection?';
 if(!window.confirm(prompt))return;
 try{await mutate((next)=>{
  if(action.type==='fleet'){next.fleets=next.fleets.filter((f)=>f.id!==action.id);if(next.defaultFleetId===action.id)next.defaultFleetId=next.fleets[0].id;}
  else updateFleet(next,(f)=>{if(action.type==='agent'){f.agents=f.agents.filter((a)=>a.id!==action.id);f.links=f.links.filter((l)=>l.source!==action.id&&l.target!==action.id);forgetAgent(f,action.id);}else f.links=f.links.filter((l)=>l.id!==action.id);});
 },'Removed from the fleet.');$('editor').close();editing=null;selected.delete(action.id);}catch(e){toast(e.message,true);}
}
function exportFleet(){const blob=new Blob([JSON.stringify(current(),null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=current().windowName+'.json';a.click();URL.revokeObjectURL(url);}
async function importFile(file){
 if(!file)return;
 try{
  if(file.size>2000000)throw new Error('Choose a JSON file smaller than 2 MB.');
  const data=JSON.parse(await file.text()),raw=data.fleets||[data];
  const imported=raw.map((f,i)=>({id:uid(),name:f.name||'Imported fleet '+(i+1),description:f.description||'',windowName:f.windowName||'codex-import-'+uid().slice(0,8),agents:(f.agents||[]).map((a,j)=>({id:a.id||uid(),name:a.name,role:a.role||'Agent',roleIds:a.roleIds||[],notes:a.notes||'',sessionId:a.sessionId||'',directory:a.directory||'',backend:a.backend||'codex',hostId:a.hostId||'local',enabled:a.enabled!==false,position:a.position||{x:80+(j%3)*320,y:60+Math.floor(j/3)*200}})),links:f.links||[],roles:f.roles||[],roleLinks:f.roleLinks||[],roleSource:f.roleSource||null,backlog:f.backlog||null,triggers:(f.triggers||[]).map((t)=>({...t,id:uid(),enabled:false}))}));
  await mutate((next)=>next.fleets.push(...imported),'Fleet imported.');setFleet(imported[0].id);
 }catch(e){toast(e.message,true);}finally{$('import-file').value='';}
}

function wireEvents(){
 document.querySelectorAll('[data-view]').forEach((n)=>n.onclick=()=>setView(n.dataset.view));
 $('fleet-list').onclick=(e)=>{const n=e.target.closest('[data-fleet]');if(n)setFleet(n.dataset.fleet);};
 $('add-agent').onclick=()=>openEditor('agent');$('new-fleet').onclick=()=>openEditor('fleet');$('edit-fleet').onclick=()=>openEditor('fleet',activeId);
 $('search').oninput=()=>{query=$('search').value;renderContent();};$('status-filter').onchange=()=>{filter=$('status-filter').value;renderContent();};
 $('refresh').onclick=()=>refresh(true);$('open-fleet').onclick=()=>launch(current().agents.filter(launchable).map((a)=>a.id));
 $('open-selected').onclick=()=>launch([...selected]);$('clear-selection').onclick=()=>{selected.clear();renderContent();};
 $('content').onclick=(e)=>{
  const t=e.target.closest('button,[data-edit-link]');if(!t)return;
  if(t.dataset.edit)openEditor('agent',t.dataset.edit);if(t.dataset.open)launch([t.dataset.open]);
  if(t.dataset.editLink)openEditor('link',t.dataset.editLink);if(t.dataset.action==='add')openEditor('agent');
  if(t.dataset.addSession)openEditor('agent',null,catalogue.find((s)=>sessionKey(s)===t.dataset.addSession));
 };
 $('content').onchange=(e)=>{
  const t=e.target;if(t.dataset.select){if(t.checked)selected.add(t.dataset.select);else selected.delete(t.dataset.select);}
  if(t.id==='select-all')filteredAgents().filter(launchable).forEach((a)=>t.checked?selected.add(a.id):selected.delete(a.id));
  // Only selection changes need a redraw; redrawing on any input's change rebuilt forms before their submit click landed.
  if(t.dataset.select||t.id==='select-all')renderContent();
 };
 $('editor-form').onsubmit=saveEditor;$('delete-item').onclick=deleteItem;
 const close=()=>{$('editor').close();editing=null;};$('close-editor').onclick=close;$('cancel-editor').onclick=close;
 $('editor-fields').onclick=(e)=>{
  const pick=e.target.closest('[data-pick-session]');
  if(pick&&editing?.type==='agent'){const s=catalogue.find((s)=>sessionKey(s)===pick.dataset.pickSession),previous=catalogue.find((s)=>sessionKey(s)===sessionKey(editing.item));editing.item.sessionId=s.id;editing.item.backend=s.backend||'codex';editing.item.hostId=s.hostId||'local';$('f-backend').value=editing.item.backend;$('f-host').value=editing.item.hostId;if(!$('f-directory').value||$('f-directory').value===previous?.directory)$('f-directory').value=s.directory;if(!$('f-name').value)$('f-name').value=s.name.slice(0,80);renderPicker();}
  if(e.target.closest('#unlink-session')&&editing?.type==='agent'){editing.item.sessionId='';renderPicker();}
 };
 $('add-link').onclick=()=>{if(current().agents.length<2)return toast('Add at least two agents to create a handoff.');openEditor('link');};
 $('editor-fields').onchange=(e)=>{if(editing?.type==='agent'&&['f-backend','f-host'].includes(e.target.id)){editing.item.backend=$('f-backend').value;editing.item.hostId=$('f-host').value;editing.item.sessionId='';renderPicker();}};
 $('arrange').onclick=()=>mutate((next)=>updateFleet(next,(f)=>f.agents.forEach((a,i)=>a.position={x:80+(i%3)*320,y:60+Math.floor(i/3)*210})),'Board arranged.').catch((e)=>toast(e.message,true));
 $('zoom-in').onclick=()=>{zoom=Math.min(1.5,Math.round((zoom+.1)*10)/10);renderBoard();};
 $('zoom-out').onclick=()=>{zoom=Math.max(.2,Math.round((zoom-.1)*10)/10);renderBoard();};
 $('content').addEventListener('pointerdown',(e)=>{
  const node=e.target.closest('[data-node]');if(!node||e.target.closest('button')||e.button!==0)return;
  const a=current().agents.find((a)=>a.id===node.dataset.node);drag={id:a.id,node,origin:{...a.position},position:{...a.position},x:e.clientX,y:e.clientY,pointer:e.pointerId};
  node.setPointerCapture(e.pointerId);node.classList.add('dragging');e.preventDefault();
 });
 $('content').addEventListener('pointermove',(e)=>{
  if(!drag||drag.saving||drag.pointer!==e.pointerId)return;
  drag.position={x:Math.max(0,Math.min(4000,Math.round(drag.origin.x+(e.clientX-drag.x)/zoom))),y:Math.max(0,Math.min(4000,Math.round(drag.origin.y+(e.clientY-drag.y)/zoom)))};
  drag.node.style.left=drag.position.x+'px';drag.node.style.top=drag.position.y+'px';$('board-links').innerHTML=linksMarkup({...current(),agents:filteredAgents()});
 });
 async function finishDrag(e){
  if(!drag||drag.saving||drag.pointer!==e.pointerId)return;
  const change={id:drag.id,position:{...drag.position}},moved=change.position.x!==drag.origin.x||change.position.y!==drag.origin.y;drag.saving=true;
  try{if(moved)await mutate((next)=>updateFleet(next,(f)=>{f.agents.find((a)=>a.id===change.id).position=change.position;}));}
  catch(error){toast(error.message,true);}finally{drag=null;renderContent();}
 }
 $('content').addEventListener('pointerup',finishDrag);$('content').addEventListener('pointercancel',finishDrag);
 $('content').addEventListener('keydown',(e)=>{
  const node=e.target.closest('[data-node]');if(!node||e.target.closest('button')||!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key))return;
  e.preventDefault();const id=node.dataset.node,d={ArrowUp:[0,-20],ArrowDown:[0,20],ArrowLeft:[-20,0],ArrowRight:[20,0]}[e.key];
  mutate((next)=>updateFleet(next,(f)=>{const a=f.agents.find((a)=>a.id===id);a.position={x:Math.max(0,Math.min(4000,a.position.x+d[0])),y:Math.max(0,Math.min(4000,a.position.y+d[1]))};})).catch((e)=>toast(e.message,true));
 });
 $('about').onclick=()=>$('about-dialog').showModal();$('close-about').onclick=()=>$('about-dialog').close();$('export-fleet').onclick=exportFleet;
 $('import-fleet').onclick=()=>$('import-file').click();$('import-file').onchange=()=>importFile($('import-file').files[0]);
 $('stop-app').onclick=async()=>{try{await api('/api/shutdown',{});stopped=true;$('about-dialog').close();$('live-pill').textContent='App stopped';toast('Fleet Studio stopped. Your agent tabs are still running.');}catch(e){toast(e.message,true);}};
}
async function start(){
 icons();wireEvents();
 try{const d=await api('/api/bootstrap');state=d.state;token=d.token;monitor=d.status;catalogue=d.sessions;if(!state.fleets.some((f)=>f.id===activeId))activeId=state.defaultFleetId;if(!localStorage.getItem('fleet-view')&&!current().agents.length)view='roster';renderAll();setInterval(()=>refresh(),5000);setTimeout(()=>refresh(true),1000);}
 catch(e){$('content').innerHTML=empty('Could not open Fleet Studio',e.message,false);toast(e.message,true);}
}
document.addEventListener('DOMContentLoaded',start,{once:true});
