function esc(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]))}
async function loadOrders(){
  const r=await fetch('/api/orders',{cache:'no-store'});if(!r.ok)return;const d=await r.json();
  const scope=window.ADMIN_SCOPE||'ALL';
  const rows=d.filter(o=>scope==='ALL'||o.area===scope);
  const counts={WAITING:0,PICKING:0,COMPLETED:0,SHORT:0};
  rows.forEach(o=>{if(o.status==='WAITING')counts.WAITING++;else if(o.status==='PICKING')counts.PICKING++;else if(o.status==='COMPLETED')counts.COMPLETED++;if((o.shortage_lines||0)>0)counts.SHORT++});
  const sum=document.getElementById('summary');
  if(sum)sum.innerHTML=`<div class="metric"><span>WAITING</span><b>${counts.WAITING}</b></div><div class="metric"><span>PICKING</span><b>${counts.PICKING}</b></div><div class="metric"><span>COMPLETED</span><b>${counts.COMPLETED}</b></div><div class="metric alert"><span>SHORT PICK</span><b>${counts.SHORT}</b></div>`;
  const el=document.getElementById('orders');if(!el)return;
  el.innerHTML=rows.map(o=>{
    const pct=o.units?Math.round((o.picked/o.units)*100):0;
    const status=(o.status||'').toLowerCase().replace(/_/g,' ');
    const short=o.shortage_lines||0;
    const source=o.is_shortage?`<span class="short-badge">SHORTAGE · FROM ${esc(o.source_order_no||'-')}</span>`:''; return `<div class="order-monitor"><div class="order-main">${source}<div class="order-store"><span class="area-badge">${esc(o.area)}</span><b>${esc(o.store)}</b></div><strong>${esc(o.order_no)}</strong><div class="order-meta">${o.lines} lines · ${o.picked}/${o.units} units · Picker: ${esc(o.assigned_to||'-')}</div></div><div class="order-right"><span class="status status-${esc(o.status)}">${esc(status)}</span>${short?`<span class="short-badge">${short} SHORT</span>`:''}<div class="progress-track"><i style="width:${pct}%"></i></div><small>${pct}% picked</small>${o.status==='COMPLETED'?`<div class="export-actions"><button class="gray small" onclick="downloadExport('${o.id}','warehouse-transfer')">TRANSFER</button><button class="gray small" onclick="downloadExport('${o.id}','shortage')">SHORTAGE</button></div>`:''}<button class="danger small" onclick="deleteOrder('${o.id}','${esc(o.order_no)}')">DELETE</button></div></div>`;
  }).join('')||'<div class="empty">No orders in this area.</div>';
  const lr=document.getElementById('lastRefresh');if(lr)lr.textContent='Updated '+new Date().toLocaleTimeString();
}
function downloadExport(id,type){location.href='/api/export/'+id+'/'+type}
async function upload(){
 const f=document.getElementById('file')?.files[0];if(!f){document.getElementById('um').textContent='Select an Excel file';return}
 const fd=new FormData();fd.append('area',document.getElementById('uploadArea').value);fd.append('file',f);document.getElementById('um').textContent='Uploading...';
 const r=await fetch('/api/admin/orders',{method:'POST',body:fd});const d=await r.json();document.getElementById('um').textContent=r.ok?`✓ Uploaded ${d.area} · ${d.store} · ${d.lines} lines · ${d.units} units`:'✕ Failed: '+(d.detail||'Upload failed');if(r.ok)document.getElementById('file').value='';
}
async function backfillNames(){const el=document.getElementById('bm');el.textContent='Searching uploaded Excel files...';const r=await fetch('/api/admin/backfill-product-names',{method:'POST'});const d=await r.json();el.textContent=r.ok?`✓ Recovered ${d.updated} Product Names.`:'✕ '+(d.detail||'Failed');}
async function loadStores(){
 const r=await fetch('/api/stores',{cache:'no-store'});if(!r.ok)return;const d=await r.json();const scope=window.ADMIN_SCOPE||'ALL';const sel=document.getElementById('storeFilter');const area=sel?.value||(scope!=='ALL'?scope:'NZ');const rows=d.filter(s=>s.area===area&&(scope==='ALL'||s.area===scope));const el=document.getElementById('stores');if(!el)return;
 el.innerHTML=rows.map(s=>`<div class="store"><span class="store-dot"></span><div><b>${esc(s.name)}</b><small>${esc(s.area)} AREA</small></div></div>`).join('')||`<div class="empty">No ${esc(area)} stores configured.</div>`;
}
async function addStore(){const p={name:document.getElementById('storeName').value,area:document.getElementById('storeArea').value};const r=await fetch('/api/admin/stores',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const d=await r.json();document.getElementById('storeMsg').textContent=r.ok?'✓ Store added':'✕ '+(d.detail||'Failed');if(r.ok){document.getElementById('storeName').value='';loadStores();}}
async function loadStaff(){const r=await fetch('/api/admin/employees',{cache:'no-store'});if(!r.ok)return;const d=await r.json();const el=document.getElementById('staff');if(!el)return;el.innerHTML=d.map(e=>`<div class="staff"><div><b>${esc(e.id)}</b> · ${esc(e.name)}</div><small>${esc(e.area)} · ${esc(e.role)} · ${e.active?'ACTIVE':'DISABLED'}</small><div class="staff-actions"><button class="small" onclick="toggleStaff('${esc(e.id)}')">${e.active?'DISABLE':'ENABLE'}</button><button class="gray small" onclick="resetPassword('${esc(e.id)}')">RESET PASSWORD</button></div></div>`).join('')||'<div class="empty">No employees.</div>';}
async function addEmployee(){const p={id:document.getElementById('nid').value,name:document.getElementById('nname').value,password:document.getElementById('npw').value,area:document.getElementById('narea').value,role:document.getElementById('role').value};const r=await fetch('/api/admin/employees',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const d=await r.json();document.getElementById('em').textContent=r.ok?'✓ Employee created':'✕ '+(d.detail||'Failed');if(r.ok){document.getElementById('nid').value='';document.getElementById('nname').value='';document.getElementById('npw').value='';loadStaff();}}
async function toggleStaff(id){await fetch('/api/admin/employees/'+encodeURIComponent(id)+'/toggle',{method:'POST'});loadStaff()}
async function resetPassword(id){const p=prompt('New password (minimum 6 characters)');if(!p)return;const r=await fetch('/api/admin/employees/'+encodeURIComponent(id)+'/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});const d=await r.json();alert(r.ok?'Password reset':(d.detail||'Failed'));}

async function deleteOrder(id,orderNo){
  if(!confirm('Delete order '+orderNo+'? This will permanently delete the order, picking records and uploaded Excel copy.')) return;
  const r=await fetch('/api/admin/orders/'+encodeURIComponent(id)+'/delete',{method:'POST'});
  const d=await r.json().catch(()=>({}));
  if(!r.ok){alert(d.detail||'Delete failed');return;}
  loadOrders();
}
