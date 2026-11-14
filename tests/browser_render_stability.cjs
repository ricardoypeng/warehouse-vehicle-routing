/* Measure redraw churn and request-time flashing, including delayed responses. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const [port,out,mode='measure',servicePort='8770']=process.argv.slice(2);fs.mkdirSync(out,{recursive:true});
(async()=>{
 const pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json(),target=pages.find(p=>p.type==='page'&&p.url.includes(':'+servicePort));
 assert(target,'Frontend page missing');const ws=new WebSocket(target.webSocketDebuggerUrl);
 await new Promise((ok,fail)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',fail,{once:true});});
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const r=JSON.parse(e.data),p=pending.get(r.id);if(p){pending.delete(r.id);r.error?p.reject(Error(JSON.stringify(r.error))):p.resolve(r.result);}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const serial=++id;pending.set(serial,{resolve,reject});ws.send(JSON.stringify({id:serial,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const wait=async expression=>{for(let i=0;i<400;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25));}throw Error('Timeout: '+expression);};
 try{
  await wait(`typeof state!=='undefined'&&state&&!busy`);
  const old=await evaluate('identity');
  await evaluate(`$('vehicles').value=8;$('tasks').value=200;$('batch').value=1;$('speed').value=30;$('create').click()`);
  await wait(`identity!==${JSON.stringify(old)}&&!busy`);
  await evaluate(`(()=>{
   const probe=globalThis.__renderProbe={canvasResizes:0,playDisabledChanges:0,removedRows:0,blankFrames:0,frames:0,canvasHeightChanges:0,active:true};
   probe.row=document.querySelector('#fleet-list tr[data-id="0"]');probe.row.focus({preventScroll:true});
   probe.observer=new MutationObserver(records=>{for(const r of records){if(r.target===$('map')&&(r.attributeName==='width'||r.attributeName==='height'))probe.canvasResizes++;if(r.target===$('play')&&r.attributeName==='disabled')probe.playDisabledChanges++;if(r.type==='childList')for(const n of r.removedNodes)if(n.nodeType===1&&n.tagName==='TR')probe.removedRows++;}});
   probe.observer.observe($('map'),{attributes:true});probe.observer.observe($('play'),{attributes:true});
   for(const key of ['fleet-list','task-list','road-list'])probe.observer.observe($(key),{childList:true});
   const frame=()=>{if(!probe.active)return;probe.frames++;const c=$('map'),box=c.getBoundingClientRect();if(probe.lastHeight!==undefined&&Math.abs(box.height-probe.lastHeight)>.1)probe.canvasHeightChanges++;probe.lastHeight=box.height;
    const a=map.shelves[0],x=(layout.ox+(a.x+.2)*layout.s)*c.width/layout.w,y=(layout.oy+(a.y+.2)*layout.s)*c.height/layout.h;
    if(c.getContext('2d').getImageData(Math.floor(x),Math.floor(y),1,1).data[3]===0)probe.blankFrames++;
    requestAnimationFrame(frame);};requestAnimationFrame(frame);
   const realFetch=globalThis.fetch.bind(globalThis);probe.realFetch=realFetch;
   globalThis.fetch=async(...args)=>{const r=await realFetch(...args);if(String(args[0]).endsWith('/step'))await new Promise(done=>setTimeout(done,120));return r;};
  })()`);
  await evaluate(`$('play').click()`);await wait('busy');
  const during=await evaluate(`({disabled:$('play').disabled,opacity:getComputedStyle($('play')).opacity})`);
  await wait('state.tick>=12&&!busy');
  await evaluate('pause();render()');await wait('!busy&&!running');
  const results=await evaluate(`(()=>{const p=__renderProbe;p.active=false;p.observer.disconnect();globalThis.fetch=p.realFetch;return {canvasResizes:p.canvasResizes,playDisabledChanges:p.playDisabledChanges,removedRows:p.removedRows,blankFrames:p.blankFrames,frames:p.frames,canvasHeightChanges:p.canvasHeightChanges,rowRetained:p.row===document.querySelector('#fleet-list tr[data-id="0"]'),focusRetained:document.activeElement===p.row,tick:state.tick};})()`);
  results.requestDelayMs=120;results.playDuringRequest=during;
  // Paused redraws must be pixel-identical: catches accumulated scale transforms.
  results.repeatedDrawIdentical=await evaluate(`(()=>{const first=$('map').toDataURL();draw();draw();return first===$('map').toDataURL();})()`);
  results.pauseResponsive=await evaluate(`(async()=>{const initial=state.tick;$('play').click();while(!busy)await new Promise(r=>setTimeout(r,5));$('play').click();await new Promise(r=>setTimeout(r,240));return !running&&!busy&&state.tick===initial+1;})()`);
  if(!results.pauseResponsive)await evaluate('pause()');
  const screenshot=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,mode+'.png'),Buffer.from(screenshot.data,'base64'));
  fs.writeFileSync(path.join(out,mode+'.json'),JSON.stringify(results,null,2));console.log(JSON.stringify(results));
  if(mode==='fixed'){
   assert.equal(results.canvasResizes,0);assert.equal(results.playDisabledChanges,0);assert.equal(results.removedRows,0);
   assert.equal(results.blankFrames,0);assert.equal(results.canvasHeightChanges,0);assert(results.rowRetained);assert(results.focusRetained);
   assert.equal(during.disabled,false);assert.equal(during.opacity,'1');assert(results.repeatedDrawIdentical);assert(results.pauseResponsive);
  }
 }finally{ws.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
