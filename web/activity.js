'use strict';
const activityCache=new Map(),activityRequests=new Set();
let activityAgent='',activityFilter='all',activityFollow=true;
const activityLabels={input:'Input',progress:'Progress update',thinking:'Thinking summary',summary:'Final summary'};
function selectedActivityAgent(){return current().agents.find(a=>a.id===activityAgent)||current().agents[0];}
async function loadActivity(){
 if(!state||stopped||view!=='activity')return;
 const agent=selectedActivityAgent();if(!agent)return;
 const key=activeId+':'+agent.id;if(activityRequests.has(key))return;
 activityRequests.add(key);
 try{const data=await api('/api/activity?fleetId='+encodeURIComponent(activeId)+'&agentId='+encodeURIComponent(agent.id));
  const previous=activityCache.get(key);activityCache.set(key,data);
  if(view==='activity'&&selectedActivityAgent()?.id===agent.id&&(JSON.stringify(previous?.messages)!==JSON.stringify(data.messages)||previous?.error!==data.error))renderAgentActivity();
 }catch(e){activityCache.set(key,{messages:[],error:e.message});if(view==='activity')renderAgentActivity();}
 finally{activityRequests.delete(key);}
}
function renderAgentActivity(){
 const agent=selectedActivityAgent();if(!agent){$('content').innerHTML=empty('No agents yet','Add an agent and link its saved session.',true);return;}
 activityAgent=agent.id;
 const data=activityCache.get(activeId+':'+agent.id),messages=(data?.messages||[]).filter(m=>(activityFilter==='all'||m.kind===activityFilter)&&m.text.toLowerCase().includes(query.toLowerCase()));
 const old=$('agent-feed'),scroll=old?.scrollTop||0;
 $('visible-count').textContent=messages.length;
 const tickets=backlogItems().filter(t=>agentReferences(t).includes(agent.id));
 $('content').innerHTML='<div class="activity-toolbar"><label>Agent<select id="activity-agent">'+current().agents.map(a=>'<option value="'+a.id+'">'+esc(a.name)+'</option>').join('')+'</select></label><label>Show<select id="activity-filter"><option value="all">All messages</option>'+Object.entries(activityLabels).map(([key,label])=>'<option value="'+key+'">'+label+'</option>').join('')+'</select></label><label class="checkbox-field"><input id="activity-follow" type="checkbox" '+(activityFollow?'checked':'')+'>Follow latest</label><button class="button small" id="refresh-activity">'+icon('refresh')+'Refresh</button></div><div class="activity-agent-header">'+avatar(agent.name)+'<div><strong>'+esc(agent.name)+'</strong><span>'+esc(displayedRole(agent))+'</span></div>'+statusLabel(agent)+'<button class="button small" data-edit="'+agent.id+'">Agent controls</button></div><div class="activity-ticket-links"><span>Linked tickets</span>'+tickets.slice(0,8).map(t=>'<button class="text-button" data-ticket="'+t.iid+'">#'+t.iid+' '+esc(t.title)+'</button>').join('')+(!tickets.length?'<span class="muted">No current ticket references</span>':'')+'</div>'+(data?.error?'<div class="backlog-notice">'+esc(data.error)+'</div>':'')+'<div class="agent-feed" id="agent-feed" role="log" aria-label="Agent conversation"><div class="feed-content">'+messages.map(m=>'<article class="feed-message '+m.kind+'" data-message-kind="'+m.kind+'"><div><strong>'+esc(activityLabels[m.kind])+'</strong><time>'+esc(m.timestamp?new Date(m.timestamp).toLocaleString():'')+'</time></div><p>'+esc(m.text)+'</p>'+(m.truncated?'<span class="field-hint">Long message shortened in this view.</span>':'')+'</article>').join('')+(!messages.length?'<p class="activity-empty">'+(!data?'Reading the saved conversation…':data.error?'Activity will appear when the session is available.':'No matching messages in the recent transcript. Progress and final summaries appear as the agent writes them.')+'</p>':'')+'</div></div><div class="feed-note">Refreshes every 3 seconds. Shows inputs, visible updates, supplied thinking summaries and final answers. '+(data?.limited?'Recent transcript window; older messages remain in Codex.':'')+'</div>';
 $('activity-agent').value=agent.id;$('activity-filter').value=activityFilter;
 const feed=$('agent-feed');feed.scrollTop=activityFollow?feed.scrollHeight:scroll;
 if(!data)loadActivity();
}
const beforeActivityRenderAll=renderAll;
renderAll=function(){beforeActivityRenderAll();if(!state||view!=='activity')return;
 $('fleet-title').textContent='Agent activity';$('fleet-description').textContent='Follow conversations and progress alongside the work.';
 $('workspace-layout').classList.add('board-layout');$('status-filter').classList.add('hidden');$('search').placeholder='Search messages\u2026';
 $('panel-footer-text').textContent='Activity is read from the existing session. Agent controls queue explicit instructions.';
};
$('content').addEventListener('change',e=>{
 if(e.target.id==='activity-agent'){activityAgent=e.target.value;renderAgentActivity();loadActivity();}
 if(e.target.id==='activity-filter'){activityFilter=e.target.value;renderAgentActivity();}
 if(e.target.id==='activity-follow'){activityFollow=e.target.checked;renderAgentActivity();}
});
$('content').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 if(b.dataset.agentActivity){activityAgent=b.dataset.agentActivity;setView('activity');loadActivity();}
 if(b.id==='refresh-activity')loadActivity();
});
setInterval(loadActivity,3000);
