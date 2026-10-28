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
function decorateBoard(){
 if(view!=='board'||!liveBoard)return;
 const data=flowCache.get(activeId);if(!data)return;
 document.querySelectorAll('[data-role-node]').forEach(node=>{
  const stats=data.roles[node.dataset.roleNode];if(!stats)return;
  let box=node.querySelector('.live-stats');
  if(!box){box=document.createElement('div');box.className='live-stats';node.querySelector('.node-tickets')?.before(box);}
  box.innerHTML=['waiting','claimed','working'].map(k=>'<span class="live-'+k+(stats[k]?' has':'')+'" title="'+{waiting:'Tickets waiting for this role',claimed:'Tickets being worked',working:'Agents working now'}[k]+'">'+stats[k]+' '+k+'</span>').join('');
 });
 const roles=Object.fromEntries((current().roles||[]).map(r=>[r.id,r.name]));
 const edges=Object.entries(data.edges).filter(([,e])=>e.pending);
 let strip=$('live-edges');
 const viewport=$('board-viewport');
 if(!viewport)return;
 if(!strip){strip=document.createElement('div');strip.id='live-edges';strip.className='live-edges';viewport.insertAdjacentElement('afterend',strip);}
 strip.innerHTML='<strong>Hand-offs in flight</strong>'+(edges.length?edges.map(([key,e])=>{const [s,t]=key.split('>');return '<span>'+esc(roles[s]||'?')+' &rarr; '+esc(roles[t]||'?')+': '+e.pending+' waiting · oldest '+elapsed(e.oldestAt)+'</span>';}).join(''):'<span class="muted">None waiting</span>');
}
function boardLiveToggle(){
 const bar=$('board-toolbar');if(!bar||$('board-live'))return;
 const label=document.createElement('label');label.className='checkbox-field';
 label.innerHTML='<input type="checkbox" id="board-live">Live';
 bar.prepend(label);
 $('board-live').checked=liveBoard;
 $('board-live').onchange=()=>{liveBoard=$('board-live').checked;localStorage.setItem('fleet-live-board',String(liveBoard));if(liveBoard)loadFlow();else renderContent();};
}

const flowOriginalRenderAll=renderAll;
renderAll=function(){flowOriginalRenderAll();if(!state)return;boardLiveToggle();updateDeployBanner();if(view==='board'){decorateBoard();loadFlow();}};
const flowOriginalRenderBoard=renderBoard;
renderBoard=function(){flowOriginalRenderBoard();decorateBoard();};
const flowOriginalLoadHealth=loadHealth;
loadHealth=async function(){await flowOriginalLoadHealth();updateDeployBanner();};
setInterval(()=>{if(state&&!stopped)loadFlow();},5000);
