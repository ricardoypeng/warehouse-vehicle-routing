'use strict';
const $=id=>document.getElementById(id);
const colors=['#247b91','#bc7750','#6a80b8','#aa9240','#6d9c7e','#a2638c','#4d94a9','#b88b55'];
const phases={parked:'停车区待命',idle:'空闲',to_pickup:'前往取货',pickup:'取货服务',to_dropoff:'载货送往放货点',dropoff:'卸货服务',returning:'返回停车区'};
const cargoLabels={empty:'空载',loading:'取货中',loaded:'载货',unloading:'卸货中',waiting_pickup:'等待取货',in_transit:'运输中',delivered:'已交付'};
const taskLabels={scheduled:'未来发布',pending:'待分配',assigned:'执行中',completed:'已完成'};
const reasons={empty_plan:'暂无移动计划',planned_wait:'计划内等待',node_conflict:'多车争用同一格',edge_swap:'禁止交换位置',occupied_by_stopped_vehicle:'前车或目标格未腾空',dispatch_queue:'停车区出库排队'};
const configNumbers=['vehicles','tasks','seed','rows','columns','shelf_length','shelf_depth','service_ticks','release_interval','dispatch_interval'];
let uiSettings=null,LIST_PAGE_SIZE=0,MAX_CANVAS_SPAN=0;
const listLimits={fleet:LIST_PAGE_SIZE,task:LIST_PAGE_SIZE};
let identity=null,state=null,map=null,running=false,busy=false,timer=null,selection=null,catalog=null,currentConfig=null;
let pendingStep=null,resetting=false,playGeneration=0;
const algorithmDrafts={assignment:new Map(),planning:new Map()};
const paintSurface=document.createElement('canvas');
let cellIndex=new Map(),accessIndex=new Map(),accessByCell=new Map(),layout=null,activity=[];
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const color=v=>colors[v.id%colors.length];
const coord=id=>{const c=cellIndex.get(id);return c?`[${c.x}, ${c.y}]`:'—';};
const accessLabel=id=>accessByCell.get(id)?.id||`格点 ${id}`;
const cellSize=()=>Math.max(uiSettings.min_cell_size,Math.min(uiSettings.max_cell_size,Math.floor(MAX_CANVAS_SPAN/Math.max(1,map.width,map.height))));
const badge=v=>`<span class="cargo-badge ${esc(v.cargo_state)}" data-loaded="${v.is_loaded}">${esc(cargoLabels[v.cargo_state]||v.cargo_state)}</span>`;
// Patch in place: keep rows, focus and scroll containers alive across ticks.
function nodeKey(node){return node.nodeType===1&&node.hasAttribute('data-kind')?`${node.tagName}:${node.dataset.kind}:${node.dataset.id}`:null;}
function patchChildren(parent,desired){
  const wanted=Array.from(desired.childNodes),keyed=new Map(Array.from(parent.childNodes).map(n=>[nodeKey(n),n]).filter(([key])=>key!==null));
  for(let i=0;i<wanted.length;i++){
    const next=wanted[i],key=nodeKey(next);let current=key!==null?keyed.get(key):parent.childNodes[i];
    if(!current||current.nodeType!==next.nodeType||(current.nodeType===1&&current.tagName!==next.tagName)||nodeKey(current)!==key){current=next.cloneNode(true);parent.insertBefore(current,parent.childNodes[i]||null);continue;}
    if(current!==parent.childNodes[i])parent.insertBefore(current,parent.childNodes[i]||null);
    if(current.nodeType===3){if(current.data!==next.data)current.data=next.data;continue;}
    if(current.nodeType!==1)continue;
    for(const name of current.getAttributeNames())if(!next.hasAttribute(name))current.removeAttribute(name);
    for(const name of next.getAttributeNames())if(current.getAttribute(name)!==next.getAttribute(name))current.setAttribute(name,next.getAttribute(name));
    patchChildren(current,next);
  }
  while(parent.childNodes.length>wanted.length)parent.lastChild.remove();
}
function patchHTML(element,html){const template=document.createElement('template');template.innerHTML=html;patchChildren(element,template.content);}
function updateText(element,value){const next=String(value);if(element.textContent!==next)element.textContent=next;}
function chosenVehicle(){if(!selection)return null;if(selection.kind==='vehicle')return state.vehicles.find(v=>v.id===selection.id);if(selection.kind==='task'){const t=state.tasks.find(t=>t.id===selection.id);return t?.state==='assigned'?state.vehicles.find(v=>v.id===t.vehicle_id&&v.task_id===t.id):null;}return null;}
function chosenTask(){if(selection?.kind==='task')return state.tasks.find(t=>t.id===selection.id);const v=chosenVehicle();return v?.task_id!==null&&v?.task_id!==undefined?state.tasks.find(t=>t.id===v.task_id):null;}
function select(kind,id){selection={kind,id};render();}
async function api(url,method='GET',body){
  const controller=new AbortController();
  const timeout=uiSettings?.request_timeout_ms??Number(document.querySelector('meta[name="request-timeout-ms"]').content);
  const deadline=setTimeout(()=>controller.abort(),timeout);
  const options={method,signal:controller.signal};
  try{
    if(body!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}
    const response=await fetch(url,options),data=await response.json();
    if(!response.ok){const error=Error(data.error||response.statusText);error.status=response.status;throw error;}
    return data;
  }catch(error){
    if(controller.signal.aborted)throw Error(`请求超时（${timeout/1000} 秒）。操作可能仍在服务端执行，可重新连接当前世界查看进度。`);
    throw error;
  }finally{clearTimeout(deadline);}
}
function stalledMessage(){
  const waiting=state.vehicles.filter(vehicle=>vehicle.status==='waiting');
  const blocked=waiting.some(vehicle=>vehicle.blocked_reason&&vehicle.blocked_reason!=='planned_wait');
  const planned=waiting.some(vehicle=>vehicle.blocked_reason==='planned_wait');
  const detail=blocked?'可点选车辆查看受阻原因':planned?'当前车辆按计划等待':'可点选车辆查看当前状态';
  return `连续 ${state.metrics.stalled_ticks} tick 无移动，${detail}`;
}
function message(text,error=false){updateText($('message'),text);$('message').classList.toggle('error',error);}
function pause(){running=false;playGeneration++;clearTimeout(timer);timer=null;updateText($('play'),'开始运行');}
function controls(){
  const ready=catalog?.categories.assignment.length&&catalog?.categories.planning.length;
  $('create').disabled=resetting||!ready;$('apply-json').disabled=resetting||!ready;
  // Keep pause available while a step request is in flight. busy guards duplicates.
  for(const id of ['play','step'])$(id).disabled=resetting||!identity||Boolean(state?.finished);
}
function algorithmEntry(category){return catalog?.categories[category].find(entry=>entry.id===$(category).value);}
function algorithmOptions(category){
  const entry=algorithmEntry(category);if(!entry)return {};
  if(!algorithmDrafts[category].has(entry.id))algorithmDrafts[category].set(entry.id,Object.fromEntries((entry.parameters||[]).map(field=>[field.name,field.default])));
  return algorithmDrafts[category].get(entry.id);
}
function renderAlgorithmParameters(category){
  const entry=algorithmEntry(category),container=$(category+'-parameters');if(!entry){container.textContent='等待算法目录。';return;}
  const fields=entry.parameters||[],options=algorithmOptions(category),active=currentConfig?.[category]===entry.id;
  const heading=`<h3>${category==='assignment'?'任务分配':'移动规划'} · ${esc(entry.label)}</h3>`;
  const html=fields.map(field=>{
    const key=`parameter-${category}-${field.name}`,value=options[field.name];let input;
    const attributes=`id="${esc(key)}" data-category="${category}" data-parameter="${esc(field.name)}"`;
    if(field.choices?.length){input=`<select ${attributes}>${field.choices.map(choice=>`<option value="${esc(choice)}" ${String(choice)===String(value)?'selected':''}>${esc(choice)}</option>`).join('')}</select>`;}
    else if(field.type==='bool'){input=`<input ${attributes} type="checkbox" ${value?'checked':''}>`;}
    else if(field.type==='str'){input=`<input ${attributes} type="text" value="${esc(value)}">`;}
    else{input=`<input ${attributes} type="number" value="${esc(value)}" step="${field.type==='int'?'1':'any'}" ${field.minimum!==null&&field.minimum!==undefined?`min="${field.minimum}"`:''} ${field.maximum!==null&&field.maximum!==undefined?`max="${field.maximum}"`:''}>`;}
    const applied=active?currentConfig[category+'_options']?.[field.name]:undefined;
    return `<label class="parameter-field" for="${esc(key)}"><span>${esc(field.label)}</span>${input}<small>${esc(field.description)}</small><small class="parameter-applied">默认 ${esc(field.default)} · ${applied!==undefined?`当前生效 ${esc(applied)}`:'尚未应用'}</small></label>`;
  }).join('');
  container.innerHTML=heading+(fields.length?html:'<p class="hint no-parameters">此算法暂无可配置参数。</p>');
}
function describe(){
  for(const category of ['assignment','planning']){
    $(category+'-description').textContent=algorithmEntry(category)?.description||'';
    renderAlgorithmParameters(category);
  }
}
function parameterInputValue(field,input){
  if(field.type==='bool')return input.type==='checkbox'?input.checked:input.value==='true';
  if(field.choices?.length){
    const raw=input.value,match=field.choices.find(choice=>String(choice)===raw);
    return match!==undefined?match:raw;
  }
  if(field.type==='int'||field.type==='float')return input.value===''?null:Number(input.value);
  return input.value;
}
$('algorithm-parameters').addEventListener('input',event=>{
  const input=event.target;if(!input.dataset.parameter)return;
  const category=input.dataset.category,field=algorithmEntry(category).parameters.find(p=>p.name===input.dataset.parameter);
  algorithmOptions(category)[field.name]=parameterInputValue(field,input);
  $('config-json').value=JSON.stringify(configuration(),null,2);
});
$('reset-parameters').onclick=()=>{
  for(const category of ['assignment','planning'])algorithmDrafts[category].delete($(category).value);
  describe();$('config-json').value=JSON.stringify(configuration(),null,2);
};

function showParameterReport(report){
  const rows=[];
  for(const category of ['assignment','planning'])for(const change of report?.[category]?.adjustments||[]){
    const result=change.action==='ignored'?'已忽略未知参数':change.action==='defaults'?`已恢复全部默认参数 ${JSON.stringify(change.effective)}`:`已使用默认值 ${JSON.stringify(change.effective)}`;
    rows.push(`<li>${category==='assignment'?'任务分配':'移动规划'} · ${esc(change.parameter)}：${esc(change.requested)}，${esc(result)}</li>`);
  }
  $('parameter-adjustments').innerHTML=rows.length?`<strong>参数处理记录</strong><ul>${rows.join('')}</ul>`:'参数已按下方当前生效值应用。';
}
function render(){
  if(!state||!map)return;
  $('tick').textContent=state.tick;$('completed').textContent=`${state.metrics.completed_tasks} / ${state.metrics.total_tasks}`;
  $('wait').textContent=state.metrics.wait;$('on-road').textContent=`${state.metrics.loaded} / ${state.vehicles.filter(v=>v.cell_id!==null).length}`;
  const scale=cellSize();
  $('map-summary').textContent=`${map.width} × ${map.height} 格 · ${map.shelves.length} 组货架 · ${map.access_points.length} 个存取点${scale<uiSettings.max_cell_size?' · 大场景缩放显示':''}`;
  $('connection').textContent=`${state.backend.effective.toUpperCase()} · 独立场景 ${identity.slice(0,6)}`;
  const warning=state.backend.fallback_reason?'；已回退 CPU':'';
  if(state.finished){pause();message(`全部任务完成，小车全部回库${warning}`);}else if(state.metrics.stalled_ticks>=uiSettings.stall_warning_ticks)message(`${stalledMessage()}${warning}`);else message(`${running?'运行中':'已暂停'} · 小车按移动计划执行${warning}`);
  draw();renderParking();renderFleet();renderTasks();renderRoads();renderSelection();
  patchHTML($('activity-log'),activity.slice(-uiSettings.activity_display_limit).map(e=>`<div class="event-row"><span>T${e.tick}</span>${esc(e.text)}</div>`).join('')||'等待开始。');
}
function renderParking(){
  patchHTML($('parking-cards'),state.parking.map(p=>`<button class="parking-card" data-kind="parking" data-id="${p.id}"><strong>${p.id} ${p.id.startsWith('N')?'上':'下'}${p.id.endsWith('W')?'左':'右'}停车区</strong>库内合计 ${p.parked} · 其中待出 ${p.queued}<br><small>入 ${coord(p.entry)} / 出 ${coord(p.exit)}</small></button>`).join(''));
}
function renderListControls(kind,total,shown){
  $(kind+'-list-controls').hidden=total<=LIST_PAGE_SIZE;
  $(kind+'-collapse').disabled=listLimits[kind]<=LIST_PAGE_SIZE;
  $(kind+'-more').disabled=shown>=total;
  $(kind+'-all').disabled=shown>=total;
  updateText($(kind+'-visible-count'),`已显示 ${shown} / ${total}`);
}
function changeListLimit(kind,action){
  if(!state)return;
  const total=kind==='fleet'?state.vehicles.length:state.tasks.filter(task=>$('task-filter').value==='all'||task.state===$('task-filter').value).length;
  if(action==='collapse')listLimits[kind]=LIST_PAGE_SIZE;
  else if(action==='more')listLimits[kind]=Math.min(total,listLimits[kind]+LIST_PAGE_SIZE);
  else if(action==='all')listLimits[kind]=Infinity;
  if(kind==='fleet')renderFleet();else renderTasks();
  if(action==='collapse')$(kind+'-list').closest('.table-wrap').scrollTop=0;
}
for(const kind of ['fleet','task'])$(kind+'-list-controls').addEventListener('click',event=>{
  const button=event.target.closest('button[data-list-action]');if(!button||button.disabled)return;
  changeListLimit(kind,button.dataset.listAction);
});
function renderFleet(){
  const chosen=chosenVehicle(),shown=state.vehicles.slice(0,listLimits.fleet);
  $('fleet-summary').textContent=`${state.vehicles.length} 辆 · 载货 ${state.metrics.loaded} · 待出 ${state.metrics.queued}${state.vehicles.length>LIST_PAGE_SIZE?` · 已显示 ${shown.length} / ${state.vehicles.length}`:''}`;
  patchHTML($('fleet-list'),shown.map(v=>`<tr data-kind="vehicle" data-id="${v.id}" tabindex="0" aria-selected="${chosen?.id===v.id}" class="${chosen?.id===v.id?'selected':''}"><td><span class="vehicle-tag"><i style="background:${color(v)}"></i>V${v.id}</span></td><td>${v.cell_id!==null?coord(v.cell_id):`${esc(v.parking)} 停车区`}</td><td>${esc(phases[v.execution_state])}</td><td>${badge(v)}</td><td>${v.plan.length} 步</td><td>${v.wait}</td><td>${v.distance}</td><td>${v.completed_tasks}</td></tr>`).join(''));
  renderListControls('fleet',state.vehicles.length,shown.length);
}
function pointButton(id){const a=accessByCell.get(id);return `<button class="cell-button" data-kind="${a?'access':'cell'}" data-id="${a?a.id:id}">${esc(accessLabel(id))}<br>${coord(id)}</button>`;}
function renderTasks(){
  const picked=chosenTask(),filter=$('task-filter').value,m=state.metrics;
  const matched=state.tasks.filter(t=>filter==='all'||t.state===filter),shown=matched.slice(0,listLimits.task);
  $('task-summary').textContent=`待分配 ${m.pending_tasks} · 执行中 ${m.assigned_tasks} · 未来 ${m.scheduled_tasks} · 已完成 ${m.completed_tasks}${matched.length>LIST_PAGE_SIZE?` · 已显示 ${shown.length} / ${matched.length}`:''}`;
  patchHTML($('task-list'),shown.map(t=>`<tr data-kind="task" data-id="${t.id}" tabindex="0" aria-selected="${picked?.id===t.id}" class="${picked?.id===t.id?'selected':''}"><td>T${t.id}</td><td>${pointButton(t.pickup)}</td><td>${pointButton(t.dropoff)}</td><td>${esc(taskLabels[t.state])}</td><td>${t.vehicle_id!==null?`V${t.vehicle_id}`:'—'}</td><td>${esc(cargoLabels[t.cargo_state])}</td><td>${t.release_tick}</td><td>${t.assigned_tick??'—'}</td><td>${t.completed_tick??'—'}</td><td>${t.wait}</td></tr>`).join('')||'<tr><td colspan="10">当前筛选下没有任务</td></tr>');
  renderListControls('task',matched.length,shown.length);
}
function renderRoads(){
  patchHTML($('road-list'),state.roads.map(r=>`<tr data-kind="road" data-id="${r.id}" tabindex="0" class="${selection?.kind==='road'&&selection.id===r.id?'selected':''}"><td>Road ${r.id}</td><td>${r.direction}</td><td>${r.capacity}</td><td>${(r.occupancy*100).toFixed(1)}%</td><td>${r.blocked_ticks}</td><td>${(r.blocked_ratio*100).toFixed(1)}%</td></tr>`).join(''));
}
function renderSelection(){
  let title='点选详情',html='点击小车、任务、存取点或货架查看详情。';const v=chosenVehicle(),t=chosenTask();
  if(selection?.kind==='vehicle'&&v){title=`小车 V${v.id}`;html=`<div class="detail-title">${badge(v)} · ${esc(phases[v.execution_state])}</div><div class="detail-grid"><span>位置：${v.cell_id!==null?`${coord(v.cell_id)} · 格点 ${v.cell_id}`:`${esc(v.parking)} 停车区`}</span><span>当前目标：${v.goal_id!==null?`${esc(accessLabel(v.goal_id))} ${coord(v.goal_id)}`:'无'}</span><span>当前任务：${v.task_id!==null?`T${v.task_id}`:'无'}</span><span>服务剩余：${v.service_remaining} tick</span><span>连续 / 累计等待：${v.wait} / ${v.total_wait} tick</span><span>原因：${esc(reasons[v.blocked_reason]||v.blocked_reason||'无')}</span></div>${t?`<div>取货 ${esc(accessLabel(t.pickup))} ${coord(t.pickup)} → 放货 ${esc(accessLabel(t.dropoff))} ${coord(t.dropoff)}</div>`:''}<div class="detail-plan">剩余移动计划 ${v.plan.length} 步：${v.plan.length?esc(v.plan.map(id=>coord(id)).join(' → ')):v.cell_id===null?'出库后由算法生成移动计划':'当前没有剩余移动步骤'}</div>`;}
  else if(selection?.kind==='task'&&t){title=`任务 T${t.id}`;html=`<div class="detail-title">${esc(taskLabels[t.state])} · ${esc(cargoLabels[t.cargo_state])}</div><div class="detail-grid"><span>取货：${esc(accessLabel(t.pickup))} ${coord(t.pickup)}</span><span>放货：${esc(accessLabel(t.dropoff))} ${coord(t.dropoff)}</span><span>执行车辆：${t.vehicle_id!==null?`V${t.vehicle_id}`:'未分配'}</span><span>发布 / 接单：${t.release_tick} / ${t.assigned_tick??'—'}</span><span>到达取 / 放：${t.pickup_tick??'—'} / ${t.dropoff_tick??'—'}</span><span>完成：${t.completed_tick??'—'} · 等待 ${t.wait} tick</span></div><p class="hint">地图绿色「取」和橙色「放」标出任务端点；执行中车辆的实际剩余计划以虚线显示。</p>`;}
  else if(selection?.kind==='access'){const a=accessIndex.get(selection.id);if(a){title=`存取点 ${a.id}`;const tasks=state.tasks.filter(t=>t.pickup===a.cell_id||t.dropoff===a.cell_id);html=`坐标 ${coord(a.cell_id)} · 格点 ${a.cell_id} · 所属货架 ${esc(a.shelf_id)} · ${a.side==='N'?'北侧':'南侧'}<br>占用车辆：${state.occupancy[String(a.cell_id)]!==undefined?`V${state.occupancy[String(a.cell_id)]}`:'无'}<br>关联任务：${tasks.map(t=>`T${t.id}（${taskLabels[t.state]}）`).join('、')||'无'}<p class="hint">橙色点表示存取位置，不表示货架库存。</p>`;}}
  else if(selection?.kind==='shelf'){const shelf=map.shelves.find(s=>s.id===selection.id);if(shelf){title=`货架 ${shelf.id}`;html=`左上角 [${shelf.x}, ${shelf.y}] · ${shelf.width} × ${shelf.height} 格<br>上下两侧各 ${shelf.width} 个存取点。货架不可通行。<br>点击货架边的橙色点查看具体存取编号。`;}}
  else if(selection?.kind==='cell'){const c=cellIndex.get(selection.id);if(c){title=`格点 ${c.id}`;const owner=state.occupancy[String(c.id)];html=`坐标 ${coord(c.id)} · 类型 ${esc(c.kind)} · 道路 ${c.road_id??'路口 / 非道路'}<br>占用车辆：${owner!==undefined?`V${owner}`:'无'}<br>可通行下一格：${c.neighbors.map(coord).join('、')||'无'}`;}}
  else if(selection?.kind==='road'){const r=map.roads.find(r=>r.id===selection.id),m=state.roads.find(r=>r.id===selection.id);if(r){title=`单向道路 ${r.id}`;html=`方向 ${r.direction} · 容量 ${r.cells.length} 辆 · 平均占用率 ${(m.occupancy*100).toFixed(1)}%<br>格点：${r.cells.map(coord).join(' → ')}<br>堵塞 ${m.blocked_ticks} tick / ${(m.blocked_ratio*100).toFixed(1)}%`;}}
  else if(selection?.kind==='parking'){const p=state.parking.find(p=>p.id===selection.id);if(p){title=`${p.id} 停车区`;html=`库内 ${p.parked} 辆，其中待出 ${p.queued} 辆<br>入库 ${coord(p.entry)} · 出库 ${coord(p.exit)}<br>停放车辆：${state.vehicles.filter(v=>v.cell_id===null&&v.parking===p.id).map(v=>`V${v.id}`).join('、')||'无'}`;}}
  updateText($('vehicle-title'),title);patchHTML($('vehicle-info'),html);
}
function rounded(ctx,x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}
function cellFrame(ctx,id,label,tint){const c=cellIndex.get(id);if(!c)return;const s=layout.s;ctx.strokeStyle=tint;ctx.fillStyle=tint+'18';ctx.lineWidth=2;rounded(ctx,(c.x+.07)*s,(c.y+.07)*s,s*.86,s*.86,3);ctx.fill();ctx.stroke();if(label){ctx.fillStyle=tint;ctx.font=`bold ${s*.36}px sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(label,(c.x+.5)*s,(c.y+.5)*s);}}
function route(ctx,v,emphasized){if(v.cell_id===null||!v.plan.length)return;const s=layout.s,cells=[v.cell_id,...v.plan].map(id=>cellIndex.get(id));ctx.save();ctx.strokeStyle=color(v);ctx.globalAlpha=emphasized?.85:.25;ctx.lineWidth=emphasized?2.4:1.2;ctx.setLineDash([s*.2,s*.14]);ctx.beginPath();cells.forEach((c,i)=>i?ctx.lineTo((c.x+.5)*s,(c.y+.5)*s):ctx.moveTo((c.x+.5)*s,(c.y+.5)*s));ctx.stroke();ctx.setLineDash([]);for(let i=1;i<cells.length;i+=3){const a=cells[i-1],b=cells[i],dx=b.x-a.x,dy=b.y-a.y;if(!dx&&!dy)continue;const x=(b.x+.5)*s,y=(b.y+.5)*s;ctx.beginPath();ctx.moveTo(x-dx*5-dy*3,y-dy*5+dx*3);ctx.lineTo(x,y);ctx.lineTo(x-dx*5+dy*3,y-dy*5-dx*3);ctx.stroke();}ctx.restore();}
function draw(){
  const canvas=$('map'),ctx=paintSurface.getContext('2d'),s=cellSize(),ox=16,oy=58,w=map.width*s+ox*2,h=map.height*s+oy*2,ratio=window.devicePixelRatio||1;
  const pixelWidth=Math.round(w*ratio),pixelHeight=Math.round(h*ratio);
  for(const surface of [canvas,paintSurface]){if(surface.width!==pixelWidth)surface.width=pixelWidth;if(surface.height!==pixelHeight)surface.height=pixelHeight;}
  layout={s,ox,oy,w,h};ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(ox,oy);
  for(const c of map.cells){ctx.fillStyle=c.kind==='junction'?'#e2eff0':'#f0f4f5';ctx.fillRect(c.x*s,c.y*s,s,s);}
  ctx.strokeStyle='#dce5e8';ctx.lineWidth=.6;ctx.beginPath();for(let x=0;x<=map.width;x++){ctx.moveTo(x*s,0);ctx.lineTo(x*s,map.height*s);}for(let y=0;y<=map.height;y++){ctx.moveTo(0,y*s);ctx.lineTo(map.width*s,y*s);}ctx.stroke();
  if($('show-lanes').checked)for(const c of map.cells){if(c.kind!=='road'||!c.neighbors.length||(c.x+c.y)%2)continue;const n=cellIndex.get(c.neighbors[0]),dx=n.x-c.x,dy=n.y-c.y,x=(c.x+.5)*s,y=(c.y+.5)*s;ctx.strokeStyle='#adbec7';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x-dx*4,y-dy*4);ctx.lineTo(x+dx*4,y+dy*4);ctx.moveTo(x+dx*4-dx*3-dy*3,y+dy*4-dy*3+dx*3);ctx.lineTo(x+dx*4,y+dy*4);ctx.lineTo(x+dx*4-dx*3+dy*3,y+dy*4-dy*3-dx*3);ctx.stroke();}
  for(const shelf of map.shelves){const x=shelf.x*s+2,y=shelf.y*s+2,sw=shelf.width*s-4,sh=shelf.height*s-4,chosen=selection?.kind==='shelf'&&selection.id===shelf.id;ctx.fillStyle=chosen?'#96b4bf':'#a8bbc6';ctx.strokeStyle=chosen?'#267487':'#8da4b3';ctx.lineWidth=chosen?2.5:1;rounded(ctx,x,y,sw,sh,3);ctx.fill();ctx.stroke();ctx.lineWidth=.7;ctx.beginPath();for(let i=1;i<shelf.width;i++){ctx.moveTo((shelf.x+i)*s,y);ctx.lineTo((shelf.x+i)*s,y+sh);}for(let i=1;i<shelf.height;i++){ctx.moveTo(x,(shelf.y+i)*s);ctx.lineTo(x+sw,(shelf.y+i)*s);}ctx.stroke();ctx.fillStyle=chosen?'#96b4bf':'#a8bbc6';const labelWidth=Math.min(62,sw*.7);ctx.fillRect(x+(sw-labelWidth)/2,y+sh/2-8,labelWidth,16);ctx.fillStyle='white';ctx.font='bold 11px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(shelf.id,x+sw/2,y+sh/2);}
  if($('show-access').checked)for(const a of map.access_points){const picked=selection?.kind==='access'&&selection.id===a.id;ctx.fillStyle=picked?'#ad651d':'#d7a16a';ctx.beginPath();ctx.arc((a.x+.5)*s,(a.y+(a.side==='N'?.84:.16))*s,picked?4:2.3,0,Math.PI*2);ctx.fill();}
  const v=chosenVehicle(),t=chosenTask();if($('show-routes').checked){if($('all-routes').checked)for(const other of state.vehicles)if(other.id!==v?.id)route(ctx,other,false);if(v)route(ctx,v,true);}
  if(selection?.kind==='road'){const r=map.roads.find(r=>r.id===selection.id);if(r)for(const c of r.cells)cellFrame(ctx,c.id,'','#338a9c');}
  if(v?.goal_id!==null&&v?.goal_id!==undefined)cellFrame(ctx,v.goal_id,'',color(v));
  if(t){cellFrame(ctx,t.pickup,'取','#38896d');cellFrame(ctx,t.dropoff,'放','#b97839');}
  for(const p of state.parking){const west=p.id.endsWith('W'),north=p.id.startsWith('N'),bw=154,bx=west?0:map.width*s-bw,by=north?-49:map.height*s+12;ctx.fillStyle=selection?.kind==='parking'&&selection.id===p.id?'#dfefe7':'#f3f9f7';ctx.strokeStyle='#b8d1c7';ctx.lineWidth=1;rounded(ctx,bx,by,bw,36,5);ctx.fill();ctx.stroke();ctx.fillStyle='#34745b';ctx.font='bold 10px sans-serif';ctx.textAlign='left';ctx.textBaseline='middle';ctx.fillText(`${p.id} 停车区`,bx+8,by+11);ctx.font='10px sans-serif';ctx.fillStyle='#748c80';ctx.fillText(`库内合计 ${p.parked} · 其中待出 ${p.queued}`,bx+8,by+25);cellFrame(ctx,p.entry,'入','#5f9b82');cellFrame(ctx,p.exit,'出','#598aa4');}
  for(const car of state.vehicles){if(car.cell_id===null)continue;const c=cellIndex.get(car.cell_id),x=(c.x+.5)*s,y=(c.y+.5)*s;if(v?.id===car.id){ctx.fillStyle='white';ctx.strokeStyle=color(car);ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,s*.46,0,Math.PI*2);ctx.fill();ctx.stroke();}ctx.fillStyle=color(car);ctx.beginPath();ctx.arc(x,y,s*.32,0,Math.PI*2);ctx.fill();ctx.fillStyle='white';ctx.font='bold 10px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(car.id,x,y);ctx.fillStyle=car.is_loaded?'#edb148':'white';ctx.strokeStyle=car.is_loaded?'#ad7620':'#94a89b';ctx.lineWidth=1;rounded(ctx,x+s*.16,y+s*.13,s*.24,s*.22,1);ctx.fill();ctx.stroke();if(car.status==='waiting'||car.status==='queued'){ctx.fillStyle='#e6ad47';ctx.strokeStyle='white';ctx.beginPath();ctx.arc(x-s*.25,y-s*.25,3.4,0,Math.PI*2);ctx.fill();ctx.stroke();}if(car.status==='servicing'){ctx.strokeStyle='#7794ce';ctx.lineWidth=2.5;ctx.beginPath();ctx.arc(x,y,s*.38,-Math.PI*.8,Math.PI*.6);ctx.stroke();}}
  if(selection?.kind==='access'){const a=accessIndex.get(selection.id);if(a)cellFrame(ctx,a.cell_id,'','#bd873b');}else if(selection?.kind==='cell')cellFrame(ctx,selection.id,'','#bd873b');
  ctx.restore();
  // Publish a completed frame in one draw; never clear the visible canvas.
  const visible=canvas.getContext('2d');visible.setTransform(1,0,0,1,0,0);visible.globalCompositeOperation='copy';visible.drawImage(paintSurface,0,0);visible.globalCompositeOperation='source-over';
}
function configureUI(settings){
  uiSettings=settings.frontend;
  LIST_PAGE_SIZE=uiSettings.list_page_size;
  MAX_CANVAS_SPAN=uiSettings.max_canvas_span;
  listLimits.fleet=LIST_PAGE_SIZE;listLimits.task=LIST_PAGE_SIZE;
  for(const name of configNumbers){
    const input=$(name),bounds=settings.numeric_bounds[name];
    input.min=bounds[0];input.max=bounds[1];input.value=settings.scenario[name];
  }
  $('device').value=settings.scenario.device;
  for(const id of ['speed','speed-value']){
    $(id).min=uiSettings.request_rate_min;$(id).max=uiSettings.request_rate_max;
    $(id).value=uiSettings.request_rate_default;
  }
  $('batch').min=uiSettings.step_batch_min;$('batch').max=uiSettings.step_batch_max;
  $('batch').value=uiSettings.step_batch_default;
}
function configuration(){const config={assignment:$('assignment').value,planning:$('planning').value,device:$('device').value,assignment_options:{...algorithmOptions('assignment')},planning_options:{...algorithmOptions('planning')}};for(const name of configNumbers)config[name]=Number($(name).value);return config;}
function fillConfiguration(config){currentConfig=config;for(const category of ['assignment','planning'])if(config[category])algorithmDrafts[category].set(config[category],{...config[category+'_options']});for(const name of configNumbers)if(config[name]!==undefined)$(name).value=config[name];for(const name of ['assignment','planning','device'])if(config[name]!==undefined)$(name).value=config[name];$('config-json').value=JSON.stringify(config,null,2);describe();}
async function create(config,reuse=false){
  if(resetting)return;const requested=config||configuration();resetting=true;pause();controls();
  if(pendingStep)await pendingStep;
  busy=true;
  try{const result=reuse?await api('/api/worlds/first'):await api('/api/worlds','POST',{config:requested});
    listLimits.fleet=LIST_PAGE_SIZE;listLimits.task=LIST_PAGE_SIZE;identity=result.world_id;state=result.state;map=state.map;cellIndex=new Map(map.cells.map(c=>[c.id,c]));accessIndex=new Map(map.access_points.map(a=>[a.id,a]));accessByCell=new Map(map.access_points.map(a=>[a.cell_id,a]));
    selection=state.vehicles.length?{kind:'vehicle',id:state.vehicles[0].id}:null;activity=[{tick:state.tick,text:reuse?'已连接第一个世界，保留当前仿真进度。':'场景已重置。所有小车停在 NW，接单后逐一出库。'}];fillConfiguration(result.config||requested);showParameterReport(result.parameters);render();
    if(result.failed)message(`当前世界已停止：${result.failed}；可应用配置重置。`,true);
  }catch(error){message(error.message,true);}finally{busy=false;resetting=false;controls();}
}
async function advance(ticks){
  if(busy||resetting||!identity||state.finished)return;busy=true;controls();
  pendingStep=(async()=>{
    try{const result=await api(`/api/worlds/${identity}/step`,'POST',{ticks});state=result.state;for(const record of state.event_records||(state.events||[]).map(text=>({tick:state.tick,text})))activity.push(record);activity=activity.slice(-uiSettings.activity_limit);render();}
    catch(error){
      pause();
      if(error.status===404){
        // Defer reconnection until this request and pendingStep have settled.
        setTimeout(()=>create(undefined,true),0);
      }else message(error.message,true);
    }finally{busy=false;controls();}
  })();
  try{await pendingStep;}finally{pendingStep=null;}
}
async function loop(generation){
  if(!running||generation!==playGeneration)return;
  // A resumed run waits for the previous request before starting its own cycle.
  if(pendingStep)await pendingStep;
  if(!running||generation!==playGeneration)return;
  await advance(Number($('batch').value));
  if(running&&generation===playGeneration)timer=setTimeout(()=>loop(generation),1000/Number($('speed').value));
}
function setRequestRate(value){
  const parsed=Number(value);if(!uiSettings||!Number.isFinite(parsed)||value==='')return;
  const rate=Math.max(uiSettings.request_rate_min,Math.min(uiSettings.request_rate_max,Math.round(parsed)));$('speed').value=rate;$('speed-value').value=rate;
}
$('speed').oninput=()=>setRequestRate($('speed').value);
$('speed-value').oninput=()=>setRequestRate($('speed-value').value);
$('speed-value').onchange=()=>setRequestRate($('speed-value').value||$('speed').value);
$('create').onclick=()=>create();$('step').onclick=()=>{pause();advance(1);};
$('apply-json').onclick=()=>{try{const config=JSON.parse($('config-json').value);if(!config||Array.isArray(config)||typeof config!=='object')throw Error('配置必须是 JSON 对象');create(config);}catch(error){message(error.message,true);}};
$('play').onclick=()=>{if(running){pause();render();}else if(state&&!state.finished){running=true;$('play').textContent='暂停';loop(++playGeneration);}};
$('clear-selection').onclick=()=>{selection=null;render();};
for(const id of ['show-lanes','show-access','show-routes','all-routes'])$(id).onchange=()=>draw();
$('task-filter').onchange=()=>{listLimits.task=LIST_PAGE_SIZE;renderTasks();$('task-list').closest('.table-wrap').scrollTop=0;};for(const category of ['assignment','planning'])$(category).onchange=()=>{describe();$('config-json').value=JSON.stringify(configuration(),null,2);};
function chooseFromElement(element){const kind=element.dataset.kind,id=element.dataset.id;select(kind,['vehicle','task','cell','road'].includes(kind)?Number(id):id);}
for(const id of ['fleet-list','task-list','road-list','parking-cards']){
  $(id).addEventListener('click',event=>{const target=event.target.closest('[data-kind]');if(target){event.stopPropagation();chooseFromElement(target);}});
  $(id).addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&event.target.matches('tr[data-kind]')){event.preventDefault();chooseFromElement(event.target);}});
}
$('map').onclick=event=>{
  if(!map||!state||!layout)return;const box=$('map').getBoundingClientRect(),s=layout.s,x=(event.clientX-box.left)/box.width*layout.w-layout.ox,y=(event.clientY-box.top)/box.height*layout.h-layout.oy;
  for(const p of state.parking){const bx=p.id.endsWith('W')?0:map.width*s-154,by=p.id.startsWith('N')?-49:map.height*s+12;if(x>=bx&&x<=bx+154&&y>=by&&y<=by+36){select('parking',p.id);return;}}
  const cell=map.cells.find(c=>c.x===Math.floor(x/s)&&c.y===Math.floor(y/s));if(!cell)return;
  const vehicle=state.vehicles.find(v=>v.cell_id===cell.id);if(vehicle){select('vehicle',vehicle.id);return;}
  if($('show-access').checked){const point=map.access_points.find(a=>Math.hypot(x-(a.x+.5)*s,y-(a.y+(a.side==='N'?.84:.16))*s)<=s*.28);if(point){select('access',point.id);return;}}
  if(cell.kind==='shelf'){const shelf=map.shelves.find(sh=>cell.x>=sh.x&&cell.x<sh.x+sh.width&&cell.y>=sh.y&&cell.y<sh.y+sh.height);if(shelf){select('shelf',shelf.id);return;}}
  select('cell',cell.id);
};
(async()=>{try{const settings=await api('/api/settings');configureUI(settings);catalog=await api('/api/algorithms');for(const category of ['assignment','planning']){const select=$(category);for(const entry of catalog.categories[category]){const option=document.createElement('option');option.value=entry.id;option.textContent=entry.label;option.title=entry.description;select.append(option);}select.disabled=select.options.length===0;const preferred=settings.scenario[category];select.value=Array.from(select.options).some(option=>option.value===preferred)?preferred:(select.options[0]?.value||'');}if(!$('assignment').options.length||!$('planning').options.length)throw Error('缺少通过自检的算法');$('algorithm-status').textContent=`已通过自检 ${Object.values(catalog.categories).flat().length} 个；未通过 ${catalog.rejected.length} 个。`;$('create').disabled=false;await create(undefined,true);}catch(error){message(error.message,true);$('connection').textContent='启动检查失败';}})();
