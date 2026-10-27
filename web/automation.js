'use strict';
// Health automations, throttles and the throughput view.
const SIGNAL_LABELS={looping:'Looping',stalled:'Stalled','admin-heavy':'Admin-heavy',burning:'Burning tokens',waiting:'Waiting on a blocker','any-problem':'Any problem'};
const ACTION_LABELS={reprompt:'Reprompt with evidence',handoff:'Reassign the work to a healthy agent','stop-turn':'Stop the running turn',stop:'Stop the turn and mute Studio messages',pause:'Mute Studio messages to the agent',escalate:'Escalate to me',none:'Do nothing more'};
const backtests=new Map();

function roleLabel(id){return id?(current().roles.find(r=>r.id===id)?.name||'Missing role'):'All roles';}
function ruleSummary(r){
 const agent=r.agentId&&current().agents.find(a=>a.id===r.agentId);
 return 'When '+(agent?esc(agent.name)+' is ':roleLabel(r.roleId).toLowerCase()+' '+(r.roleId?'agents are ':'are '))+SIGNAL_LABELS[r.when.signal].toLowerCase()+
  (r.when.forMinutes?' for '+r.when.forMinutes+' min':'')+': '+ACTION_LABELS[r.action].toLowerCase()+
  (r.action==='handoff'?' in '+roleLabel(r.handoffRoleId||r.roleId):'')+
  '. At most '+(r.maxFires||'unlimited')+(r.maxFires?' time(s)':'')+' per agent in '+(r.windowHours||24)+' h, '+(r.cooldownMinutes||15)+' min apart'+
  (r.maxFires&&r.thenAction!=='none'?', then '+ACTION_LABELS[r.thenAction].toLowerCase():'')+'.';
}
function ruleCard(r){
 const test=backtests.get(r.id);
 return '<article class="trigger-card automation-card"><div><h3>'+esc(r.name)+'</h3><span class="role-state '+(r.enabled?'':'draft')+'">'+(r.enabled?'Running':'Off')+'</span></div>'+
  '<p>'+esc(ruleSummary(r))+'</p>'+
  (test?'<p class="field-hint">Backtest: would have acted <strong>'+test.fires+'</strong> time(s) in the last '+test.hours+' h'+(test.fallbacks?', with '+test.fallbacks+' fallback(s)':'')+'.</p>':'')+
  lastFireLine(automationData().lastFire?.[r.id])+
  '<div class="row-actions"><button class="button small" data-automation-toggle="'+r.id+'">'+(r.enabled?'Turn off':'Turn on')+'</button><button class="button small" data-automation-backtest="'+r.id+'">Backtest 24 h</button><button class="button small" data-automation-edit="'+r.id+'">Edit</button></div></article>';
}
function lastFireLine(f){
 if(!f)return '<p class="field-hint">Has not acted yet.</p>';
 return '<p class="field-hint">Last: '+esc(f.action)+' for '+esc(f.agentName)+' '+ago(f.at)+' · <strong>'+esc(f.status)+'</strong>'+(f.detail?' · '+esc(f.detail):'')+'</p>';
}
function fireRow(f){
 return '<tr><td>'+ago(f.at)+'</td><td>'+esc(f.agentName)+(f.recipientName?' → '+esc(f.recipientName):'')+'</td><td>'+esc(f.ruleName)+'<small>'+esc(f.evidence)+'</small></td><td>'+esc(f.action)+(f.fallback?' <span class="stage-tag">fallback</span>':'')+'</td><td>'+esc(f.status)+'<small>'+esc(f.detail||'')+'</small></td><td>'+esc(f.outcome||'—')+'</td></tr>';
}
function throttleForm(){
 const t=current().throttles||{},alerts=current().alerts||{};
 const roleLimits=t.roleTokensPerHour||{};
 return '<form class="throttle-form" id="throttle-form"><h3>Throttles and alerts</h3><div class="form-row">'+
  field('Max agents working at once','maxWorkingAgents',t.maxWorkingAgents??'','type="number" min="1" max="500" placeholder="No limit"')+
  field('Fleet tokens per hour','fleetTokensPerHour',t.fleetTokensPerHour??'','type="number" min="1000" placeholder="No limit"')+'</div>'+
  '<div class="form-row">'+current().roles.filter(r=>r.kind!=='subagent').map(r=>field(esc(r.name)+' tokens per hour','role-'+r.id,roleLimits[r.id]??'','type="number" min="1000" placeholder="No limit" data-role-limit="'+r.id+'"')).join('')+'</div>'+
  field('Escalation webhook (Slack-compatible JSON)','webhookUrl',alerts.webhookUrl||'','type="url" placeholder="https://hooks.example/…"')+
  '<p class="field-hint">Automatic sends that would exceed a limit wait, and the waiting time is shown in Throughput.</p><button class="button primary small" type="submit">Save limits</button></form>';
}
function renderAutomation(){
 const f=current(),rules=(f.automations||[]).filter(r=>(r.name+' '+r.when.signal).toLowerCase().includes(query.toLowerCase())),data=automationData();
 $('visible-count').textContent=rules.length;
 $('content').innerHTML=
  (data.workflowPaused?'<div class="backlog-notice">Workflow automation is paused, so these automations are paused too. Resume it from Progress or Workflow.</div>':'')+
  (data.escalations.length?'<section class="escalations"><h3>Needs you</h3>'+data.escalations.map(e=>'<div class="progress-attention"><span class="stage-tag">'+esc(e.ruleName)+'</span><strong>'+esc(e.agentName)+'</strong><span>'+esc(e.message)+'</span><button class="button small" data-escalation-ack="'+e.id+'" aria-label="Dismiss escalation for '+esc(e.agentName)+'">Done</button></div>').join('')+'</section>':'')+
  '<div class="definition-toolbar"><div><strong>Recovery rules act on health, not on a schedule.</strong><p>Changes apply on the next check; no deployment is needed. Each rule limits itself, then falls back once.</p></div><div>'+
  (data.presets?.length?'<label class="checkbox-field">For <select id="automation-preset-role" aria-label="Role for recommended automations"><option value="">all roles</option>'+current().roles.filter(r=>r.kind!=='subagent').map(r=>'<option value="'+r.id+'">'+esc(r.name)+'</option>').join('')+'</select></label><button class="button small" id="automation-presets">Add recommended (off)</button>':'')+'<button class="button small" id="automation-new">'+icon('plus')+'New automation</button></div></div>'+
  '<h3>Recovery</h3>'+
  (rules.length?'<div class="roles-grid">'+rules.map(ruleCard).join('')+'</div>':'<p class="field-hint">No recovery rules yet. Add the recommended set to reprompt loops and stalls, refocus admin-heavy agents and escalate token burn.</p>')+
  routingSection()+dispatchSection()+
  throttleForm()+
  timelineSection(data);
}

function routingSection(){
 const r=rulesCache.get(activeId);
 const rows=(r?.routing||[]).filter(x=>(x.name+' '+x.when+' '+x.to).toLowerCase().includes(query.toLowerCase()));
 return '<h3>Routing</h3><p class="field-hint">Moves work between roles when an agent reports a hand-off. Routing belongs to the workflow, so changes reach agents\' briefs after you deploy. Timers and idle checks only reach agents that have open work; dispatch hands out new tickets.</p>'+
  '<div class="table-wrap"><table><thead><tr><th>RULE</th><th>WHEN</th><th>TO</th><th>DELIVERY</th><th>STATE</th><th></th></tr></thead><tbody>'+
  (rows.length?rows.map(x=>'<tr><td>'+esc(x.name)+'</td><td>'+esc(x.when)+'</td><td>'+esc(x.to)+'</td><td>'+(x.mode==='review'?'Waits for review':'Automatic')+'</td><td>'+(x.running?'Running':x.enabled?'On in draft':'Off')+(x.note?'<small>'+esc(x.note)+'</small>':'')+'</td><td><button class="button small" data-routing-edit="'+x.id+'" aria-label="Edit '+esc(x.name)+'">Edit</button></td></tr>').join(''):'<tr><td colspan="6" class="muted">No routing rules. Add hand-offs on the Design board, then create rules from them here.</td></tr>')+
  '</tbody></table></div><div class="row-actions"><button class="button small" id="routing-new">'+icon('plus')+'New routing rule</button><button class="button small" id="routing-populate">Create from hand-offs</button></div>';
}
function dispatchSection(){
 const d=rulesCache.get(activeId)?.dispatch||current().dispatch||{enabled:true,onDeploy:true};
 return '<h3>Dispatch</h3><p class="field-hint">Gives agents their next piece of work: finished work moves to the next role, and an idle agent with nothing open is offered the oldest unclaimed ticket its role picks up. Nothing is sent when no ticket is waiting.</p>'+
  '<label class="checkbox-field"><input type="checkbox" id="dispatch-enabled" '+(d.enabled?'checked':'')+'>Offer waiting tickets to idle agents</label>'+
  '<label class="checkbox-field"><input type="checkbox" id="dispatch-deploy" '+(d.onDeploy?'checked':'')+'>Send every agent its brief when I deploy</label>';
}
function timelineSection(data){
 const rows=rulesCache.get(activeId)?.timeline;
 if(!rows)return '<h3>Timeline</h3><p class="field-hint">Loading…</p>';
 return '<h3>Timeline</h3><div class="table-wrap"><table><thead><tr><th>WHEN</th><th>FROM</th><th>AGENT</th><th>RULE</th><th>ACTION</th><th>DELIVERY</th><th>OUTCOME</th></tr></thead><tbody>'+
  (rows.length?rows.slice(0,60).map(t=>'<tr><td>'+ago(t.at)+'</td><td>'+esc(t.source)+'</td><td>'+esc(t.agent)+'</td><td>'+esc(t.rule)+(t.cause?'<small>'+esc(t.cause)+'</small>':'')+'</td><td>'+esc(t.action)+'</td><td>'+esc(t.status)+(t.detail?'<small>'+esc(t.detail)+'</small>':'')+'</td><td>'+esc(t.outcome||'—')+'</td></tr>').join(''):'<tr><td colspan="7" class="muted">No rule has messaged an agent yet.</td></tr>')+'</tbody></table></div>';
}
function wasteBar(waste){
 const kinds=['loop','admin','wait','aborted','nudge'],total=kinds.reduce((s,k)=>s+(waste[k]||0),0);
 if(!total)return '<p class="field-hint">No waste estimated in the last 24 h.</p>';
 return '<div class="mix-bar waste-bar">'+kinds.filter(k=>waste[k]).map(k=>'<span class="waste-'+k+'" style="flex:'+waste[k]+'" title="'+k+': '+tokenCount(waste[k])+'"></span>').join('')+'</div>'+
  '<p class="field-hint">'+kinds.filter(k=>waste[k]).map(k=>'<span class="legend waste-'+k+'"></span>'+{loop:'repeated steps',admin:'reporting and tracker admin',wait:'polling and sleeping',aborted:'abandoned turns',nudge:'reprompts that produced no work'}[k]+' '+tokenCount(waste[k])).join(' · ')+'</p>';
}
function renderThroughput(){
 const health=healthCache.get(activeId),progress=typeof progressData==='function'?progressData():null,auto=automationData();
 if(!health){$('content').innerHTML='<div class="empty-state"><span class="loading-ring"></span><h3>Measuring flow</h3></div>';return;}
 const rows=health.agents.map(r=>r.health).filter(Boolean);
 const sum=key=>rows.reduce((s,h)=>s+(h[key]||0),0);
 const waste={};rows.forEach(h=>Object.entries(h.waste24h||{}).forEach(([k,v])=>waste[k]=(waste[k]||0)+v));
 const tokens=sum('tokens24h'),wasted=sum('wasteTokens24h'),closed=progress&&!progress.error?progress.summary.completed24h:null;
 const recovered=auto.outcomes?.recovered||0;
 const tiles=[
  ['Tickets closed · 24h',progress&&!progress.error&&!progress.tracker?.url?'Connect tracker':closed??'—'],
  ['Fresh tokens · 24h',tokenCount(tokens)],
  ['Estimated waste · 24h',tokenCount(wasted)+(tokens?' ('+Math.round(100*wasted/tokens)+'%)':'')],
  ['Tokens per closed ticket',closed?tokenCount(Math.round(tokens/closed)):'—'],
  ['Throttled · 24h',Math.round((auto.throttledSeconds24h||0)/60)+' agent-min'],
  ['Recovery that helped · 24h',auto.delivered?recovered+' of '+auto.delivered+' judged':'—'],
 ];
 $('visible-count').textContent=current().roles.length;
 $('content').innerHTML='<div class="progress-metrics throughput-metrics">'+tiles.map(([l,v])=>'<div><span>'+l+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>'+
  '<h3>Where tokens are wasted</h3>'+wasteBar(waste)+(wasted?'<p><button class="text-button" data-waste-agents>See which agents waste the most</button></p>':'')+
  '<h3>By role</h3><div class="table-wrap"><table><thead><tr><th>ROLE</th><th>AGENTS</th><th>STUCK NOW</th><th>OPEN QUEUE</th><th>FRESH TOKENS 24H</th><th>WASTE</th><th>CALL MIX</th></tr></thead><tbody>'+
  current().roles.filter(r=>r.kind!=='subagent').map(role=>{
   const agents=current().agents.filter(a=>(a.roleIds||[]).includes(role.id)),hs=agents.map(a=>agentHealth(a.id)).filter(Boolean);
   const t=hs.reduce((s,h)=>s+h.tokens24h,0),w=hs.reduce((s,h)=>s+h.wasteTokens24h,0),mix={};
   hs.forEach(h=>CALL_CLASSES.forEach(k=>mix[k]=(mix[k]||0)+(h.classes24h?.[k]||0)));
   const queue=progress&&!progress.error?progress.roles.find(r=>r.id===role.id)?.queueCount:null;
   return '<tr><td><button class="text-button" data-role-health="'+esc(role.name)+'" title="Show these agents in Agent health">'+esc(role.name)+'</button></td><td>'+agents.length+'</td><td>'+(stuckCount(agents)?'<span class="health-badge health-stalled">'+stuckCount(agents)+'</span>':'0')+'</td><td>'+(queue??'—')+'</td><td>'+tokenCount(t)+'</td><td>'+(t?Math.round(100*w/t)+'%':'—')+'</td><td>'+mixBar(mix)+'</td></tr>';
  }).join('')+'</tbody></table></div>'+
  (current().roles.length?'':'<p class="field-hint">Define roles to see flow by role.</p>')+
  '<p class="field-hint">Closures come from the tracker; tokens and waste from local session telemetry. Fresh tokens are input, output and cache writes, excluding cache reads, the same measure as Agent health. Tokens per ticket divides all fresh tokens by closures, so it is an indicator, not a cost attribution.</p>';
}

// Automation rule editor, sharing the existing editor dialog.
const automationOriginalOpenEditor=openEditor;
openEditor=function(type,id=null,preSession=null){
 if(type!=='automation')return automationOriginalOpenEditor(type,id,preSession);
 const f=current(),existing=(f.automations||[]).find(r=>r.id===id);
 const item=existing?clone(existing):{id:uid(),name:'',enabled:true,roleId:'',when:{signal:'stalled',forMinutes:0},action:'reprompt',thenAction:'escalate',message:'',handoffRoleId:'',parkSource:true,cooldownMinutes:15,maxFires:3,windowHours:24,pauseMinutes:60};
 editing={type,id,item};
 $('editor-title').textContent=(id?'Edit ':'New ')+'automation';$('editor-eyebrow').textContent='HEALTH AUTOMATION';
 $('delete-item').classList.toggle('hidden',!id);$('delete-item').disabled=false;$('delete-item').textContent='Remove automation';
 const options=(labels,selected)=>Object.entries(labels).map(([k,v])=>'<option value="'+k+'"'+(k===selected?' selected':'')+'>'+v+'</option>').join('');
 const roles=(selected,empty)=>'<option value="">'+empty+'</option>'+f.roles.map(r=>'<option value="'+r.id+'"'+(r.id===selected?' selected':'')+'>'+esc(r.name)+'</option>').join('');
 const canStop=f.agents.some(a=>(a.hostId||'local')==='local');
 const actions=Object.fromEntries(Object.entries(ACTION_LABELS).filter(([k])=>k!=='none'&&(k!=='stop-turn'||canStop||item.action==='stop-turn')));
 $('editor-fields').innerHTML=field('Name','name',item.name,'required maxlength="100"')+
  '<div class="form-row"><div class="field"><label for="f-auto-role">Watch agents in</label><select id="f-auto-role">'+roles(item.roleId,'All roles')+'</select></div>'+
  '<div class="field"><label for="f-auto-agent">Only this agent (optional)</label><select id="f-auto-agent"><option value="">Every agent in the role</option>'+f.agents.map(a=>'<option value="'+a.id+'"'+(a.id===item.agentId?' selected':'')+'>'+esc(a.name)+'</option>').join('')+'</select></div>'+
  '<div class="field"><label for="f-auto-signal">When an agent is</label><select id="f-auto-signal">'+options(SIGNAL_LABELS,item.when.signal)+'</select></div>'+
  field('For at least (minutes)','auto-for',item.when.forMinutes,'type="number" min="0" max="10080"')+'</div>'+
  '<div class="form-row"><div class="field"><label for="f-auto-action">Then</label><select id="f-auto-action">'+options(actions,item.action)+'</select></div>'+
  '<div class="field"><label for="f-auto-handoff">Hand off to</label><select id="f-auto-handoff">'+roles(item.handoffRoleId,'Same role')+'</select></div></div>'+
  textarea('Message template (optional)','f-auto-message',item.message,4000)+
  '<p class="field-hint">Leave empty for a message tailored to the signal. Placeholders: {agent}, {goal}, {evidence}, {ticket}, {state}, {minutes}.</p>'+
  '<div class="form-row">'+field('Cooldown (minutes)','auto-cooldown',item.cooldownMinutes,'type="number" min="1" max="10080"')+field('Max times per agent (0 = unlimited)','auto-max',item.maxFires,'type="number" min="0" max="1000"')+field('Within (hours)','auto-window',item.windowHours,'type="number" min="1" max="720"')+'</div>'+
  '<div class="form-row"><div class="field"><label for="f-auto-then">After the limit</label><select id="f-auto-then">'+options({escalate:ACTION_LABELS.escalate,handoff:ACTION_LABELS.handoff,stop:ACTION_LABELS.stop,pause:ACTION_LABELS.pause,none:ACTION_LABELS.none},item.thenAction)+'</select></div>'+
  field('Pause length (minutes)','auto-pause',item.pauseMinutes,'type="number" min="1" max="10080"')+'</div>'+
  '<label class="checkbox-field"><input id="f-auto-park" type="checkbox" '+(item.parkSource?'checked':'')+'>Pause the stuck agent after handing off its work</label>'+
  '<label class="checkbox-field"><input id="f-auto-enabled" type="checkbox" '+(item.enabled?'checked':'')+'>Running</label>';
 $('editor').showModal();
};
const automationOriginalSaveEditor=saveEditor;
saveEditor=async function(event){
 if(!editing||editing.type!=='automation')return automationOriginalSaveEditor(event);
 event.preventDefault();
 const item=clone(editing.item),number=id=>Number($(id).value);
 const agentId=$('f-auto-agent').value;if(agentId)item.agentId=agentId;else delete item.agentId;
 Object.assign(item,{name:$('f-name').value.trim(),roleId:$('f-auto-role').value,action:$('f-auto-action').value,handoffRoleId:$('f-auto-handoff').value,
  message:$('f-auto-message').value.trim(),thenAction:$('f-auto-then').value,parkSource:$('f-auto-park').checked,enabled:$('f-auto-enabled').checked,
  cooldownMinutes:number('f-auto-cooldown'),maxFires:number('f-auto-max'),windowHours:number('f-auto-window'),pauseMinutes:number('f-auto-pause'),
  when:{signal:$('f-auto-signal').value,forMinutes:number('f-auto-for')}});
 $('save-editor').disabled=true;
 try{
  await saveAutomations(list=>{const i=list.findIndex(r=>r.id===item.id);if(i>=0)list[i]=item;else list.push(item);},'Automation saved.');
  $('editor').close();editing=null;
 }catch(e){toast(e.message,true);}finally{$('save-editor').disabled=false;}
};
const automationOriginalDeleteItem=deleteItem;
deleteItem=async function(){
 if(!editing||editing.type!=='automation')return automationOriginalDeleteItem();
 if(!window.confirm('Remove this recovery rule? Its history stays in the timeline.'))return;
 const id=editing.id;
 try{await saveAutomations(list=>list.splice(list.findIndex(r=>r.id===id),1),'Automation removed.');$('editor').close();editing=null;}
 catch(e){toast(e.message,true);}
};
function saveAutomations(change,message){
 return mutate(next=>updateFleet(next,f=>{f.automations??=[];change(f.automations);}),message);
}
async function automationClick(e){
 if(!['automation','throughput'].includes(view))return;
 const b=e.target.closest('button');if(!b)return;
 try{
  if(b.id==='automation-new')openEditor('automation');
  if(b.id==='automation-presets'){
   // Presets arrive switched off with a backtest, so the operator sees what they would do first.
   const roleId=$('automation-preset-role')?.value||'',suffix=roleId?' · '+roleLabel(roleId):'';
   const presets=automationData().presets.map(p=>({...clone(p),name:p.name+suffix,id:uid(),enabled:false,roleId,message:'',handoffRoleId:'',parkSource:true,windowHours:24,pauseMinutes:60}));
   const existing=new Set((current().automations||[]).map(r=>r.name));
   const added=presets.filter(p=>!existing.has(p.name));
   await saveAutomations(list=>list.push(...added),'Recommended automations added, switched off. Check the backtests, then turn on the ones you want.');
   await Promise.allSettled(added.map(async preset=>{
    const saved=current().automations.find(r=>r.id===preset.id);
    if(saved)backtests.set(saved.id,await api('/api/automation/backtest',{fleetId:activeId,rule:saved,hours:24}));
   }));
   renderAutomation();
  }
  if(b.dataset.automationEdit)openEditor('automation',b.dataset.automationEdit);
  if(b.dataset.automationToggle)await saveAutomations(list=>{const r=list.find(r=>r.id===b.dataset.automationToggle);r.enabled=!r.enabled;},'Automation updated.');
  if(b.dataset.automationBacktest){
   const rule=current().automations.find(r=>r.id===b.dataset.automationBacktest);
   backtests.set(rule.id,await api('/api/automation/backtest',{fleetId:activeId,rule,hours:24}));renderAutomation();
  }
  if(b.hasAttribute('data-waste-agents')){healthSort='waste';setView('health');}
  if(b.dataset.roleHealth){setView('health');query=b.dataset.roleHealth;$('search').value=query;renderContent();}
  if(b.dataset.routingEdit)openEditor('trigger',b.dataset.routingEdit);
  if(b.id==='routing-new')openEditor('trigger');
  if(b.id==='routing-populate'){const data=await api('/api/workflow/populate',{fleetId:activeId,revision:state.revision});state=data.state;await loadHealth();renderAll();toast(data.report.created.length+' routing rule(s) created from hand-offs, switched off. Review and turn on the ones you want.');}
  if(b.dataset.escalationAck){await api('/api/automation/acknowledge',{fleetId:activeId,escalationId:b.dataset.escalationAck});await loadHealth();}
 }catch(error){toast(error.message,true);}
}
$('content').addEventListener('click',automationClick);
$('content').addEventListener('change',async e=>{
 if(view!=='automation'||!['dispatch-enabled','dispatch-deploy'].includes(e.target.id))return;
 const dispatch={...(current().dispatch||{enabled:true,onDeploy:true,states:[]}),enabled:$('dispatch-enabled').checked,onDeploy:$('dispatch-deploy').checked};
 try{await mutate(next=>updateFleet(next,f=>{f.dispatch=dispatch;}),'Dispatch settings saved.');await loadHealth();}catch(error){toast(error.message,true);}
});
$('content').addEventListener('submit',async e=>{
 if(e.target.id!=='throttle-form')return;
 e.preventDefault();
 const value=id=>$(id).value.trim()===''?null:Number($(id).value);
 const roles={};document.querySelectorAll('[data-role-limit]').forEach(n=>{if(n.value.trim())roles[n.dataset.roleLimit]=Number(n.value);});
 const throttles={...(current().throttles||{})};delete throttles.maxWorkingAgents;delete throttles.fleetTokensPerHour;delete throttles.roleTokensPerHour;
 if(value('f-maxWorkingAgents')!==null)throttles.maxWorkingAgents=value('f-maxWorkingAgents');
 if(value('f-fleetTokensPerHour')!==null)throttles.fleetTokensPerHour=value('f-fleetTokensPerHour');
 if(Object.keys(roles).length)throttles.roleTokensPerHour=roles;
 const webhook=$('f-webhookUrl').value.trim();
 try{await mutate(next=>updateFleet(next,f=>{f.throttles=throttles;f.alerts=webhook?{webhookUrl:webhook}:{};}),'Limits saved.');}
 catch(error){toast(error.message,true);}
});
