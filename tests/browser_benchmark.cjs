/* Measure real frontend draw and render costs at three scenario scales. */
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
    for(let i=0;i<400;i++){
      if(await evaluate("typeof state!=='undefined'&&state&&!busy&&!resetting"))break;
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    const cases=[['default',{}],['medium',{rows:6,columns:6,vehicles:64,tasks:1000}],['maximum',{rows:12,columns:12,shelf_length:30,shelf_depth:12,vehicles:512,tasks:10000}]];
    const results=[];
    for(const [scale,config] of cases){
      const result=await evaluate(`(async()=>{
        const start=performance.now();await create({...${JSON.stringify(config)},device:'cpu'});
        if(resetting||busy)throw Error('World did not settle');
        for(const [key,value] of Object.entries(${JSON.stringify(config)}))if(currentConfig[key]!==value)throw Error('Scenario creation failed: '+key);
        const createMilliseconds=performance.now()-start;
        const drawSamples=[],renderSamples=[];
        for(let i=0;i<10;i++){
          let t=performance.now();draw();drawSamples.push(performance.now()-t);
          t=performance.now();render();renderSamples.push(performance.now()-t);
          await new Promise(resolve=>requestAnimationFrame(resolve));
        }
        return {config:currentConfig,cells:map.cells.length,accessPoints:map.access_points.length,createMilliseconds,drawSamples,renderSamples};
      })()`);
      results.push({scale,...result});
    }
    console.log(JSON.stringify({passed:true,userAgent:await evaluate('navigator.userAgent'),results}));
  }finally{ws.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
