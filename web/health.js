'use strict';
// Agent health: where agents are stuck, what they spend tokens on, and quick tweaks.
const healthCache=new Map(),automationCache=new Map(),turnCache=new Map(),rulesCache=new Map();
let healthExpanded='';
const HEALTH_LABELS={looping:'Looping',burning:'Burning tokens',stalled:'Stalled','admin-heavy':'Admin-heavy',waiting:'Waiting',productive:'Productive',idle:'Idle',offline:'Offline','no-telemetry':'No telemetry',unknown:'Not observed yet'};
let healthSignature='';
const HEALTH_PROBLEMS=['looping','burning','stalled','admin-heavy'];
const CALL_CLASSES=['work','explore','admin','wait','other'];

function agentHealth(id){
 const rows=healthCache.get(activeId)?.agents||[];
 return rows.find(row=>row.agentId===id)?.health||null;
}
function healthState(id){return agentHealth(id)?.state||'unknown';}
function healthChipClass(id){return 'health-chip health-'+healthState(id);}
function healthTitle(id){
 const h=agentHealth(id);
 return h?' · '+(HEALTH_LABELS[h.state]||h.state)+(h.signals[0]?': '+h.signals[0].message:''):'';
}
function stuckCount(agents){return agents.filter(a=>HEALTH_PROBLEMS.includes(healthState(a.id))).length;}
function healthBadge(h){
 if(!h)return '<span class="health-badge health-unknown">No telemetry</span>';
 return '<span class="health-badge health-'+h.state+'">'+esc(HEALTH_LABELS[h.state]||h.state)+'</span>';
}
function tokenCount(n){
 n=Number(n)||0;
 return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n);
}
function sinceLabel(at){return at?elapsed(at):'—';}
function mixBar(classes){
 const total=CALL_CLASSES.reduce((sum,key)=>sum+(classes?.[key]||0),0);
 if(!total)return '<span class="muted">No tool calls</span>';
 return '<div class="mix-bar" title="'+CALL_CLASSES.map(k=>k+': '+(classes[k]||0)).join(', ')+'">'+
  CALL_CLASSES.filter(k=>classes[k]).map(k=>'<span class="mix-'+k+'" style="flex:'+classes[k]+'"></span>').join('')+'</div>';
}
function automationData(){return automationCache.get(activeId)||{fires:[],escalations:[],paused:[],throttled:[],outcomes:{},presets:[]};}

async function loadHealth(){
 if(!state||stopped)return;
 const fid=activeId;
 try{
  const [health,automation,rules]=await Promise.all([
   api('/api/health/agents?fleetId='+encodeURIComponent(fid)),
   api('/api/automation?fleetId='+encodeURIComponent(fid)),
   view==='automation'?api('/api/rules?fleetId='+encodeURIComponent(fid)):Promise.resolve(null)]);
  healthCache.set(fid,health);automationCache.set(fid,automation);if(rules)rulesCache.set(fid,rules);
  if(fid!==activeId)return;
  if(view==='throughput'&&typeof loadProgress==='function')loadProgress();
  // Never redraw under someone typing, and redraw the board only when a health state changed.
  if(typingInContent())return;
  const signature=fid+JSON.stringify(health.agents.map(a=>[a.agentId,a.health?.state]));
  if(['health','automation','throughput'].includes(view))renderContent();
  else if(view==='board'&&!drag&&!roleDrag&&signature!==healthSignature)renderContent();
  healthSignature=signature;
 }catch(e){/* The status banner already reports a disconnected server. */}
}
async function loadTurns(agentId){
 const data=await api('/api/telemetry/turns?fleetId='+encodeURIComponent(activeId)+'&agentId='+encodeURIComponent(agentId));
 turnCache.set(agentId,data.turns);
 if(view==='health')renderHealth();
}

function turnTimeline(agentId){
 const turns=turnCache.get(agentId);
 if(!turns)return '<p class="field-hint">Loading recent turns…</p>';
 if(!turns.length)return '<p class="field-hint">No turns recorded yet. Telemetry covers local Codex and Claude Code sessions.</p>';
 return '<ol class="turn-timeline">'+turns.slice(0,12).map(t=>{
  const fresh=(t.tokens?.input||0)+(t.tokens?.output||0)+(t.tokens?.cacheWrite||0);
  const calls=(t.calls||[]).slice(-24).map(c=>'<span class="call call-'+c.cls+(c.error?' call-error':'')+'" title="'+esc(c.tool+(c.error?' (failed)':''))+'">'+esc(c.label||c.tool)+'</span>').join('');
  return '<li><div class="turn-head"><strong>'+esc(t.status)+'</strong>'+(t.fromStudio?'<span class="stage-tag">Studio prompt</span>':'')+
   '<span>'+ago(t.startedAt)+' · '+t.callCount+' calls · '+tokenCount(fresh)+' fresh tokens · '+tokenCount(t.tokens?.cacheRead)+' cached'+(t.errors?' · '+t.errors+' failed':'')+'</span></div>'+
   '<div class="turn-calls">'+(calls||'<span class="muted">No tool calls</span>')+'</div></li>';
 }).join('')+'</ol>';
}
function healthRow(row){
 const a=current().agents.find(x=>x.id===row.agentId),h=row.health,paused=automationData().paused.find(p=>p.agentId===row.agentId);
 if(!a)return '';
 const role=current().roles.find(r=>(a.roleIds||[]).includes(r.id));
 const signal=h?.signals?.[0];
 const expanded=healthExpanded===a.id;
 return '<tr class="health-row'+(h&&HEALTH_PROBLEMS.includes(h.state)?' is-stuck':'')+'"><td><button class="agent-name" data-health-expand="'+a.id+'" aria-expanded="'+expanded+'">'+esc(a.name)+'</button><div class="agent-folder">'+esc(role?.name||displayedRole(a))+' · '+esc(runtimeLabel(a))+'</div></td>'+
  '<td>'+healthBadge(h)+(h?'<small>'+sinceLabel(h.since)+'</small>':'')+(paused?'<small>Paused until '+new Date(paused.until*1000).toLocaleTimeString()+'</small>':'')+'</td>'+
  '<td class="health-evidence">'+esc(signal?.message||h?.polling||(h?.state==='no-telemetry'?'Studio cannot read this agent\'s transcript (remote, or no linked session); only session status is shown.':h?'—':'Waiting for the first observation'))+'</td>'+
  '<td>'+tokenCount(h?.tokensLastHour)+'<small>'+tokenCount(h?.tokens24h)+' / 24h</small></td>'+
  '<td>'+mixBar(h?.classes24h)+'<small>'+(h?h.calls24h+' calls · '+h.errors24h+' failed':'')+'</small></td>'+
  '<td>'+(h?tokenCount(h.wasteTokens24h)+'<small>'+(h.tokens24h?Math.round(100*h.wasteTokens24h/h.tokens24h):0)+'% of 24h</small>':'—')+'</td>'+
  '<td><div class="row-actions">'+
   '<button class="button small" data-health-nudge="'+a.id+'" title="Send a message now" aria-label="Nudge '+esc(a.name)+'">Nudge</button>'+
   (role?'<button class="button small" data-health-role="'+role.id+'" title="Edit the role goal and brief">Tweak role</button>':'')+
   (paused?'<button class="button small" data-health-resume="'+a.id+'">Resume</button>':'')+
   '<button class="icon-button" data-agent-activity="'+a.id+'" aria-label="Activity for '+esc(a.name)+'" title="Read agent activity">'+icon('activity')+'</button>'+
  '</div></td></tr>'+
  (expanded?'<tr class="health-detail"><td colspan="7">'+(h?.signals?.length>1?'<ul>'+h.signals.map(s=>'<li><strong>'+esc(HEALTH_LABELS[s.state]||s.state)+'</strong> '+esc(s.message)+'</li>').join('')+'</ul>':'')+turnTimeline(a.id)+'</td></tr>':'');
}
function renderHealth(){
 const data=healthCache.get(activeId);
 if(!data){$('content').innerHTML='<div class="empty-state"><span class="loading-ring"></span><h3>Reading agent health</h3><p>Combining session status with turn and tool-call telemetry.</p></div>';loadHealth();return;}
 const needle=query.toLowerCase();
 const rows=data.agents.filter(r=>(r.name+' '+(r.health?.state||'')).toLowerCase().includes(needle))
  .sort((x,y)=>(HEALTH_PROBLEMS.includes(y.health?.state)-HEALTH_PROBLEMS.includes(x.health?.state))||x.name.localeCompare(y.name));
 const counts={};data.agents.forEach(r=>{const s=r.health?.state||'no-telemetry';counts[s]=(counts[s]||0)+1;});
 const auto=automationData();
 $('visible-count').textContent=rows.length;
 $('content').innerHTML=
  (auto.escalations.length?'<div class="backlog-notice">'+auto.escalations.length+' escalation(s) need you. <button class="text-button" data-view-link="automation">Review</button></div>':'')+
  '<div class="health-summary">'+['looping','stalled','admin-heavy','burning','waiting','productive','idle','offline','no-telemetry'].map(s=>'<div class="health-tile health-'+s+'"><span>'+HEALTH_LABELS[s]+'</span><strong>'+(counts[s]||0)+'</strong></div>').join('')+'</div>'+
  '<div class="table-wrap"><table class="health-table"><thead><tr><th>AGENT</th><th>HEALTH</th><th>WHY</th><th>TOKENS / H</th><th>CALL MIX 24H</th><th>WASTE 24H</th><th></th></tr></thead><tbody>'+
  rows.map(healthRow).join('')+'</tbody></table></div>'+
  '<p class="field-hint">Call mix: <span class="legend mix-work"></span>work <span class="legend mix-explore"></span>explore <span class="legend mix-admin"></span>admin <span class="legend mix-wait"></span>waiting <span class="legend mix-other"></span>other. Fresh tokens exclude cache reads. Waste is estimated from repeated, admin, polling and aborted calls.</p>';
}
async function healthClick(e){
 if(!['health','automation','throughput'].includes(view))return;
 const b=e.target.closest('button');if(!b)return;
 try{
  if(b.dataset.viewLink)setView(b.dataset.viewLink);
  if(b.dataset.healthExpand){healthExpanded=healthExpanded===b.dataset.healthExpand?'':b.dataset.healthExpand;renderHealth();if(healthExpanded)await loadTurns(healthExpanded);}
  if(b.dataset.healthNudge){
   const h=agentHealth(b.dataset.healthNudge),evidence=h?.signals?.[0]?.message;
   await showControl('message',[b.dataset.healthNudge],'[Fleet Studio] '+(evidence?evidence+'\n\n':'')+'Step back, state the next concrete action toward your goal, and take it.');
  }
  if(b.dataset.healthRole){setView('roles');openEditor('role',b.dataset.healthRole);}
  if(b.dataset.healthResume){await api('/api/automation/resume',{fleetId:activeId,agentId:b.dataset.healthResume});await loadHealth();toast('Automatic deliveries resumed.');}
 }catch(error){toast(error.message,true);}
}
$('content').addEventListener('click',healthClick);
setInterval(()=>{if(state&&!stopped)loadHealth();},5000);

const healthOriginalRenderContent=renderContent;
function typingInContent(){return !!document.activeElement?.closest('#content input,#content textarea,#content select');}
let renderForced=false;
renderContent=function(){
 if(!state||drag)return;
 // Background refreshes must not redraw a form someone is typing in; navigation and saves (renderAll) still do.
 if(!renderForced&&['health','automation','throughput'].includes(view)&&typingInContent())return;
 if(view==='health')return renderHealth();
 if(view==='automation')return renderAutomation();
 if(view==='throughput')return renderThroughput();
 healthOriginalRenderContent();
};
const healthOriginalRenderAll=renderAll;
renderAll=function(){
 renderForced=true;
 try{healthOriginalRenderAll();}finally{renderForced=false;}
 if(!state)return;
 const views={
  health:['Agent health','Where agents are stuck, what they spend tokens on, and quick tweaks.','Agents'],
  automation:['Rules','Everything that messages an agent: routing work between roles, recovering stuck agents, and dispatching tickets.','Rules'],
  throughput:['Throughput','Work delivered, tokens spent, waste and throttling.','Flow and waste'],
 };
 if(!views[view])return;
 const [title,description,panel]=views[view];
 document.querySelector('main > .metrics').classList.add('hidden');
 $('fleet-title').textContent=title;$('fleet-description').textContent=description;
 $('view-eyebrow').textContent=current().name;$('panel-title').textContent=panel;
 $('workspace-layout').classList.add('board-layout');$('status-filter').classList.add('hidden');$('board-toolbar').classList.add('hidden');
 $('search').placeholder=view==='health'?'Find an agent or state…':'Find a rule…';
 $('panel-footer-text').textContent='Health refreshes every 5 seconds from session telemetry.';
 if(!healthCache.has(activeId)||view==='automation'&&!rulesCache.has(activeId))loadHealth();
 if(view==='throughput'&&typeof loadProgress==='function'&&!progressCache.has(activeId))loadProgress();
};
