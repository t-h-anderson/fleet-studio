'use strict';
const workflowCache=new Map(),workflowLoading=new Set();
let entrypointPreview=null;
let workflowSection='overview',workflowPreview=null,workflowEditing=null;
let workflowRendered=null;
const workflowSections={overview:'Release & adoption',policies:'Agent policies',states:'Ticket states',transitions:'Transitions',sources:'Sources',gaps:'Mapping gaps'};
const workflowData=()=>workflowCache.get(activeId);
async function loadWorkflow(force=false){
 if(!state||workflowLoading.has(activeId))return;
 const fid=activeId,revision=state.revision;
 if(!force&&workflowCache.get(fid)?.revision===revision)return;
 workflowLoading.add(fid);
 try{const data=await api('/api/workflow/inspect',{fleetId:fid});workflowCache.set(fid,{...data,revision});if(activeId===fid&&view==='workflow')renderContent();}
 catch(e){toast(e.message,true);}finally{workflowLoading.delete(fid);}
}
function workflowCard(row,section){
 const spec=workflowData().spec;
 const names=ids=>(ids||[]).map(id=>spec.roles.find(r=>r.id===id)?.name||id).join(', ');
 let detail=section==='policies'?row.text:section==='states'?(row.fallback?'Fallback':row.issueState+' / '+[...row.labelsAny,...row.labelPrefixes.map(p=>p+'*')].join(', '))+' | Queues: '+names(row.roles):section==='transitions'?row.from+' \u2192 '+row.to+' \u00b7 '+row.guard:section==='sources'?row.kind+' \u00b7 '+row.url+' \u00b7 '+row.note:row.summary;
 return '<article class="workflow-item"><div><h3>'+esc(row.name||row.action||row.id)+'</h3><code>'+esc(row.id)+'</code></div>'+(section==='policies'?'<details data-workflow-policy="'+esc(row.id)+'"><summary>Read policy</summary><pre class="policy-text">'+esc(detail)+'</pre></details>':'<p>'+esc(detail)+'</p>')+(row.roles?.length?'<p class="field-hint">Applies to '+esc(names(row.roles))+'</p>':'')+(section==='gaps'?'<span class="role-state draft">'+esc(row.resolution)+(row.blocking?' \u00b7 blocks deployment':'')+'</span>':'')+'<button class="button small" data-workflow-edit="'+esc(row.id)+'">Edit</button>'+(section==='states'?'<button class="text-button" data-workflow-up="'+row.id+'">Move up</button>':'')+'</article>';
}
function renderWorkflowContent(html,scope){
 const content=$('content');
 const sameView=workflowRendered?.root===content.firstElementChild&&workflowRendered.scope===scope;
 // Keep the actual nodes while the displayed data is unchanged. This also
 // preserves selection and focus through both background polling loops.
 if(sameView&&workflowRendered.html===html)return;
 const policies=new Map(sameView?[...content.querySelectorAll('[data-workflow-policy]')].map(details=>{
  const text=details.querySelector('.policy-text');
  return [details.dataset.workflowPolicy,{open:details.open,top:text.scrollTop,left:text.scrollLeft,focused:document.activeElement===details.querySelector('summary')}];
 }):[]);
 const pagePosition={x:window.scrollX,y:window.scrollY};
 content.innerHTML=html;
 workflowRendered={root:content.firstElementChild,html,scope};
 for(const details of content.querySelectorAll('[data-workflow-policy]')){
  const saved=policies.get(details.dataset.workflowPolicy);if(!saved)continue;
  details.open=saved.open;
  const text=details.querySelector('.policy-text');text.scrollTop=saved.top;text.scrollLeft=saved.left;
  if(saved.focused)details.querySelector('summary').focus({preventScroll:true});
 }
 if(policies.size)window.scrollTo(pagePosition.x,pagePosition.y);
}
function workflowAdoptionMarkup(data){
 const rows=data.adoption||[],required=rows.filter(a=>a.required),ready=required.filter(a=>a.bindingMatches&&a.entryPoint==='installed'&&a.acknowledged).length;
 if(!rows.length)return '';
 const label=value=>({'not-installed':'Not installed',installed:'Installed',changed:'Changed on disk',missing:'Missing',waiting:'Waiting',queued:'Queued',unknown:'Unconfirmed',failed:'Failed','not-notified':'Not notified'}[value]||value);
 return '<div class="workflow-adoption"><h3>Fleet adoption</h3><p>'+ready+' of '+required.length+' participating agents have the entry point and have acknowledged this release. '+(data.migrationComplete?'Fleet migration complete.':'Migration is pending; queued instructions are not adoption.')+'</p><div class="table-wrap"><table><thead><tr><th>AGENT</th><th>ENTRY POINT</th><th>ASSIGNMENT</th><th>NOTIFICATION</th><th>ADOPTION</th></tr></thead><tbody>'+rows.map(a=>'<tr data-adoption-agent="'+esc(a.agentId)+'"><td>'+esc(a.name)+(a.required?'':' <span class="muted">(excluded or unlinked)</span>')+'</td><td>'+esc(label(a.entryPoint))+'</td><td>'+(a.bindingMatches?'Matches release':'Needs deployment')+'</td><td>'+esc(label(a.notification))+'</td><td>'+(a.acknowledged?'Acknowledged':'Pending')+'</td></tr>').join('')+'</tbody></table></div></div>';
}
function renderWorkflow(){
 loadWorkflow();const data=workflowData(),f=current();$('visible-count').textContent=data?.spec.policies.length||0;
 if(!data){$('content').innerHTML='<div class="activity-empty">Loading workflow...</div>';return;}
 const s=data.spec;
 const html='<div class="definition-toolbar"><div><strong>'+esc(s.name)+' \u00b7 '+esc(s.version)+'</strong><p>Draft &rarr; review &rarr; deploy &rarr; notify &rarr; acknowledge</p></div><div><button class="button small" id="workflow-import">Import JSON</button><button class="button small" id="workflow-export">Export JSON</button><button class="button small" id="workflow-json">Edit specification</button><button class="button primary small" id="workflow-deploy">Review deployment</button></div></div>'+
 '<div class="workflow-status">'+(data.active?'Active release '+esc(data.active.version)+' / '+esc(data.active.id.slice(0,10))+' \u00b7 '+(data.draftChanged?'Draft has undeployed changes':'Draft matches release'):'No deployed Studio workflow. The current definition is a draft.')+' \u00b7 '+data.enabledTriggers+' active trigger(s)'+(data.paused?' \u00b7 Automation paused':'')+'</div>'+
 '<div class="workflow-tabs">'+Object.entries(workflowSections).map(([id,label])=>'<button class="button small '+(id===workflowSection?'primary':'')+'" data-workflow-section="'+id+'">'+label+(id==='overview'?'':' ('+s[id].length+')')+'</button>').join('')+'</div>'+
 (workflowSection==='overview'?'<div class="workflow-overview"><div class="info-block"><h3>One editable workflow</h3><p>Define roles and actions on the <button class="text-button" data-workflow-view="board">design board</button>, distribute <button class="text-button" data-workflow-view="roles">roles</button>, and configure <button class="text-button" data-workflow-view="triggers">triggers</button>. Policies, ticket states, transitions and source references are editable here.</p><p>Deploy freezes definitions, assignments and enabled triggers. Draft edits take effect on the next deployment. Excluding an agent from the fleet stops automatic delivery immediately.</p><button class="button small" id="workflow-template">Review sample workflow</button><button class="button small" id="workflow-document">Download agent reference</button></div><div class="info-block"><h3>Deployment and adoption</h3><p>Deployment writes a versioned local release with generated workflow and agent instructions. Sending instructions queues adoption at a safe boundary; acknowledgements are reported by agents.</p>'+(data.active?'<button class="button small" id="workflow-entrypoints">Review agent entry points</button><button class="button small" id="workflow-notify">Review agent notifications</button><button class="button small" id="workflow-pause">'+(data.paused?'Resume automation':'Pause automation')+'</button>':'<p>Deploy a reviewed version before notifying agents.</p>')+'<p>'+data.entryPoints.filter(e=>!e.status||e.status==='installed').length+' folder entry point(s) installed. Installed entry points follow the active release on future safe boundaries.</p><p class="field-hint">Ticket transitions and acceptance guards remain agent responsibilities. Studio runs only configured event, session-condition and timer notifications.</p></div><div class="info-block"><h3>Release history</h3>'+(data.history.map(r=>'<div class="workflow-release"><span>'+esc(r.version)+' / '+esc(r.id.slice(0,10))+' \u00b7 '+esc(new Date(r.createdAt*1000).toLocaleString())+'</span><button class="text-button" data-workflow-restore="'+r.id+'">Review as draft</button></div>').join('')||'<p>No releases yet.</p>')+'</div>'+workflowAdoptionMarkup(data)+'</div>':'<div class="definition-toolbar"><p>'+esc({policies:'Agent instructions. Empty role selection applies to every role.',states:'Projection uses the first matching rule; keep the fallback last. These labels do not establish ownership.',transitions:'Allowed work transitions and their guards. Agents record them in the current ticket.',sources:'Evidence for the mapping. Importing a source reference does not verify or publish policy.',gaps:'Explicit migration limits. Open blocking gaps prevent deployment.'}[workflowSection])+'</p><button class="button small" id="workflow-new">New '+esc(workflowSection==='policies'?'policy':workflowSection.slice(0,-1))+'</button></div><div class="workflow-items">'+s[workflowSection].filter(r=>JSON.stringify(r).toLowerCase().includes(query.toLowerCase())).map(r=>workflowCard(r,workflowSection)).join('')+'</div>');
 renderWorkflowContent(html,JSON.stringify([activeId,s.id,workflowSection]));
}
const workflowOriginalRenderAll=renderAll;
renderAll=function(){workflowOriginalRenderAll();if(!state)return;if(view==='workflow'){$('fleet-title').textContent='Workflow';$('fleet-description').textContent='Design, deploy and track adoption of your fleet process.';$('view-eyebrow').textContent='MANAGED WORKFLOW';$('panel-title').textContent='Workflow specification';$('workspace-layout').classList.add('board-layout');$('status-filter').classList.add('hidden');$('search').placeholder='Find a policy or rule...';$('panel-footer-text').textContent='Drafts are saved automatically. Review a deployment to activate changes.';}};
function workflowChangeMarkup(p){
 let html='';
 for(const section of ['roles','handoffs','policies','states','transitions','triggers','sources','gaps']){
  const c=p.changes[section];if(!c.added.length&&!c.removed.length&&!c.changed.length&&!c.orderChanged)continue;
  html+='<details><summary>'+esc(section)+': '+c.added.length+' added, '+c.changed.length+' changed, '+c.removed.length+' removed'+(c.orderChanged?' \u00b7 order changed':'')+'</summary>';
  for(const id of [...c.added,...c.changed,...c.removed])html+='<h4>'+esc(id)+'</h4><div class="workflow-diff"><pre>'+esc(JSON.stringify(p.previousSpec?.[section]?.find(r=>r.id===id)||null,null,2))+'</pre><pre>'+esc(JSON.stringify(p.spec[section].find(r=>r.id===id)||null,null,2))+'</pre></div>';
  html+='</details>';
 }
 return html;
}
async function previewWorkflow(options){
 await saveQueue;workflowPreview=await api('/api/workflow/preview',{fleetId:activeId,revision:state.revision,...options});const p=workflowPreview;
 $('workflow-review-title').textContent=p.kind==='deploy'?'Review workflow deployment':'Review workflow import';
 $('workflow-review-body').innerHTML='<p>'+(p.kind==='deploy'?'This creates an immutable local release. The enabled triggers below become active. Notify agents separately after reviewing their instructions.':'This replaces the workflow draft and disables imported triggers. Existing matched role assignments and board positions are preserved.')+'</p><p><strong>'+esc(p.spec.name)+' \u00b7 '+esc(p.spec.version)+'</strong></p>'+workflowChangeMarkup(p)+(p.kind==='deploy'?'<div class="info-block"><strong>Pending handoffs: '+(p.pendingHandoffs||[]).length+'</strong><p>Agents must acknowledge this release before automatic delivery. Unsent handoffs from the previous release require review; queued messages are not resent.</p>'+(p.pendingHandoffs||[]).map(h=>'<p>'+esc(h.name)+' ? '+esc(h.targetName)+' ? '+esc(h.status)+'</p>').join('')+'</div>':'')+'<details><summary>Agent assignments and automation</summary><pre>'+esc(JSON.stringify(p.bindingChanges||p.agents,null,2))+'</pre><p>Enabled triggers: '+esc(p.enabledTriggers.join(', ')||'None')+'</p></details>'+p.gaps.map(g=>'<p class="field-hint">'+esc(g.resolution+' \u00b7 '+g.summary)+'</p>').join('')+'<details><summary>Generated agent workflow</summary><pre class="policy-text">'+esc(p.document)+'</pre></details>';
 $('workflow-apply').textContent=p.kind==='deploy'?'Deploy this version':'Apply to draft';$('workflow-review').showModal();
}
const workflowDefaults={policies:{id:'',name:'',roles:[],text:'',sources:[]},states:{id:'',name:'',roles:[],issueState:'any',labelsAny:[],labelPrefixes:[],fallback:false},transitions:{id:'',from:'',to:'',role:'',action:'',guard:'',sources:[]},sources:{id:'',kind:'proposal',url:'https://',revision:'',note:''},gaps:{id:'',summary:'',roles:[],blocking:false,resolution:'open',sources:[]}};
function editWorkflowItem(id=null){
 const section=workflowSection,item=id?clone(workflowData().spec[section].find(r=>r.id===id)):clone(workflowDefaults[section]);workflowEditing={section,id,item,fleetId:activeId};
 $('workflow-editor-title').textContent=id?'Edit '+id:'New '+section;
 const options={kind:['committed-policy','owner-direction','observed-assignment','proposal'],issueState:['any','opened','closed'],resolution:['open','accepted']};
 const s=workflowData().spec;
 $('workflow-editor-fields').innerHTML=Object.entries(item).map(([key,value])=>{
  const label=key.replace(/([A-Z])/g,' $1'),fieldId='wf-'+key;
  if(['roles','sources'].includes(key))return '<fieldset><legend>'+esc(label)+'</legend>'+s[key].map(r=>'<label class="checkbox-field"><input type="checkbox" data-wf-list="'+key+'" value="'+esc(r.id)+'" '+(value.includes(r.id)?'checked':'')+'>'+esc(r.name||r.id)+'</label>').join('')+'</fieldset>';
  if(typeof value==='boolean')return '<label class="checkbox-field"><input type="checkbox" id="'+fieldId+'" '+(value?'checked':'')+'>'+esc(label)+'</label>';
  const values=options[key]||(['from','to'].includes(key)?s.states.map(r=>r.id):key==='role'?s.roles.map(r=>r.id):null);
  if(values)return '<div class="field"><label for="'+fieldId+'">'+esc(label)+'</label><select id="'+fieldId+'">'+values.map(v=>'<option '+(value===v?'selected':'')+'>'+esc(v)+'</option>').join('')+'</select></div>';
  if(['text','guard','summary','note'].includes(key))return textarea(label,fieldId,value,key==='text'?60000:12000);
  return '<div class="field"><label for="'+fieldId+'">'+esc(label)+(Array.isArray(value)?' (comma-separated)':'')+'</label><input id="'+fieldId+'" value="'+esc(Array.isArray(value)?value.join(', '):value)+'" '+(key==='id'&&id?'readonly':'')+'></div>';
 }).join('');$('workflow-item-delete').hidden=!id;$('workflow-item-editor').showModal();
}
$('workflow-item-form').onsubmit=async event=>{event.preventDefault();const edit=workflowEditing,item=clone(edit.item);try{
 for(const [key,value] of Object.entries(item)){if(['roles','sources'].includes(key))item[key]=[...document.querySelectorAll('[data-wf-list="'+key+'"]:checked')].map(n=>n.value);else if(typeof value==='boolean')item[key]=$('wf-'+key).checked;else if(Array.isArray(value))item[key]=$('wf-'+key).value.split(',').map(s=>s.trim()).filter(Boolean);else item[key]=$('wf-'+key).value.trim();}
 await mutate(next=>{const f=next.fleets.find(f=>f.id===edit.fleetId);f.workflow??=Object.fromEntries(Object.entries(workflowData().spec).filter(([k])=>!['roles','handoffs','triggers'].includes(k)));const list=f.workflow[edit.section];if(edit.id)list[list.findIndex(r=>r.id===edit.id)]=item;else if(edit.section==='states'&&list.some(r=>r.fallback))list.splice(list.findIndex(r=>r.fallback),0,item);else list.push(item);},'Workflow draft saved.');$('workflow-item-editor').close();await loadWorkflow(true);
 }catch(e){toast(e.message,true);}};
$('workflow-item-delete').onclick=async()=>{const edit=workflowEditing;try{await mutate(next=>{const f=next.fleets.find(f=>f.id===edit.fleetId);f.workflow[edit.section]=f.workflow[edit.section].filter(r=>r.id!==edit.id);},'Removed from draft.');$('workflow-item-editor').close();await loadWorkflow(true);}catch(e){toast(e.message,true);}};
$('workflow-apply').onclick=async()=>{const button=$('workflow-apply');button.disabled=true;try{const result=await api('/api/workflow/apply',{previewId:workflowPreview.id});state=result.state;workflowCache.clear();$('workflow-review').close();renderAll();toast(result.release?'Workflow deployed. Review notifications to have agents adopt it.':'Workflow imported as a draft; triggers are disabled.');}catch(e){toast(e.message,true);}finally{button.disabled=false;}};
$('workflow-file').onchange=async event=>{const file=event.target.files[0];event.target.value='';if(!file)return;try{if(file.size>1800000)throw Error('Workflow file is too large.');await previewWorkflow({kind:'import',spec:JSON.parse(await file.text())});}catch(e){toast(e.message,true);}};
$('workflow-json-form').onsubmit=async e=>{e.preventDefault();try{const spec=JSON.parse($('workflow-json-text').value);await previewWorkflow({kind:'import',spec});$('workflow-json-dialog').close();}catch(error){toast(error.message,true);}};
for(const [id,dialog] of [['close-workflow-review','workflow-review'],['close-workflow-editor','workflow-item-editor'],['close-workflow-json','workflow-json-dialog']])$(id).onclick=()=>$(dialog).close();
document.addEventListener('click',async event=>{const b=event.target.closest('button');if(!b)return;try{
 if(b.dataset.workflowSection){workflowSection=b.dataset.workflowSection;renderWorkflow();}
 if(b.dataset.workflowView)setView(b.dataset.workflowView);
 if(b.dataset.workflowEdit)editWorkflowItem(b.dataset.workflowEdit);
 if(b.dataset.workflowRestore)await previewWorkflow({kind:'import',releaseId:b.dataset.workflowRestore});
 if(b.dataset.workflowUp){const id=b.dataset.workflowUp;await mutate(next=>updateFleet(next,f=>{const list=f.workflow.states,i=list.findIndex(r=>r.id===id);if(i>0)[list[i-1],list[i]]=[list[i],list[i-1]];}),'State order updated.');}
 if(b.id==='workflow-new')editWorkflowItem();
 if(b.id==='workflow-import')$('workflow-file').click();
 if(b.id==='workflow-export'){await loadWorkflow(true);downloadText(workflowData().spec.id+'.workflow.json',JSON.stringify(workflowData().spec,null,2)+'\n');}
 if(b.id==='workflow-json'){await loadWorkflow(true);$('workflow-json-text').value=JSON.stringify(workflowData().spec,null,2);$('workflow-json-dialog').showModal();}
 if(b.id==='workflow-template')await previewWorkflow({kind:'import',template:'sample'});
 if(b.id==='workflow-deploy')await previewWorkflow({kind:'deploy'});
 if(b.id==='workflow-document'){const p=await api('/api/workflow/preview',{fleetId:activeId,revision:state.revision,kind:'import',spec:workflowData().spec});downloadText(workflowData().spec.id+'.md',p.document);}
 if(b.id==='workflow-entrypoints'){entrypointPreview=await api('/api/workflow/entrypoints/preview',{fleetId:activeId,revision:state.revision});$('entrypoint-body').innerHTML='<p>Replace the selected folders workflow entry points with a reference to the active Studio release. Original files are retained for recovery. Review any additional local instructions before replacing them.</p>'+entrypointPreview.changes.map(c=>'<h3>'+esc(c.name)+'</h3><p>'+esc(c.path)+'</p><div class="workflow-diff"><pre>'+esc(c.before||'(No existing file)')+'</pre><pre>'+esc(c.after)+'</pre></div>').join('');$('entrypoint-dialog').showModal();}
 if(b.id==='workflow-notify'){const preview=await api('/api/workflow/notify',{fleetId:activeId});await showControl('message',[],'',preview);}
 if(b.id==='workflow-pause'){await api('/api/workflow/pause',{fleetId:activeId,paused:!workflowData().paused});await loadWorkflow(true);}
 }catch(e){toast(e.message,true);}});

$('close-entrypoint').onclick=()=>$('entrypoint-dialog').close();
$('apply-entrypoint').onclick=async()=>{const b=$('apply-entrypoint');b.disabled=true;try{const r=await api('/api/workflow/entrypoints/apply',{previewId:entrypointPreview.id});state=r.state;$('entrypoint-dialog').close();await loadWorkflow(true);renderAll();toast(r.installed.length+' agent folder entry points installed.');}catch(e){toast(e.message,true);}finally{b.disabled=false;}};

setInterval(()=>{if(state&&view==='workflow'&&!stopped)loadWorkflow(true);},5000);
