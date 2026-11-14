/* Check an accepted explicit wait through a service loading probe_review_edges. */
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
  let serial=0;const pending=new Map();
  ws.addEventListener('message',event=>{
    const result=JSON.parse(event.data),request=pending.get(result.id);
    if(request){pending.delete(result.id);result.error?request.reject(Error(JSON.stringify(result.error))):request.resolve(result.result);}
  });
  const evaluate=expression=>new Promise((resolve,reject)=>{
    const id=++serial;
    pending.set(id,{resolve:result=>{
      if(result.exceptionDetails)reject(Error(JSON.stringify(result.exceptionDetails)));
      else resolve(result.result.value);
    },reject});
    ws.send(JSON.stringify({id,method:'Runtime.evaluate',params:{expression,returnByValue:true,awaitPromise:true}}));
  });
  try{
    let ready=false;
    for(let i=0;i<400;i++){
      ready=await evaluate("typeof state!=='undefined'&&state&&!busy&&!resetting");
      if(ready)break;
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    assert(ready,'World did not load');
    await evaluate("create({vehicles:1,tasks:5,assignment:'fifo',planning:'wait_once',device:'cpu'})");
    await evaluate('advance(2)');
    const result=await evaluate("(()=>{select('vehicle',0);return {reason:state.vehicles[0].blocked_reason,status:state.vehicles[0].status,detail:$('vehicle-info').textContent};})()");
    assert.equal(result.reason,'planned_wait');
    assert.equal(result.status,'waiting');
    assert(result.detail.includes('计划内等待'));
    const diagnostics=await evaluate(`(async()=>{
      const originalFetch=window.fetch,timeout=uiSettings.request_timeout_ms;
      const originalState=state;
      try{
        state={...state,metrics:{...state.metrics,stalled_ticks:uiSettings.stall_warning_ticks},vehicles:[{status:'waiting',blocked_reason:'planned_wait'}]};
        const plannedMessage=stalledMessage();
        state={...state,vehicles:[{status:'waiting',blocked_reason:'node_conflict'}]};
        const blockedMessage=stalledMessage();
        state=originalState;uiSettings.request_timeout_ms=40;
        const hang=signal=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));
        window.fetch=(url,options)=>hang(options.signal);
        await advance(1);
        const step={busy,pending:pendingStep!==null,running,message:$('message').textContent};
        await create();
        const reset={busy,resetting,message:$('message').textContent};
        window.fetch=async(url,options)=>({ok:true,json:()=>hang(options.signal)});
        let bodyTimeout=false;try{await api('/api/settings');}catch(error){bodyTimeout=error.message.includes('请求超时');}
        window.fetch=originalFetch;uiSettings.request_timeout_ms=timeout;
        const before=state.tick;await advance(1);
        return {plannedMessage,blockedMessage,step,reset,bodyTimeout,recovered:state.tick===before+1};
      }finally{window.fetch=originalFetch;uiSettings.request_timeout_ms=timeout;state=originalState;render();}
    })()`);
    assert(diagnostics.plannedMessage.includes('按计划等待'));
    assert(!diagnostics.plannedMessage.includes('受阻'));
    assert(diagnostics.blockedMessage.includes('受阻'));
    assert.equal(diagnostics.step.busy,false);assert.equal(diagnostics.step.pending,false);assert.equal(diagnostics.step.running,false);
    assert(diagnostics.step.message.includes('请求超时'));
    assert.equal(diagnostics.reset.busy,false);assert.equal(diagnostics.reset.resetting,false);
    assert(diagnostics.bodyTimeout);assert(diagnostics.recovered);
    console.log(JSON.stringify({passed:true,...result,diagnostics}));
  }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
