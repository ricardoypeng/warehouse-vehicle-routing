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
  await evaluate(`$('planning').value='matrix_routes';$('planning').dispatchEvent(new Event('change'))`);
  await check('matrix model exposes five default parameter fields',`document.querySelectorAll('#planning-parameters [data-parameter]').length===5&&$('parameter-planning-distance_weight').value==='1'&&$('parameter-planning-occupancy_weight').value==='8'`);
  await evaluate(`for(const [name,value] of Object.entries({candidate_count:2,distance_weight:2.5,occupancy_weight:12})){$('parameter-planning-'+name).value=value;$('parameter-planning-'+name).dispatchEvent(new Event('input',{bubbles:true}));}`);
  await check('parameter edits are included in full JSON configuration',`JSON.parse($('config-json').value).planning_options.distance_weight===2.5`);
  let old=await evaluate('identity');await evaluate(`$('create').click()`);await wait(`identity!==${JSON.stringify(old)}&&!busy`);
  await check('server returns effective custom options and editor marks applied values',`currentConfig.planning_options.distance_weight===2.5&&currentConfig.planning_options.candidate_count===2&&$('planning-parameters').textContent.includes('当前生效 2.5')`);
  await evaluate(`$('parameter-planning-distance_weight').value=9;$('parameter-planning-distance_weight').dispatchEvent(new Event('input',{bubbles:true}));$('planning').value='shortest_path';$('planning').dispatchEvent(new Event('change'))`);
  await check('parameter-free algorithms show an explicit empty state',`$('planning-parameters').textContent.includes('暂无可配置参数')&&document.querySelectorAll('#planning-parameters [data-parameter]').length===0`);
  await evaluate(`$('planning').value='matrix_routes';$('planning').dispatchEvent(new Event('change'))`);
  await check('algorithm switches preserve drafts without changing active values',`$('parameter-planning-distance_weight').value==='9'&&currentConfig.planning_options.distance_weight===2.5`);
  await evaluate(`$('reset-parameters').click()`);
  await check('restore defaults changes draft only',`$('parameter-planning-distance_weight').value==='1'&&currentConfig.planning_options.distance_weight===2.5`);
  await evaluate(`$('planning').value='traffic_aware';$('planning').dispatchEvent(new Event('change'))`);
  await check('traffic-aware exposes its own two configurable fields',`document.querySelectorAll('#planning-parameters [data-parameter]').length===2&&$('parameter-planning-occupancy_penalty').value==='8'`);
  const tick=await evaluate('state.tick');await evaluate(`$('step').click()`);await wait(`state.tick>${tick}&&!busy`);
  await check('existing world can still step while unapplied parameter draft differs',`currentConfig.planning==='matrix_routes'&&currentConfig.planning_options.distance_weight===2.5`);
  await evaluate(`$('planning').value='matrix_routes';$('planning').dispatchEvent(new Event('change'))`);
  old=await evaluate('identity');
  await evaluate(`(()=>{const config=configuration();config.planning_options={candidate_count:'wrong',distance_weight:2.5,typo_weight:7};$('config-json').value=JSON.stringify(config);$('apply-json').click();})()`);
  await wait(`identity!==${JSON.stringify(old)}&&!busy`);
  await check('invalid JSON parameter values fall back while valid values survive',`currentConfig.planning_options.candidate_count===4&&currentConfig.planning_options.distance_weight===2.5&&!('typo_weight' in currentConfig.planning_options)&&$('parameter-planning-candidate_count').value==='4'`);
  await check('frontend reports ignored names and actual fallback values',`$('parameter-adjustments').textContent.includes('typo_weight')&&$('parameter-adjustments').textContent.includes('已忽略未知参数')&&$('parameter-adjustments').textContent.includes('已使用默认值 4')`);
  const {data}=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,'parameter-editor.png'),Buffer.from(data,'base64'));
  fs.writeFileSync(path.join(out,'browser-result.json'),JSON.stringify({passed:true,assertions},null,2));console.log(JSON.stringify({passed:true,assertions}));
 }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
