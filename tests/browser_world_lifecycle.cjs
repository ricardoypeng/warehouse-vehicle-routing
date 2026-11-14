/* Verify first-world reuse across reloads and isolation from batch worlds. */
'use strict';
const assert=require('node:assert/strict');
const [debugPort='9222',serverPort='8770']=process.argv.slice(2);
(async()=>{
  const base=`http://127.0.0.1:${serverPort}`;
  const pages=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const target=pages.find(page=>page.type==='page'&&page.url.startsWith(base+'/'));
  assert(target,'Frontend page missing');
  const ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  let serial=0;const pending=new Map();let loaded=null;
  ws.addEventListener('message',event=>{
    const result=JSON.parse(event.data),request=pending.get(result.id);
    if(request){pending.delete(result.id);result.error?request.reject(Error(JSON.stringify(result.error))):request.resolve(result.result);}
    if(result.method==='Page.loadEventFired'&&loaded){loaded();loaded=null;}
  });
  const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++serial;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
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
  try{
    await call('Page.enable');
    await wait("typeof state!=='undefined'&&state&&!busy&&!resetting");
    await evaluate("create({tasks:12,vehicles:2,device:'cpu'})");
    await evaluate('advance(5)');
    const before=await evaluate('({id:identity,tick:state.tick,config:currentConfig})');
    const batch=await(await fetch(base+'/api/batch/worlds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:{tasks:0,vehicles:3,device:'cpu'}})})).json();
    assert(batch.world_id);assert.notEqual(batch.world_id,before.id);
    for(let reload=0;reload<3;reload++){
      const navigation=new Promise(resolve=>{loaded=resolve;});
      await call('Page.reload');await navigation;
      await wait("typeof state!=='undefined'&&state&&!busy&&!resetting");
      const after=await evaluate('({id:identity,tick:state.tick,config:currentConfig})');
      assert.deepEqual(after,before);
    }
    const external=await(await fetch(base+'/api/worlds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:{tasks:8,vehicles:5,device:'cpu'}})})).json();
    await evaluate('advance(1)');
    await wait(`identity===${JSON.stringify(external.world_id)}&&!busy&&!resetting`);
    assert.equal(await evaluate('state.tick'),0);
    assert.equal(await evaluate('state.vehicles.length'),5);
    await evaluate("create({tasks:0,vehicles:4,device:'cpu'})");
    const reset=await evaluate('({id:identity,tick:state.tick,vehicles:state.vehicles.length})');
    assert.notEqual(reset.id,before.id);assert.equal(reset.tick,0);assert.equal(reset.vehicles,4);
    const extra=await(await fetch(base+`/api/worlds/${batch.world_id}`)).json();
    assert.equal(extra.state.tick,0);assert.equal(extra.state.vehicles.length,3);
    const first=await(await fetch(base+'/api/worlds/first')).json();
    assert.equal(first.world_id,reset.id);
    assert.equal((await fetch(base+`/api/worlds/${batch.world_id}`,{method:'DELETE'})).status,404);
    assert.equal((await fetch(base+`/api/batch/worlds/${batch.world_id}`,{method:'DELETE'})).status,200);
    assert.equal((await fetch(base+`/api/batch/worlds/${reset.id}`,{method:'DELETE'})).status,200);
    const navigation=new Promise(resolve=>{loaded=resolve;});
    await call('Page.reload');await navigation;
    await wait("typeof state!=='undefined'&&state&&!busy&&!resetting");
    const recreated=await evaluate('({id:identity,tick:state.tick})');
    assert.notEqual(recreated.id,reset.id);assert.equal(recreated.tick,0);
    console.log(JSON.stringify({reloads:3,reused:before,reset,batchPreserved:true,batchOnlyDelete:true,externalResetRecovered:true,recreated}));
  }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
