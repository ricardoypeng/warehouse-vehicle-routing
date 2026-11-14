/* Check frontend limits against the running service's configured settings. */
'use strict';
const assert=require('node:assert/strict');
const [debugPort='9222',serverPort='8770']=process.argv.slice(2);
(async()=>{
  const base=`http://127.0.0.1:${serverPort}`;
  const settings=await(await fetch(base+'/api/settings')).json();
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
    for(let attempt=0;attempt<400;attempt++){
      ready=await evaluate("typeof state!=='undefined'&&state&&!busy&&!resetting");
      if(ready)break;
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    assert(ready,'World did not load');
    const ui=await evaluate(`({
      bounds:Object.fromEntries(configNumbers.map(name=>[name,[Number($(name).min),Number($(name).max)]])),
      pageSize:LIST_PAGE_SIZE,canvasSpan:MAX_CANVAS_SPAN,
      bootstrapTimeout:Number(document.querySelector('meta[name="request-timeout-ms"]').content),
      requestTimeout:uiSettings.request_timeout_ms,
      rate:[Number($('speed').min),Number($('speed').max)],
      batch:[Number($('batch').min),Number($('batch').max)],
      activity:[uiSettings.activity_limit,uiSettings.activity_display_limit]
    })`);
    assert.equal(ui.bootstrapTimeout,settings.frontend.request_timeout_ms);
    assert.equal(ui.requestTimeout,settings.frontend.request_timeout_ms);
    assert.deepEqual(ui.bounds,settings.numeric_bounds);
    assert.equal(ui.pageSize,settings.frontend.list_page_size);
    assert.equal(ui.canvasSpan,settings.frontend.max_canvas_span);
    assert.deepEqual(ui.rate,[settings.frontend.request_rate_min,settings.frontend.request_rate_max]);
    assert.deepEqual(ui.batch,[settings.frontend.step_batch_min,settings.frontend.step_batch_max]);
    assert.deepEqual(ui.activity,[settings.frontend.activity_limit,settings.frontend.activity_display_limit]);
    const activityCheck=await evaluate(`(async()=>{
      const saved=activity;
      activity=Array.from({length:uiSettings.activity_limit+5},(_,i)=>({tick:state.tick,text:'activity-check-'+i}));
      await advance(1);render();
      const result={retained:activity.length,displayed:document.querySelectorAll('#activity-log .event-row').length};
      activity=saved;render();
      return result;
    })()`);
    assert.equal(activityCheck.retained,settings.frontend.activity_limit);
    assert.equal(activityCheck.displayed,Math.min(settings.frontend.activity_limit,settings.frontend.activity_display_limit));
    assert(await evaluate("Array.from(document.querySelectorAll('.parking-card')).every(card=>card.textContent.includes('库内合计')&&card.textContent.includes('其中待出'))"));
    const rates=await evaluate(`(()=>{
      const before=$('speed').value;
      setRequestRate(${settings.frontend.request_rate_max}+100);
      const high=Number($('speed-value').value);
      setRequestRate(${settings.frontend.request_rate_min}-100);
      const low=Number($('speed-value').value);
      setRequestRate(before);
      return {high,low};
    })()`);
    assert.equal(rates.high,settings.frontend.request_rate_max);
    assert.equal(rates.low,settings.frontend.request_rate_min);
    console.log(JSON.stringify({passed:true,...ui,clampedRates:rates,activityCheck}));
  }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
