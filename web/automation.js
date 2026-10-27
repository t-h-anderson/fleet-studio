'use strict';
// Health automations, throttles and the throughput view.
const SIGNAL_LABELS={looping:'Looping',stalled:'Stalled','admin-heavy':'Admin-heavy',burning:'Burning tokens',waiting:'Waiting on a blocker','any-problem':'Any problem'};
const ACTION_LABELS={reprompt:'Reprompt with evidence',handoff:'Hand off to a healthy agent','stop-turn':'Stop the Studio-started turn',pause:'Pause automatic deliveries',escalate:'Escalate to me',none:'Do nothing more'};
const backtests=new Map();

function roleLabel(id){return id?(current().roles.find(r=>r.id===id)?.name||'Missing role'):'All roles';}
function ruleSummary(r){
 return 'When '+roleLabel(r.roleId).toLowerCase()+' '+(r.roleId?'agents are ':'are ')+SIGNAL_LABELS[r.when.signal].toLowerCase()+
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
  '<div class="row-actions"><button class="button small" data-automation-toggle="'+r.id+'">'+(r.enabled?'Turn off':'Turn on')+'</button><button class="button small" data-automation-backtest="'+r.id+'">Backtest 24 h</button><button class="button small" data-automation-edit="'+r.id+'">Edit</button></div></article>';
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
  '<div class="form-row">'+current().roles.filter(r=>r.kind!=='subagent').slice(0,6).map(r=>field(esc(r.name)+' tokens per hour','role-'+r.id,roleLimits[r.id]??'','type="number" min="1000" placeholder="No limit" data-role-limit="'+r.id+'"')).join('')+'</div>'+
  field('Escalation webhook (Slack-compatible JSON)','webhookUrl',alerts.webhookUrl||'','type="url" placeholder="https://hooks.example/…"')+
  '<p class="field-hint">Automatic sends that would exceed a limit wait, and the waiting time is shown in Throughput.</p><button class="button primary small" type="submit">Save limits</button></form>';
}
function renderAutomation(){
 const f=current(),rules=(f.automations||[]).filter(r=>(r.name+' '+r.when.signal).toLowerCase().includes(query.toLowerCase())),data=automationData();
 $('visible-count').textContent=rules.length;
 $('content').innerHTML=
  (data.escalations.length?'<section class="escalations"><h3>Needs you</h3>'+data.escalations.map(e=>'<div class="progress-attention"><span class="stage-tag">'+esc(e.ruleName)+'</span><strong>'+esc(e.agentName)+'</strong><span>'+esc(e.message)+'</span><button class="button small" data-escalation-ack="'+e.id+'">Done</button></div>').join('')+'</section>':'')+
  '<div class="definition-toolbar"><div><strong>Automations act on health, not on a schedule.</strong><p>Changes apply on the next check; no deployment or acknowledgement is needed. Each rule limits itself, then falls back once.</p></div><div>'+
  (data.presets?.length?'<button class="button small" id="automation-presets">Add recommended</button>':'')+'<button class="button small" id="automation-new">'+icon('plus')+'New automation</button></div></div>'+
  (rules.length?'<div class="roles-grid">'+rules.map(ruleCard).join('')+'</div>':'<p class="field-hint">No automations yet. Add the recommended set to reprompt loops and stalls, refocus admin-heavy agents and pause token burn.</p>')+
  throttleForm()+
  '<h3>Recent actions</h3><div class="table-wrap"><table><thead><tr><th>WHEN</th><th>AGENT</th><th>RULE</th><th>ACTION</th><th>DELIVERY</th><th>OUTCOME</th></tr></thead><tbody>'+
  (data.fires.length?data.fires.slice(0,40).map(fireRow).join(''):'<tr><td colspan="6" class="muted">No automation has acted yet.</td></tr>')+'</tbody></table></div>';
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
 const fired=auto.fires.filter(f=>f.at>Date.now()/1000-86400),recovered=fired.filter(f=>f.outcome==='recovered').length;
 const tiles=[
  ['Tickets closed · 24h',closed??'—'],
  ['Fresh tokens · 24h',tokenCount(tokens)],
  ['Estimated waste · 24h',tokenCount(wasted)+(tokens?' ('+Math.round(100*wasted/tokens)+'%)':'')],
  ['Tokens per closed ticket',closed?tokenCount(Math.round(tokens/closed)):'—'],
  ['Throttled time · 24h',Math.round((auto.throttledSeconds24h||0)/60)+' min'],
  ['Automations that helped',fired.length?recovered+' of '+fired.length:'—'],
 ];
 $('visible-count').textContent=current().roles.length;
 $('content').innerHTML='<div class="progress-metrics throughput-metrics">'+tiles.map(([l,v])=>'<div><span>'+l+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>'+
  '<h3>Where tokens are wasted</h3>'+wasteBar(waste)+
  '<h3>By role</h3><div class="table-wrap"><table><thead><tr><th>ROLE</th><th>AGENTS</th><th>STUCK NOW</th><th>OPEN QUEUE</th><th>FRESH TOKENS 24H</th><th>WASTE</th><th>CALL MIX</th></tr></thead><tbody>'+
  current().roles.filter(r=>r.kind!=='subagent').map(role=>{
   const agents=current().agents.filter(a=>(a.roleIds||[]).includes(role.id)),hs=agents.map(a=>agentHealth(a.id)).filter(Boolean);
   const t=hs.reduce((s,h)=>s+h.tokens24h,0),w=hs.reduce((s,h)=>s+h.wasteTokens24h,0),mix={};
   hs.forEach(h=>CALL_CLASSES.forEach(k=>mix[k]=(mix[k]||0)+(h.classes24h?.[k]||0)));
   const queue=progress&&!progress.error?progress.roles.find(r=>r.id===role.id)?.queueCount:null;
   return '<tr><td>'+esc(role.name)+'</td><td>'+agents.length+'</td><td>'+(stuckCount(agents)?'<span class="health-badge health-stalled">'+stuckCount(agents)+'</span>':'0')+'</td><td>'+(queue??'—')+'</td><td>'+tokenCount(t)+'</td><td>'+(t?Math.round(100*w/t)+'%':'—')+'</td><td>'+mixBar(mix)+'</td></tr>';
  }).join('')+'</tbody></table></div>'+
  '<p class="field-hint">Closures come from the tracker; tokens and waste from local session telemetry. Tokens per ticket divides all fresh tokens by closures, so it is an indicator, not a cost attribution.</p>';
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
 $('editor-fields').innerHTML=field('Name','name',item.name,'required maxlength="100"')+
  '<div class="form-row"><div class="field"><label for="f-auto-role">Watch agents in</label><select id="f-auto-role">'+roles(item.roleId,'All roles')+'</select></div>'+
  '<div class="field"><label for="f-auto-signal">When an agent is</label><select id="f-auto-signal">'+options(SIGNAL_LABELS,item.when.signal)+'</select></div>'+
  field('For at least (minutes)','auto-for',item.when.forMinutes,'type="number" min="0" max="10080"')+'</div>'+
  '<div class="form-row"><div class="field"><label for="f-auto-action">Then</label><select id="f-auto-action">'+options(Object.fromEntries(Object.entries(ACTION_LABELS).filter(([k])=>k!=='none')),item.action)+'</select></div>'+
  '<div class="field"><label for="f-auto-handoff">Hand off to</label><select id="f-auto-handoff">'+roles(item.handoffRoleId,'Same role')+'</select></div></div>'+
  textarea('Message template (optional)','f-auto-message',item.message,4000)+
  '<p class="field-hint">Leave empty for a message tailored to the signal. Placeholders: {agent}, {goal}, {evidence}, {ticket}, {state}, {minutes}.</p>'+
  '<div class="form-row">'+field('Cooldown (minutes)','auto-cooldown',item.cooldownMinutes,'type="number" min="1" max="10080"')+field('Max times per agent (0 = unlimited)','auto-max',item.maxFires,'type="number" min="0" max="1000"')+field('Within (hours)','auto-window',item.windowHours,'type="number" min="1" max="720"')+'</div>'+
  '<div class="form-row"><div class="field"><label for="f-auto-then">After the limit</label><select id="f-auto-then">'+options({escalate:ACTION_LABELS.escalate,handoff:ACTION_LABELS.handoff,pause:ACTION_LABELS.pause,none:ACTION_LABELS.none},item.thenAction)+'</select></div>'+
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
 const id=editing.id;
 try{await saveAutomations(list=>list.splice(list.findIndex(r=>r.id===id),1),'Automation removed.');$('editor').close();editing=null;}
 catch(e){toast(e.message,true);}
};
function saveAutomations(change,message){
 return mutate(next=>updateFleet(next,f=>{f.automations??=[];change(f.automations);}),message);
}
async function automationClick(e){
 if(view!=='automation')return;
 const b=e.target.closest('button');if(!b)return;
 try{
  if(b.id==='automation-new')openEditor('automation');
  if(b.id==='automation-presets'){
   const presets=automationData().presets.map(p=>({...clone(p),id:uid(),enabled:true,roleId:'',message:'',handoffRoleId:'',parkSource:true,windowHours:24,pauseMinutes:60}));
   const existing=new Set((current().automations||[]).map(r=>r.name));
   await saveAutomations(list=>list.push(...presets.filter(p=>!existing.has(p.name))),'Recommended automations added.');
  }
  if(b.dataset.automationEdit)openEditor('automation',b.dataset.automationEdit);
  if(b.dataset.automationToggle)await saveAutomations(list=>{const r=list.find(r=>r.id===b.dataset.automationToggle);r.enabled=!r.enabled;},'Automation updated.');
  if(b.dataset.automationBacktest){
   const rule=current().automations.find(r=>r.id===b.dataset.automationBacktest);
   backtests.set(rule.id,await api('/api/automation/backtest',{fleetId:activeId,rule,hours:24}));renderAutomation();
  }
  if(b.dataset.escalationAck){await api('/api/automation/acknowledge',{fleetId:activeId,escalationId:b.dataset.escalationAck});await loadHealth();}
 }catch(error){toast(error.message,true);}
}
$('content').addEventListener('click',automationClick);
$('content').addEventListener('submit',async e=>{
 if(e.target.id!=='throttle-form')return;
 e.preventDefault();
 const value=id=>$(id).value.trim()===''?null:Number($(id).value);
 const roles={};document.querySelectorAll('[data-role-limit]').forEach(n=>{if(n.value.trim())roles[n.dataset.roleLimit]=Number(n.value);});
 const throttles={};
 if(value('f-maxWorkingAgents')!==null)throttles.maxWorkingAgents=value('f-maxWorkingAgents');
 if(value('f-fleetTokensPerHour')!==null)throttles.fleetTokensPerHour=value('f-fleetTokensPerHour');
 if(Object.keys(roles).length)throttles.roleTokensPerHour=roles;
 const webhook=$('f-webhookUrl').value.trim();
 try{await mutate(next=>updateFleet(next,f=>{f.throttles=throttles;f.alerts=webhook?{webhookUrl:webhook}:{};}),'Limits saved.');}
 catch(error){toast(error.message,true);}
});
