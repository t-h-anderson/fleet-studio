'use strict';
// Agent health: where agents are stuck, what they spend tokens on, and quick tweaks.
const healthCache=new Map(),automationCache=new Map(),turnCache=new Map(),rulesCache=new Map();
let healthExpanded='';
const HEALTH_LABELS={delegating:'Subagents working',looping:'Looping',burning:'Burning tokens',stalled:'Stalled','admin-heavy':'Admin-heavy',waiting:'Waiting',productive:'Productive',idle:'Idle',offline:'Offline','no-telemetry':'No telemetry',unknown:'Not observed yet'};
let healthSignature='',healthSort='problems';
// Thresholds behind each kind of signal, so the operator can see why it fired and tune it.
const THRESHOLD_HINTS={'silent-turn':s=>'threshold '+s.silentMinutes+' min','idle-with-work':s=>'threshold '+s.stallMinutes+' min',
 'failing-repeat':s=>'threshold '+s.loopRepeats+' runs',repeat:s=>'threshold '+(s.loopRepeats+2)+' runs',cycle:s=>'threshold '+s.loopRepeats+' cycles',
 'no-progress':s=>'threshold '+s.noProgressFailures+' failures over '+s.noProgressMinutes+' min',admin:s=>'threshold '+Math.round(s.adminShare*100)+'%',
 burn:s=>'threshold '+tokenCount(s.burnTokensPerHour)+'/h'};
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
 // A closed session is not stalled work an agent can resume; someone has to reopen it.
 const label=h.signals?.[0]?.kind==='closed-session'?'Needs reopening':HEALTH_LABELS[h.state]||h.state;
 return '<span class="health-badge health-'+h.state+'">'+esc(label)+'</span>';
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
  if(editingContent())return;
  const signature=fid+JSON.stringify(health.agents.map(a=>[a.agentId,a.health?.state]));
  if(['health','automation','throughput'].includes(view))renderContent();
  else if(view==='board'&&!roleDrag&&signature!==healthSignature)renderContent();
  healthSignature=signature;
 }catch(e){/* The status banner already reports a disconnected server. */}
}
async function loadTurns(agentId){
 const data=await api('/api/telemetry/turns?fleetId='+encodeURIComponent(activeId)+'&agentId='+encodeURIComponent(agentId));
 turnCache.set(agentId,data.turns);
 if(view==='health')renderContent();
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
 const roles=current().roles.filter(r=>(a.roleIds||[]).includes(r.id)),role=roles[0];
 const signal=h?.signals?.[0],settings=healthCache.get(activeId)?.settings,hint=signal&&settings&&THRESHOLD_HINTS[signal.kind]?' ('+THRESHOLD_HINTS[signal.kind](settings)+')':'';
 const expanded=healthExpanded===a.id;
 return '<tr class="health-row'+(h&&HEALTH_PROBLEMS.includes(h.state)?' is-stuck':'')+'"><td><button class="agent-name" data-health-expand="'+a.id+'" aria-expanded="'+expanded+'">'+esc(a.name)+'</button><div class="agent-folder">'+esc(role?.name||displayedRole(a))+' · '+esc(runtimeLabel(a))+'</div>'+(row.briefStale?'<span class="stage-tag" title="This agent\'s role, scope or session changed since the last deploy">Brief out of date</span>':'')+'</td>'+
  '<td>'+healthBadge(h)+(h?'<small>'+sinceLabel(h.since)+'</small>':'')+(paused?'<small>Paused until '+new Date(paused.until*1000).toLocaleTimeString()+'</small>':'')+'</td>'+
  '<td class="health-evidence">'+esc((signal?.message?signal.message+hint:'')||h?.polling||(h?.state==='no-telemetry'?'Studio cannot read this agent\'s transcript (remote, or no linked session); only session status is shown.':h?'—':'Waiting for the first observation'))+'</td>'+
  '<td>'+tokenCount(h?.tokensLastHour)+'<small>'+tokenCount(h?.tokens24h)+' / 24h</small></td>'+
  '<td>'+mixBar(h?.classes24h)+'<small>'+(h?h.calls24h+' calls · '+h.errors24h+' failed':'')+'</small></td>'+
  '<td>'+(h?'≈'+tokenCount(h.wasteTokens24h)+'<small>'+(h.tokens24h?Math.round(100*h.wasteTokens24h/h.tokens24h):0)+'% of 24h</small>':'—')+'</td>'+
  '<td><div class="row-actions">'+
   '<button class="button small" data-health-nudge="'+a.id+'" title="Send a message now" aria-label="Nudge '+esc(a.name)+'">Nudge</button>'+
   '<button class="button small" data-health-agent="'+a.id+'" title="Edit this agent\'s scope notes and roles">Edit scope</button>'+
   roles.map(r=>'<button class="button small" data-health-role="'+r.id+'" title="Edit the '+esc(r.name)+' goal and brief">Tweak '+esc(roles.length>1?r.name:'role')+'</button>').join('')+
   (paused?'<button class="button small" data-health-resume="'+a.id+'">Resume</button>':'')+
   '<button class="icon-button" data-agent-activity="'+a.id+'" aria-label="Activity for '+esc(a.name)+'" title="Read agent activity">'+icon('activity')+'</button>'+
  '</div></td></tr>'+
  (expanded?'<tr class="health-detail"><td colspan="7">'+(h?.signals?.length>1?'<ul>'+h.signals.map(s=>'<li><strong>'+esc(HEALTH_LABELS[s.state]||s.state)+'</strong> '+esc(s.message)+'</li>').join('')+'</ul>':'')+subagentMarkup(h?.subagents)+turnTimeline(a.id)+'</td></tr>':'');
}
function setupChecklist(data){
 const f=current(),status=data.deployment||{};
 const steps=[
  ['Add agents and link their sessions',f.agents.some(a=>a.sessionId),'roster'],
  ['Give each role a goal',f.roles.length&&f.roles.every(r=>r.kind==='subagent'||r.goal),'roles'],
  ['Deploy the workflow so agents get their brief',status.active&&!status.draftChanged,'workflow'],
  ['Connect your tracker so dispatch can hand out tickets',!!f.backlog?.projectUrl,'backlog'],
  ['Turn on recovery rules',(f.automations||[]).some(r=>r.enabled),'automation'],
 ];
 if(steps.every(s=>s[1]))return '';
 return '<section class="setup-checklist"><h3>Get your fleet working</h3><ol>'+steps.map(([label,done,target])=>'<li class="'+(done?'done':'')+'">'+(done?'✓ ':'')+esc(label)+(done?'':' <button class="text-button" data-view-link="'+target+'">Open</button>')+'</li>').join('')+'</ol></section>';
}
function thresholdPanel(data){
 const s=data.settings,p={quietMinutes:60,handoffMinutes:30,...(data.progressSettings||{})};
 const fields=[['stallMinutes','Idle with open work, minutes'],['silentMinutes','Working turn with no activity, minutes'],['loopRepeats','Repeats that count as a loop'],
  ['noProgressFailures','Failures before work counts as not converging'],['noProgressMinutes','…over at least, minutes'],['adminShare','Admin share of calls (0–1)'],
  ['burnTokensPerHour','Tokens per hour without work']];
 return '<details class="threshold-panel"><summary>Detection thresholds</summary><form id="threshold-form"><div class="form-row">'+
  fields.map(([k,l])=>field(l,'h-'+k,s[k],'type="number" step="any" data-health-key="'+k+'"')).join('')+'</div>'+
  '<p class="field-hint">Progress uses two older measures too: no report for '+field('Quiet after, minutes','p-quietMinutes',p.quietMinutes,'type="number"')+field('Hand-off overdue after, minutes','p-handoffMinutes',p.handoffMinutes,'type="number"')+'</p>'+
  '<button class="button primary small" type="submit">Save thresholds</button></form></details>';
}
function renderHealth(){
 const data=healthCache.get(activeId);
 if(!data){$('content').innerHTML='<div class="empty-state"><span class="loading-ring"></span><h3>Reading agent health</h3><p>Combining session status with turn and tool-call telemetry.</p></div>';loadHealth();return;}
 if(!current().agents.length){$('content').innerHTML=setupChecklist(data)+empty('No agents yet','Add an agent and link its Codex or Claude Code session; its health appears here as soon as it works.',true);return;}
 const needle=query.toLowerCase();
 const byProblem=(x,y)=>(HEALTH_PROBLEMS.includes(y.health?.state)-HEALTH_PROBLEMS.includes(x.health?.state))||x.name.localeCompare(y.name);
 const byWaste=(x,y)=>(y.health?.wasteTokens24h||0)-(x.health?.wasteTokens24h||0);
 const rows=data.agents.filter(r=>(r.name+' '+(r.health?.state||'')+' '+current().roles.filter(x=>r.roleIds.includes(x.id)).map(x=>x.name).join(' ')).toLowerCase().includes(needle))
  .sort(healthSort==='waste'?byWaste:byProblem);
 const counts={};data.agents.forEach(r=>{const s=r.health?.state||'no-telemetry';counts[s]=(counts[s]||0)+1;});
 const auto=automationData();
 $('visible-count').textContent=rows.length;
 $('content').innerHTML=
  setupChecklist(data)+
  (data.deployment?.paused?'<div class="backlog-notice">Workflow automation is paused, so no rule, hand-off or dispatch message is sent. <button class="text-button" data-view-link="workflow">Resume in Workflow</button></div>':'')+
  (auto.escalations.length?'<div class="backlog-notice">'+auto.escalations.length+' escalation(s) need you. <button class="text-button" data-view-link="automation">Review</button></div>':'')+
  '<div class="health-summary">'+['looping','stalled','admin-heavy','burning','waiting','productive','idle','offline','no-telemetry'].map(s=>'<div class="health-tile health-'+s+'"><span>'+HEALTH_LABELS[s]+'</span><strong>'+(counts[s]||0)+'</strong></div>').join('')+'</div>'+
  '<div class="table-wrap"><table class="health-table"><thead><tr><th>AGENT</th><th>HEALTH</th><th>WHY</th><th>TOKENS / H</th><th>CALL MIX 24H</th><th>WASTE 24H</th><th></th></tr></thead><tbody>'+
  rows.map(healthRow).join('')+'</tbody></table></div>'+
  thresholdPanel(data)+
  '<p class="field-hint">'+(healthSort==='waste'?'Sorted by waste. <button class="text-button" id="health-sort-problems">Sort by problems</button> ':'')+'Call mix: <span class="legend mix-work"></span>work <span class="legend mix-explore"></span>explore <span class="legend mix-admin"></span>admin <span class="legend mix-wait"></span>waiting <span class="legend mix-other"></span>other. Fresh tokens exclude cache reads. Waste is estimated from repeated, admin, polling and aborted calls.</p>';
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
  // Tweaks open in place so the operator keeps the health context.
  if(b.dataset.healthRole||b.dataset.healthAgent){
   openEditor(b.dataset.healthRole?'role':'agent',b.dataset.healthRole||b.dataset.healthAgent);
   // Briefs are frozen at deploy, so say when a tweak reaches the agent.
   $('editor-fields').insertAdjacentHTML('afterbegin','<p class="field-hint deploy-hint">Saved to the draft. Agents see the change after you deploy: use Review &amp; deploy at the top.</p>');
  }
  if(b.id==='health-sort-problems'){healthSort='problems';renderHealth();}
  if(b.dataset.healthResume){await api('/api/automation/resume',{fleetId:activeId,agentId:b.dataset.healthResume});await loadHealth();toast('Automatic deliveries resumed.');}
 }catch(error){toast(error.message,true);}
}
$('content').addEventListener('click',healthClick);
$('content').addEventListener('submit',async e=>{
 if(e.target.id!=='threshold-form')return;
 e.preventDefault();
 const health={};document.querySelectorAll('[data-health-key]').forEach(n=>{health[n.dataset.healthKey]=Number(n.value);});
 const quiet=Number($('f-p-quietMinutes').value),handoff=Number($('f-p-handoffMinutes').value);
 try{await mutate(next=>updateFleet(next,f=>{f.health=health;f.progress={...(f.progress||{}),quietMinutes:quiet,handoffMinutes:handoff};}),'Thresholds saved; they apply from the next check.');await loadHealth();}
 catch(error){toast(error.message,true);}
});
setInterval(()=>{if(state&&!stopped)loadHealth();},5000);

const healthOriginalRenderContent=renderContent;
let renderForced=false;
renderContent=function(){
 if(!state)return;
 // Background refreshes must not redraw a form someone is typing in; navigation and saves (renderAll) still do.
 if(!renderForced&&EDITABLE_VIEWS.includes(view)&&editingContent())return;
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
  automation:['Rules','Recovering stuck agents, dispatching tickets and limits. Hand-offs between roles are set on the Workflow screen.','Rules'],
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

function subagentMarkup(report){
 if(!report)return '';
 // An older Codex records no spawn edges: ordinary supervision continues, only risky actions wait.
 if(report.supported===false)return '<div class="info-block"><strong>Subagents</strong><p>Subagent tracking is unavailable on this Codex version; automatic goal recovery and automatic stops stay held.</p></div>';
 const children=report.children||[];
 return '<div class="info-block"><strong>Subagents: '+children.length+'</strong><p>'+(!report.known?'Activity is uncertain. Automatic intervention is held.':report.active+' working; '+report.uncertain+' need review.')+' Child tokens used (lifetime, separate from parent): '+tokenCount(report.tokensUsed)+'.</p>'+children.map(c=>'<p style="margin-left:'+Math.min(8,c.depth||1)*12+'px">'+esc(c.path||c.name)+' · '+esc(c.state)+' · '+tokenCount(c.tokensUsed)+' tokens</p>').join('')+'</div>';
}
