'use strict';
async function reconcileHandoff(id){
 const d=progressData(),h=d.handoffs.find(h=>h.id===id);if(!h)return;
 $('progress-detail-title').textContent='Reconcile '+h.name;
 $('progress-detail-body').innerHTML='<p>Record evidence that the recipient already received this handoff, or repair its missing source identity. Policy-only revalidation preserves the original assignment: unsent work returns to guarded delivery; received contracts become reportable under the new release without resending. Receipt/source repair does not send instructions. No action changes goals or grants technical acceptance.</p><form id="reconcile-form"><div class="field"><label for="reconcile-mode">Action</label><select id="reconcile-mode" name="mode"><option value="record-receipt">Record an external receipt</option><option value="repair-source">Repair the original unsent event</option><option value="refresh-release">Revalidate an assignment after a policy-only release</option></select></div><div class="field"><label for="reconcile-evidence">Evidence link on the original ticket</label><input id="reconcile-evidence" name="evidenceUrl" type="url" required value="'+esc(h.ticket||'')+'"></div><div class="field"><label for="reconcile-note">What establishes receipt or the source identity?</label><textarea id="reconcile-note" name="note" required maxlength="1800"></textarea></div><div class="field"><label for="reconcile-message">Recipient message supporting receipt (optional)</label><select id="reconcile-message" name="receiptMessageId"><option value="">Use linked tracker evidence</option></select></div><button class="button primary" type="submit">Preview reconciliation</button></form>';
 const form=$('reconcile-form');
 form.onsubmit=async e=>{e.preventDefault();try{
  const p=await api('/api/handoffs/reconcile/preview',{actionId:id,...Object.fromEntries(new FormData(form))});
  $('progress-detail-body').innerHTML='<p><strong>'+esc(p.effect)+'</strong></p><p>Original source: '+esc(p.source?.name||'Not established')+' · Recipient: '+esc(p.targetName)+'</p><p>'+esc(p.note)+'</p>'+evidenceLink(p.evidenceUrl,'Inspect receipt evidence')+'<p>Receipt provenance: operator reconciliation. The original event identity and delivery history are retained.</p><button class="button primary" id="apply-reconciliation">Apply reconciliation</button>';
  $('apply-reconciliation').onclick=async()=>{try{await api('/api/handoffs/reconcile/apply',{previewId:p.id});detailDialog.close();await loadProgress(true);toast(p.mode==='refresh-release'?'Assignment revalidated; current delivery guards apply.':'Handoff reconciled; no instruction sent.');}catch(error){toast(error.message,true);}};
 }catch(error){toast(error.message,true);}};
 const a=d.agents.find(a=>a.id===h.targetAgentId);
 if(a?.session.sessionId!==h.targetSessionId)return;
 try{
  const activity=await api('/api/activity?'+new URLSearchParams({fleetId:d.fleetId,agentId:h.targetAgentId}));
  if($('reconcile-form')!==form)return;
  $('reconcile-message').innerHTML+=[...activity.messages].reverse().filter(m=>['summary','progress'].includes(m.kind)).slice(0,20).map(m=>'<option value="'+esc(m.id)+'">'+esc(m.timestamp+' · '+m.text.slice(0,200))+'</option>').join('');
 }catch{/* Linked tracker evidence remains available when session activity is absent. */}
}
