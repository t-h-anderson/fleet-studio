'use strict';
// Profiles: whole setups saved as files, so a fleet can be retooled from a list in one reviewed deploy.
const profileCache=new Map();

async function loadProfiles(){
 const fid=activeId;
 const data=await api('/api/profiles?fleetId='+encodeURIComponent(fid));
 profileCache.set(fid,data);
 if(fid===activeId){
  if(view==='profiles')renderContent();
  if(typeof updateDesignTabs==='function')updateDesignTabs();
 }
 return data;
}
function profileData(){return profileCache.get(activeId);}
async function switchProfile(slug){
 await previewWorkflow({kind:'profile',profile:slug});
}
// The review says what a switch cannot carry over, so nothing is silently dropped.
function profileReviewMarkup(p){
 if(!p.profile)return '';
 return '<div class="info-block"><strong>Retooling to '+esc(p.profile.name)+'</strong><p>Roles, assignments, hand-off delivery, recovery rules, limits and dispatch are replaced by the profile. The current setup is saved first as a profile, so you can switch back.</p>'+
  (p.profile.remap.length?'<ul class="profile-remap">'+p.profile.remap.map(line=>'<li>'+esc(line)+'</li>').join('')+'</ul>':'<p>Every agent and rule in the profile matched this fleet.</p>')+'</div>';
}

function renderProfiles(){
 const data=profileData();
 if(!data){
  $('content').innerHTML='<div class="empty-state"><span class="loading-ring"></span><h3>Loading profiles</h3></div>';
  loadProfiles().catch(e=>toast(e.message,true));
  return;
 }
 $('visible-count').textContent=data.profiles.length;
 const card=p=>'<article class="role-card profile-card'+(p.active?' is-active':'')+'"><div class="role-card-heading"><h3>'+esc(p.name)+'</h3>'+
  (p.active?'<span class="stage-tag">Active</span>':'')+(p.automatic?'<span class="stage-tag">Saved automatically</span>':'')+'</div>'+
  (p.description?'<p>'+esc(p.description)+'</p>':'')+
  '<p class="field-hint">'+p.roles+' roles · '+p.agents+' agents · saved '+esc(ago(p.savedAt))+(p.source?' from '+esc(p.source):'')+'</p>'+
  '<div class="role-card-actions"><button class="button small primary" data-profile-switch="'+esc(p.slug)+'"'+(p.active?' disabled title="This profile is what agents follow now"':'')+'>Switch to this…</button>'+
  '<button class="button small danger" data-profile-delete="'+esc(p.slug)+'">Delete</button></div></article>';
 $('content').innerHTML='<form class="profile-save" id="profile-save-form"><div class="form-row">'+field('Save current setup as','profile-name','','required maxlength="80" placeholder="e.g. Feature delivery"')+
  field('Description (optional)','profile-description','','maxlength="600"')+'</div>'+
  '<label class="checkbox-field"><input type="checkbox" id="f-profile-overwrite">Replace a profile with the same name</label>'+
  '<button class="button primary small" type="submit">Save profile</button>'+
  '<p class="field-hint">A profile is the whole setup: the workflow, who holds which role, hand-off delivery, recovery rules, limits and dispatch. It never stores sessions or folders, so the files in the profiles folder of your data directory can be copied to another machine.</p></form>'+
  (data.profiles.length?'<div class="roles-grid">'+data.profiles.map(card).join('')+'</div>':'<div class="empty-state"><h3>No profiles yet</h3><p>Save this setup to start a list you can switch between.</p></div>')+
  (data.problems.length?'<div class="notice">Some files could not be read: '+data.problems.map(esc).join('; ')+'</div>':'');
}

$('content').addEventListener('submit',async e=>{
 if(e.target.id!=='profile-save-form')return;
 e.preventDefault();
 try{
  await saveQueue;
  const data=await api('/api/profiles/save',{fleetId:activeId,revision:state.revision,name:$('f-profile-name').value,
   description:$('f-profile-description').value,overwrite:$('f-profile-overwrite').checked});
  profileCache.set(activeId,data);
  // Redraw directly: the focused submit button would otherwise count as editing and hold the old list.
  renderProfiles();updateDesignTabs();toast('Profile saved.');
 }catch(error){toast(error.message,true);}
});
$('content').addEventListener('click',async e=>{
 const b=e.target.closest('[data-profile-switch],[data-profile-delete]');if(!b)return;
 try{
  if(b.dataset.profileSwitch)await switchProfile(b.dataset.profileSwitch);
  if(b.dataset.profileDelete&&window.confirm('Delete this profile file? The fleet itself is not changed.')){
   await api('/api/profiles/delete',{slug:b.dataset.profileDelete});await loadProfiles();toast('Profile deleted.');
  }
 }catch(error){toast(error.message,true);}
});

const profilesOriginalRenderContent=renderContent;
renderContent=function(){
 if(view!=='profiles')return profilesOriginalRenderContent();
 if(!state)return;
 if(!renderForced&&editingContent())return;
 renderProfiles();
};
const profilesOriginalRenderAll=renderAll;
renderAll=function(){
 profilesOriginalRenderAll();
 if(!state)return;
 // A profile list from another fleet or before a deploy is stale; fetch it again when the Workflow screen opens.
 if(['board','roles','workflow','profiles'].includes(view)&&!profileCache.has(activeId))loadProfiles().catch(()=>{});
 if(view!=='profiles')return;
 $('panel-title').textContent='Profiles';
 $('workspace-layout').classList.add('board-layout');$('status-filter').classList.add('hidden');$('board-toolbar').classList.add('hidden');
 $('search').placeholder='Find a profile…';
};
