/* Run against a service whose catalog omits the configured preferred IDs. */
'use strict';
const assert=require('node:assert/strict');
const [debugPort='9222',serverPort='8770']=process.argv.slice(2);
(async()=>{
  const base=`http://127.0.0.1:${serverPort}`;
  const [settings,catalog]=await Promise.all(['/api/settings','/api/algorithms'].map(async path=>(await fetch(base+path)).json()));
  const expected={};
  for(const category of ['assignment','planning']){
    assert(catalog.categories[category].length,'Missing available algorithm');
    assert(!catalog.categories[category].some(entry=>entry.id===settings.scenario[category]),'Preferred algorithm must be absent for this regression');
    expected[category]=catalog.categories[category][0].id;
  }
  const pages=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const target=pages.find(page=>page.type==='page'&&page.url.startsWith(base+'/'));
  assert(target,'Frontend page missing');
  const ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  let serial=0;const pending=new Map();let loaded=null;
  ws.addEventListener('message',event=>{
    const result=JSON.parse(event.data),request=pending.get(result.id);
    if(result.method==='Page.loadEventFired'&&loaded){loaded();loaded=null;}
    if(request){pending.delete(result.id);result.error?request.reject(Error(JSON.stringify(result.error))):request.resolve(result.result);}
  });
  const call=(method,params={})=>new Promise((resolve,reject)=>{
    const id=++serial;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));
  });
  const evaluate=async expression=>{
    const result=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const wait=async expression=>{
    for(let attempt=0;attempt<400;attempt++){
      if(await evaluate(expression))return;
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    throw Error('Timeout: '+expression);
  };
  const selection=()=>evaluate("({assignment:$('assignment').value,planning:$('planning').value})");
  try{
    await call('Page.enable');
    await wait("typeof state!=='undefined'&&state&&!busy&&!resetting");
    assert.deepEqual(await selection(),expected);
    assert.deepEqual(await evaluate('({assignment:currentConfig.assignment,planning:currentConfig.planning})'),expected);
    const before=await evaluate('identity');
    await evaluate("$('tasks').value=0;$('device').value='cpu';$('create').click()");
    await wait(`identity!==${JSON.stringify(before)}&&!busy&&!resetting`);
    assert.deepEqual(await selection(),expected);
    assert.deepEqual(await evaluate('({assignment:currentConfig.assignment,planning:currentConfig.planning})'),expected);
    assert.equal(await evaluate('state.tasks.length'),0);
    const after=await evaluate('identity');
    const navigation=new Promise(resolve=>{loaded=resolve;});
    await call('Page.reload');await navigation;
    await wait(`typeof state!=='undefined'&&state&&!busy&&!resetting&&identity===${JSON.stringify(after)}`);
    assert.deepEqual(await selection(),expected);
    console.log(JSON.stringify({passed:true,selected:expected,resetApplied:true,reloadReusedWorld:true}));
  }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
