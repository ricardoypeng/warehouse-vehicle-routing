/* Verify the parameter editor against an isolated browser and server. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const [debugPort,out,servicePort='8771']=process.argv.slice(2);
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const pages=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
 const target=pages.find(p=>p.type==='page'&&p.url.includes(':'+servicePort));assert(target);
 const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((ok,fail)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',fail,{once:true});});
 let serial=0;const pending=new Map();
 ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(pending.has(data.id)){const p=pending.get(data.id);pending.delete(data.id);data.error?p.reject(Error(JSON.stringify(data.error))):p.resolve(data.result);}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++serial;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const wait=async expression=>{for(let i=0;i<400;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25));}throw Error('Timeout: '+expression);};
 const assertions=[];const check=async(name,expression)=>{assert(await evaluate(expression),name);assertions.push(name);};
 try{
  await call('Runtime.enable');await call('Page.enable');await wait(`typeof state!=='undefined'&&state&&!busy`);
  const previous=await evaluate('identity');
  await evaluate(`$('planning').value='shortest_path';$('planning').dispatchEvent(new Event('change'));$('vehicles').value=2;$('tasks').value=4;$('device').value='cpu';$('batch').value=500;$('create').click()`);
  await wait(`identity!==${JSON.stringify(previous)}&&!busy`);
  await evaluate(`$('play').click()`);await wait('state.finished&&!busy');
  await check('batched playback retains all assignment and completion events',`state.event_records.length===8&&activity.filter(e=>e.text.includes('assigned')||e.text.includes('completed')).length===8`);
  await check('activity uses original event ticks rather than batch end tick',`JSON.stringify(activity.filter(e=>e.text.includes('assigned')||e.text.includes('completed')))===JSON.stringify(state.event_records)&&state.event_records[0].tick===1&&new Set(state.event_records.map(e=>e.tick)).size>1&&state.event_records.every(e=>e.tick<state.tick)`);
  await check('rendered activity shows original timestamps',`$('activity-log').textContent.includes('T1')&&$('activity-log').textContent.includes('completed')`);
  await evaluate('advance(1)');
  await check('finished frontend does not replay events',`activity.filter(e=>e.text.includes('assigned')||e.text.includes('completed')).length===8`);
  const {data}=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,'batch-events.png'),Buffer.from(data,'base64'));
  fs.writeFileSync(path.join(out,'browser-result.json'),JSON.stringify({passed:true,assertions},null,2));console.log(JSON.stringify({passed:true,assertions}));
 }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
