'use strict';
// Cross-view awareness: undeployed changes, and a live overlay of the flow on the design board.
const flowCache=new Map();
let liveBoard=localStorage.getItem('fleet-live-board')!=='false';

function deploymentStatus(){return healthCache.get(activeId)?.deployment||null;}
function openDeployReview(){
 setView('workflow');
 if(typeof previewWorkflow==='function')previewWorkflow({kind:'deploy'}).catch(e=>toast(e.message,true));
}
function updateDeployBanner(){
 let banner=$('deploy-banner');
 if(!banner){
  banner=document.createElement('div');banner.id='deploy-banner';banner.className='deploy-banner hidden';banner.setAttribute('role','status');
  document.querySelector('.topbar').insertAdjacentElement('afterend',banner);
  banner.onclick=e=>{if(e.target.closest('#deploy-banner-review'))openDeployReview();};
 }
 const status=deploymentStatus(),changed=!!status?.draftChanged;
 const stale=(status?.staleAgents||[]).length;
 banner.classList.toggle('hidden',!changed);
 // Agents keep working from the deployed brief until the draft is deployed and they are notified.
 banner.innerHTML=changed?'<span><strong>Undeployed changes.</strong> '+(status.active?(stale?stale+' agent(s) are still working from an older brief.':'Agents are still working from the deployed version.'):'Nothing is deployed yet, so agents have no brief.')+'</span><button class="button small primary" id="deploy-banner-review">Review &amp; deploy</button>':'';
 $('nav-workflow')?.classList.toggle('has-badge',changed);
 if(view==='workflow'){const b=$('workflow-deploy');if(b&&status?.active){b.disabled=!changed;b.title=changed?'':'The draft matches the deployed version.';}}
 if(view==='roles')document.querySelectorAll('[data-apply-role]').forEach(b=>{
  // Keep the original label so it comes back once the draft is deployed.
  if(changed){b.dataset.label??=b.textContent;b.textContent='Deploy & notify…';b.disabled=false;}
  else if(b.dataset.label){b.textContent=b.dataset.label;delete b.dataset.label;}
 });
}
// Sending assignments from a stale draft fails; route it to the deployment review instead.
$('content').addEventListener('click',e=>{
 const b=e.target.closest('[data-apply-role]');
 if(!b||!deploymentStatus()?.draftChanged)return;
 e.preventDefault();e.stopImmediatePropagation();openDeployReview();
},true);

async function loadFlow(){
 if(!state||stopped||view!=='board'||!liveBoard)return;
 try{flowCache.set(activeId,await api('/api/flow?fleetId='+encodeURIComponent(activeId)));decorateBoard();}catch(e){/* Shown by the status banner. */}
}
const LIVE_CLASSES=['live-pending','live-slow','live-attention','live-quiet','live-hot'];
// One hand-off's or a group's live numbers, summed over its members.
function liveLinkStats(data,ids){
 const rows=ids.map(id=>data.links?.[id]).filter(Boolean);
 const sum=key=>rows.reduce((total,row)=>total+row[key],0),oldest=rows.map(r=>r.oldestAt).filter(Boolean);
 return {pending:sum('pending'),held:sum('held'),attention:sum('attention'),recent:sum('recent'),oldestAt:oldest.length?Math.min(...oldest):null,
  items:rows.flatMap(r=>r.items).sort((a,b)=>a.at-b.at)};
}
function liveState(stats,data){
 const waiting=stats.pending+stats.held+stats.attention;
 // Held is ordinary back-pressure (recipient busy, throttled); only a delivery needing attention is red.
 if(stats.attention)return 'live-attention';
 if(!waiting)return 'live-quiet';
 return Date.now()/1000-stats.oldestAt>=60*(data.hot?.slowMinutes||15)?'live-slow':'live-pending';
}
function liveTitle(stats){
 const waiting=stats.pending+stats.held+stats.attention;
 return (waiting?waiting+' waiting, oldest '+elapsed(stats.oldestAt)+(stats.held?'; '+stats.held+' held':'')+(stats.attention?'; '+stats.attention+' need attention':''):'Nothing waiting')+
  '; '+stats.recent+' delivered in 24h';
}
function clearLive(){
 document.querySelectorAll('#board-links [data-link-ids]').forEach(n=>n.classList.remove(...LIVE_CLASSES));
 document.querySelectorAll('[data-role-node]').forEach(n=>{n.classList.remove(...LIVE_CLASSES);n.removeAttribute('title');});
 document.querySelectorAll('.live-badge,.live-stats,#live-edges').forEach(n=>n.remove());
 const legend=$('board-live-legend');if(legend)legend.classList.add('hidden');
 const panel=$('inspector-live');if(panel)setMarkup(panel,'');
}
function decorateBoard(){
 if(view!=='board')return;
 const data=flowCache.get(activeId);
 // The overlay reads hand-offs, which only the roles layout draws.
 if(!liveBoard||!data||boardMode!=='roles')return clearLive();
 $('board-live-legend')?.classList.remove('hidden');
 decorateRoles(data);decorateLinks(data);decorateStrip(data);fillInspectorLive();
}
function decorateRoles(data){
 document.querySelectorAll('[data-role-node]').forEach(node=>{
  const stats=data.roles[node.dataset.roleNode];if(!stats)return;
  let box=node.querySelector('.live-stats');
  if(!box){box=document.createElement('div');box.className='live-stats';node.querySelector('.node-tickets')?.before(box);}
  const waste=stats.tokens24h?Math.round(100*stats.wasteTokens24h/stats.tokens24h):0;
  const chips=['waiting','claimed','working'].map(k=>'<span class="live-'+k+(stats[k]?' has':'')+'" title="'+{waiting:'Tickets waiting for this role',claimed:'Tickets being worked',working:'Agents working now'}[k]+'">'+stats[k]+' '+k+'</span>');
  // Problems only appear when they apply, so a healthy card stays quiet.
  if(stats.throttled)chips.push('<span class="live-throttled has" title="Agents held back by a throttle now">'+stats.throttled+' throttled</span>');
  if(waste)chips.push('<span class="live-waste'+(waste>=25?' has':'')+'" title="Estimated share of the last 24h\'s fresh tokens spent on loops, admin, polling and abandoned turns">'+waste+'% waste</span>');
  box.innerHTML=chips.join('');
  const hot=data.hot?.role===node.dataset.roleNode;
  node.classList.toggle('live-hot',hot);
  if(hot)node.title='Largest ticket queue right now';else node.removeAttribute('title');
 });
}
function decorateLinks(data){
 const svg=$('board-links');if(!svg)return;
 svg.querySelectorAll('.live-badge').forEach(n=>n.remove());
 svg.querySelectorAll('.workflow-label[data-link-ids]').forEach(label=>{
  const state=liveState(liveLinkStats(data,label.dataset.linkIds.split(' ')),data);
  label.classList.remove(...LIVE_CLASSES);label.classList.add(state);
 });
 svg.querySelectorAll('.workflow-connection[data-link-ids]').forEach(group=>{
  const ids=group.dataset.linkIds.split(' '),stats=liveLinkStats(data,ids),state=liveState(stats,data);
  group.classList.remove(...LIVE_CLASSES);group.classList.add(state);
  // The slowest hand-off stands out even when the board is busy.
  group.classList.toggle('live-hot',ids.includes(data.hot?.link));
  const waiting=stats.pending+stats.held+stats.attention;if(!waiting)return;
  const text=waiting+' · '+elapsed(stats.oldestAt),x=+group.dataset.badgeX,y=+group.dataset.badgeY,width=12+7*text.length;
  const badge=document.createElementNS('http://www.w3.org/2000/svg','g');
  badge.setAttribute('class','live-badge '+state);
  badge.innerHTML='<title>'+esc(liveTitle(stats))+'</title><rect x="'+(x-width/2)+'" y="'+(y-11)+'" width="'+width+'" height="18" rx="9"></rect><text x="'+x+'" y="'+(y+2)+'" text-anchor="middle">'+esc(text)+'</text>';
  svg.append(badge);
 });
}
function decorateStrip(data){
 const viewport=$('board-viewport');if(!viewport)return;
 let strip=$('live-edges');
 if(!strip){strip=document.createElement('div');strip.id='live-edges';strip.className='live-edges';viewport.insertAdjacentElement('afterend',strip);}
 const f=current(),roles=Object.fromEntries((f.roles||[]).map(r=>[r.id,r.name]));
 const waiting=stats=>stats.pending+stats.held+stats.attention;
 const busy=f.roleLinks.map(l=>({link:l,stats:liveLinkStats(data,[l.id])})).filter(r=>waiting(r.stats)).sort((a,b)=>a.stats.oldestAt-b.stats.oldestAt);
 // Deliveries no hand-off explains still show by role pair, counting only the remainder.
 const explained={};busy.forEach(r=>{const key=r.link.source+'>'+r.link.target;explained[key]=(explained[key]||0)+waiting(r.stats);});
 const other=Object.entries(data.edges).map(([key,e])=>[key,{...e,pending:e.pending-(explained[key]||0)}]).filter(([,e])=>e.pending>0);
 setMarkup(strip,'<strong>Hand-offs in flight</strong>'+(busy.length||other.length?
  busy.map(({link,stats})=>'<button class="text-button live-'+liveState(stats,data).slice(5)+'" data-live-link="'+link.id+'" title="'+esc(liveTitle(stats))+'">'+esc(roles[link.source]||'?')+' &rarr; '+esc(roles[link.target]||'?')+': '+waiting(stats)+' waiting · oldest '+elapsed(stats.oldestAt)+'</button>').join('')+
  other.map(([key,e])=>{const [s,t]=key.split('>');return '<span>'+esc(roles[s]||'?')+' &rarr; '+esc(roles[t]||'?')+': '+e.pending+' waiting · oldest '+elapsed(e.oldestAt)+'</span>';}).join(''):
  '<span class="muted">None waiting</span>'));
}
// Polls rewrite only what changed, so a focused button keeps its focus.
function setMarkup(node,markup){if(node.dataset.markup!==markup){node.innerHTML=markup;node.dataset.markup=markup;}}
$('content').addEventListener('click',e=>{
 const b=e.target.closest('[data-live-link]');
 if(b&&typeof selectDesign==='function')selectDesign('rolelink',b.dataset.liveLink);
 const health=e.target.closest('[data-live-health]');
 if(health){setView('health');query=health.dataset.liveHealth;$('search').value=query;renderContent();}
});
// The inspector's live section sits outside its form fields, so refilling it never touches unsaved edits.
function fillInspectorLive(){
 const panel=$('inspector-live'),data=flowCache.get(activeId);if(!panel)return;
 if(!liveBoard||!data||typeof designSelection==='undefined'||!designSelection){setMarkup(panel,'');return;}
 const note=deploymentStatus()?.draftChanged?'<p class="field-hint">Live numbers follow the deployed workflow; undeployed changes show after you deploy.</p>':'';
 if(designSelection.type==='role'){
  const stats=data.roles[designSelection.id];if(!stats){setMarkup(panel,'');return;}
  setMarkup(panel,'<h4>Live</h4><p class="live-line">'+stats.waiting+' tickets waiting · '+stats.claimed+' claimed · '+stats.working+' agents working'+(stats.throttled?' · '+stats.throttled+' throttled':'')+'</p>'+
   '<p class="live-line">24h: '+tokenCount(stats.tokens24h)+' fresh tokens'+(stats.tokens24h?', ≈'+Math.round(100*stats.wasteTokens24h/stats.tokens24h)+'% waste':'')+'</p>'+
   (stats.agents.length?'<ul class="inspector-links">'+stats.agents.map(a=>'<li><span class="'+healthChipClass(a.id)+'">'+esc(a.name)+'</span> <button class="text-button" data-live-health="'+esc(a.name)+'">Agent health</button></li>').join('')+'</ul>':'')+note);
  return;
 }
 const stats=liveLinkStats(data,[designSelection.id]);
 setMarkup(panel,'<h4>Live</h4><p class="live-line">'+esc(liveTitle(stats))+'.</p>'+
  (stats.items.length?'<ul class="inspector-links live-items">'+stats.items.map(item=>'<li><span>'+esc(item.from||'?')+' &rarr; '+esc(item.to||'?')+' · '+esc(item.status)+' · '+elapsed(item.at)+'</span>'+
   (item.actionId?' <button class="button small" data-review-trigger="'+esc(item.actionId)+'">Review</button><button class="button small" data-dismiss-trigger="'+esc(item.actionId)+'">Dismiss</button>':'')+'</li>').join('')+'</ul>':'')+note)
}
function boardLiveToggle(){
 const bar=$('board-toolbar');if(!bar||$('board-live'))return;
 // The board edits the draft; the Progress view shows what is deployed.
 const draft=document.createElement('span');draft.className='stage-tag';draft.id='board-draft-label';draft.textContent='Draft';
 draft.title='The design board edits the draft. Progress shows the deployed workflow agents follow.';bar.prepend(draft);
 const label=document.createElement('label');label.className='checkbox-field';
 label.innerHTML='<input type="checkbox" id="board-live">Live';
 bar.prepend(label);
 const legend=document.createElement('span');legend.id='board-live-legend';legend.className='live-legend hidden';
 legend.innerHTML='<span class="live-pending">waiting</span><span class="live-slow">slow</span><span class="live-attention">needs attention</span><span class="delivery-review">review</span><span class="delivery-off">off</span>';
 label.after(legend);
 $('board-live').checked=liveBoard;
 $('board-live').onchange=()=>{liveBoard=$('board-live').checked;localStorage.setItem('fleet-live-board',String(liveBoard));if(liveBoard)loadFlow();else clearLive();};
}

const flowOriginalRenderAll=renderAll;
renderAll=function(){flowOriginalRenderAll();if(!state)return;boardLiveToggle();updateDeployBanner();if(view==='board'){decorateBoard();loadFlow();}};
const flowOriginalRenderBoard=renderBoard;
renderBoard=function(){flowOriginalRenderBoard();decorateBoard();};
const flowOriginalLoadHealth=loadHealth;
loadHealth=async function(){await flowOriginalLoadHealth();updateDeployBanner();};
setInterval(()=>{if(state&&!stopped)loadFlow();},5000);
