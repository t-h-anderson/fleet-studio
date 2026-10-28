'use strict';
// One Workflow screen: Design (board and inspector), Roles and Process & deploy are tabs of a single view.
const DESIGN_VIEWS={board:'Design',roles:'Roles',workflow:'Process & deploy'};
let designSelection=null;

// One strip above the board toolbar, so the tabs sit in the same place on every tab.
function updateDesignTabs(){
 let tabs=$('design-tabs');
 if(!tabs){
  tabs=document.createElement('div');tabs.id='design-tabs';tabs.className='design-tabs';tabs.setAttribute('role','tablist');
  $('board-toolbar').insertAdjacentElement('beforebegin',tabs);
  tabs.addEventListener('click',e=>{const tab=e.target.closest('[data-design-tab]');if(tab)setView(tab.dataset.designTab);});
 }
 tabs.classList.toggle('hidden',!DESIGN_VIEWS[view]);
 tabs.innerHTML=Object.entries(DESIGN_VIEWS).map(([id,label])=>
  '<button class="button small '+(view===id?'primary':'')+'" role="tab" aria-selected="'+(view===id)+'" data-design-tab="'+id+'">'+label+'</button>').join('');
}

// The inspector: what is selected on the board, edited in place.
function roleInspector(role){
 const f=current(),names=Object.fromEntries(f.roles.map(r=>[r.id,r.name]));
 const links=f.roleLinks.filter(l=>l.source===role.id||l.target===role.id);
 const agents=assignedAgents(role);
 return '<h3>'+esc(role.name)+'</h3>'+
  '<p class="field-hint">'+(agents.length?'Worked by '+agents.map(a=>esc(a.name)).join(', '):'No agent holds this role yet; assign one under More settings.')+'</p>'+
  textarea('Goal: the outcome this role exists for','i-goal',role.goal,4000)+
  textarea('Success criteria (one per line)','i-successCriteria',(role.successCriteria||[]).join('\n'),4000)+
  field('Done when','i-doneWhen',role.doneWhen||'','maxlength="2000"')+
  textarea('Shared responsibilities','i-responsibilities',role.responsibilities)+
  textarea('Authority and handoff','i-authority',role.authority)+
  '<div class="row-actions inspector-actions"><button class="button primary small" id="inspector-save">Save</button>'+
  '<button class="button small" data-inspector-more="role">More settings…</button>'+
  (agents.some(a=>a.sessionId)?'<button class="button small" data-apply-role="'+role.id+'">Send assignments</button>':'')+'</div>'+
  (links.length?'<h4>Hand-offs</h4><ul class="inspector-links">'+links.map(l=>'<li><button class="text-button" data-inspector-link="'+l.id+'">'+
   esc(names[l.source]||'?')+' &rarr; '+esc(names[l.target]||'?')+': '+esc(l.label)+'</button> <span class="stage-tag">'+esc((l.delivery||'off')==='review'?'needs review':(l.delivery||'off'))+'</span></li>').join('')+'</ul>':'')+
  '<p class="field-hint">Saved to the draft; agents get the change after you deploy.</p>';
}
function linkInspector(link){
 const f=current(),name=id=>f.roles.find(r=>r.id===id)?.name||'Role';
 const trigger=(f.triggers||[]).find(t=>t.workflowLinkId===link.id);
 return '<h3>'+esc(name(link.source))+' &rarr; '+esc(name(link.target))+'</h3>'+
  field('Action on connection','i-label',link.label,'required maxlength="100"')+
  textarea('When / trigger description','i-trigger',link.trigger,2000)+
  textarea('Handoff artifact','i-artifact',link.artifact,2000)+
  textarea('Required condition / acceptance','i-condition',link.condition,2000)+
  deliveryField('i-delivery',link.delivery)+
  (trigger?'<p class="field-hint">Agents report it as <code>'+esc(trigger.eventName)+'</code>.</p>':'')+
  '<div class="row-actions inspector-actions"><button class="button primary small" id="inspector-save">Save</button>'+
  '<button class="button small" data-inspector-more="rolelink">More settings…</button>'+
  '<button class="button small danger" id="inspector-remove">Remove</button></div>'+
  '<p class="field-hint">Saved to the draft; delivery changes take effect after you deploy.</p>';
}
function inspectorMarkup(){
 const f=current();
 const item=designSelection?.type==='role'?f.roles.find(r=>r.id===designSelection.id):designSelection?.type==='rolelink'?f.roleLinks.find(l=>l.id===designSelection.id):null;
 if(!item){designSelection=null;return '<p class="field-hint">Select a role or a hand-off on the board to edit it here. Use Add handoff to connect two roles.</p>';}
 return designSelection.type==='role'?roleInspector(item):linkInspector(item);
}
function addInspector(){
 if(view!=='board'||boardMode!=='roles'||$('design-inspector'))return;
 const content=$('content'),canvas=document.createElement('div');canvas.className='design-canvas';
 [...content.children].forEach(n=>canvas.append(n));
 const split=document.createElement('div');split.className='design-split';
 const panel=document.createElement('aside');panel.id='design-inspector';panel.className='design-inspector';panel.setAttribute('aria-label','Selected role or hand-off');
 panel.innerHTML=inspectorMarkup();
 split.append(canvas,panel);content.append(split);
}
function selectDesign(type,id){designSelection={type,id};const panel=$('design-inspector');if(panel){panel.innerHTML=inspectorMarkup();panel.querySelector('textarea,input')?.focus({preventScroll:true});}}

async function saveInspector(){
 const selection=designSelection;if(!selection)return;
 const value=id=>$(id).value.trim();
 try{
  if(selection.type==='role'){
   const change={goal:value('i-goal'),successCriteria:$('i-successCriteria').value.split('\n').map(v=>v.trim()).filter(Boolean),
    doneWhen:value('f-i-doneWhen'),responsibilities:value('i-responsibilities'),authority:value('i-authority')};
   await mutate(next=>updateFleet(next,f=>Object.assign(f.roles.find(r=>r.id===selection.id),change)),'Role saved to the draft. Deploy to update agents\' briefs.');
  }else{
   const change={label:value('f-i-label'),trigger:value('i-trigger'),artifact:value('i-artifact'),condition:value('i-condition'),delivery:$('i-delivery').value};
   await mutate(next=>updateFleet(next,f=>Object.assign(f.roleLinks.find(l=>l.id===selection.id),change)),'Hand-off saved to the draft. Deploy to change delivery.');
  }
 }catch(e){toast(e.message,true);}
}
async function removeInspectorLink(){
 const selection=designSelection;if(!selection||selection.type!=='rolelink'||!window.confirm('Remove this hand-off from the workflow?'))return;
 try{
  designSelection=null;
  await mutate(next=>updateFleet(next,f=>{f.roleLinks=f.roleLinks.filter(l=>l.id!==selection.id);}),'Hand-off removed from the draft.');
 }catch(e){toast(e.message,true);}
}

// On the Design tab, clicking a role or a hand-off selects it rather than opening a dialog.
$('content').addEventListener('click',e=>{
 if(view!=='board'||boardMode!=='roles')return;
 const role=e.target.closest('[data-edit-role]'),link=e.target.closest('[data-edit-role-link]');
 if((role||link)&&!e.target.closest('#design-inspector')){
  e.preventDefault();e.stopImmediatePropagation();
  selectDesign(role?'role':'rolelink',role?role.dataset.editRole:link.dataset.editRoleLink);
 }
},true);
$('content').addEventListener('click',e=>{
 if(!e.target.closest('#design-inspector'))return;
 const b=e.target.closest('button');if(!b)return;
 if(b.id==='inspector-save')saveInspector();
 if(b.id==='inspector-remove')removeInspectorLink();
 if(b.dataset.inspectorLink)selectDesign('rolelink',b.dataset.inspectorLink);
 if(b.dataset.inspectorMore)openEditor(b.dataset.inspectorMore,designSelection.id);
});

const designOriginalRenderBoard=renderBoard;
renderBoard=function(){designOriginalRenderBoard();addInspector();};
const designOriginalRenderAll=renderAll;
renderAll=function(){
 designOriginalRenderAll();
 if(!state)return;
 updateDesignTabs();
 if(!DESIGN_VIEWS[view])return;
 // The single Workflow item stays highlighted on every tab.
 document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.id==='nav-workflow'));
 $('fleet-title').textContent='Workflow';
 $('fleet-description').textContent='Draw roles and hand-offs, say how work moves between them, then deploy it to your agents.';
 $('view-eyebrow').textContent='WORKFLOW · '+DESIGN_VIEWS[view].toUpperCase();
};
