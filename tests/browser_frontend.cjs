/* Run against a headless browser CDP endpoint. Artifacts stay in tmp. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const [debugPort,out,servicePort='8770']=process.argv.slice(2);if(!debugPort||!out)throw Error('Usage: node browser_frontend.cjs DEBUG_PORT OUTPUT_DIR [SERVER_PORT]');
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const pages=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
 const target=pages.find(p=>p.type==='page'&&p.url.includes(':'+servicePort));
 assert(target,'Frontend page not found');
 const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((ok,fail)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',fail,{once:true});});
 let serial=0;const pending=new Map();
 ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(pending.has(data.id)){const p=pending.get(data.id);pending.delete(data.id);data.error?p.reject(Error(JSON.stringify(data.error))):p.resolve(data.result);}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++serial;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const wait=async expression=>{for(let i=0;i<300;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25));}throw Error('Timeout: '+expression);};
 const screenshot=async name=>{const {data}=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(data,'base64'));};
 const assertions=[];const check=(name,passed)=>{assert(passed,name);assertions.push(name);};
 try{
  await call('Runtime.enable');await call('Page.enable');
  await wait(`typeof state!=='undefined' && state && map?.shelves?.length===9 && !busy`);
  check('9 grouped shelves and 126 named access points',await evaluate('map.shelves.length===9&&map.access_points.length===126&&map.access_points.every(a=>a.id.startsWith(a.shelf_id))'));
  const old=await evaluate('identity');
  await evaluate(`$('vehicles').value=1;$('tasks').value=1;$('service_ticks').value=2;$('batch').value=7;$('create').click()`);
  await wait(`identity!==${JSON.stringify(old)}&&!busy`);
  let previous=await evaluate('state.tick');await evaluate(`$('step').click()`);await wait(`state.tick===${previous+1}&&!busy`);
  check('single step stays one tick even when playback batch is seven',true);
  await evaluate(`document.querySelector('#fleet-list tr[data-id="0"]').click()`);
  check('vehicle ID zero selectable from fleet table',await evaluate(`selection.kind==='vehicle'&&selection.id===0&&document.querySelector('#fleet-list tr[data-id="0"]').classList.contains('selected')`));
  const seen=new Set();let recordedLoaded=false;
  for(let i=0;i<350;i++){
   const v=await evaluate('state.vehicles[0]');seen.add(v.cargo_state);
   if(v.is_loaded&&v.plan.length&&!recordedLoaded){
    await evaluate(`(()=>{const c=cellIndex.get(state.vehicles[0].cell_id),b=$('map').getBoundingClientRect();$('map').dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:b.left+(layout.ox+(c.x+.5)*layout.s)/layout.w*b.width,clientY:b.top+(layout.oy+(c.y+.5)*layout.s)/layout.h*b.height}));})()`);
    check('canvas click selects loaded vehicle and exposes actual remaining route',await evaluate(`selection.kind==='vehicle'&&selection.id===0&&chosenVehicle().is_loaded&&chosenVehicle().plan.length>0&&$('vehicle-info').textContent.includes('载货')`));
    await screenshot('loaded-vehicle-route');
    await evaluate(`document.querySelector('#task-list tr[data-id="0"]').click()`);
    check('task ID zero links assigned vehicle and pickup/dropoff',await evaluate(`selection.kind==='task'&&selection.id===0&&chosenVehicle().id===0&&chosenTask().pickup!==chosenTask().dropoff&&$('vehicle-title').textContent.includes('T0')`));
    await screenshot('selected-task');
    await evaluate(`document.querySelector('#task-list button[data-kind="access"]').click()`);
    check('task access button selects named access point rather than parent task row',await evaluate(`selection.kind==='access'&&$('vehicle-title').textContent.includes(selection.id)`));
    await screenshot('selected-access');
    await evaluate(`(()=>{const a=map.shelves[0],b=$('map').getBoundingClientRect();$('map').dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:b.left+(layout.ox+(a.x+a.width/2)*layout.s)/layout.w*b.width,clientY:b.top+(layout.oy+(a.y+a.height/2)*layout.s)/layout.h*b.height}));})()`);
    check('canvas click selects entire shelf with ID',await evaluate(`selection.kind==='shelf'&&selection.id===map.shelves[0].id`));
    await evaluate(`document.querySelector('#fleet-list tr[data-id="0"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))`);
    check('keyboard selection restores vehicle path',await evaluate(`selection.kind==='vehicle'&&selection.id===0`));recordedLoaded=true;
   }
   if(await evaluate('state.finished'))break;
   previous=await evaluate('state.tick');await evaluate(`$('step').click()`);await wait(`state.tick>${previous}&&!busy`);
  }
  check('empty/loading/loaded/unloading lifecycle observed', ['empty','loading','loaded','unloading'].every(s=>seen.has(s)));
  check('completed delivery clears cargo while retaining task history',await evaluate(`state.finished&&!state.vehicles[0].is_loaded&&state.tasks[0].cargo_state==='delivered'`));
  const prior=await evaluate('identity');await evaluate(`$('vehicles').value=8;$('tasks').value=24;$('batch').value=5;$('create').click()`);await wait(`identity!==${JSON.stringify(prior)}&&!busy`);
  await evaluate(`$('play').click()`);await wait('state.tick>=15');await evaluate(`$('play').click()`);await wait('!busy&&!running');
  check('play/pause controls drive server and stop loop',await evaluate('state.tick>=15&&!running'));
  for(let i=0;i<150;i++){
   if(await evaluate('state.vehicles.some(v=>v.is_loaded&&v.plan.length)'))break;
   previous=await evaluate('state.tick');await evaluate(`$('step').click()`);await wait(`state.tick>${previous}&&!busy`);
  }
  await evaluate(`(()=>{const v=state.vehicles.find(v=>v.is_loaded&&v.plan.length);document.querySelector('#fleet-list tr[data-id="'+v.id+'"]').click();$('all-routes').checked=true;$('all-routes').dispatchEvent(new Event('change'));})()`);
  await screenshot('fleet-overview');
  check('fleet overview includes loaded and empty vehicles, actual route and selection',await evaluate(`chosenVehicle().is_loaded&&state.vehicles.some(v=>!v.is_loaded)&&$('show-routes').checked&&$('all-routes').checked`));
  const snapshot=await evaluate('({tick:state.tick,vehicles:state.vehicles,selection,metrics:state.metrics})');
  fs.writeFileSync(path.join(out,'browser-result.json'),JSON.stringify({passed:true,assertions,snapshot},null,2));
  console.log(JSON.stringify({passed:true,assertions,tick:snapshot.tick}));
 }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
