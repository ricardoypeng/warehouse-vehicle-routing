/* Verify one playback timer survives rapid pause/resume and reset races. */
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
    await wait("typeof state!=='undefined'&&state&&!busy&&!resetting");
    await evaluate("create({tasks:40,vehicles:2,device:'cpu'})");
    await evaluate(`(()=>{
      const p=globalThis.__playProbe={requests:0,active:new Set(),gateNext:true,held:false,release:null};
      p.fetch=globalThis.fetch.bind(globalThis);p.setTimeout=globalThis.setTimeout.bind(globalThis);p.clearTimeout=globalThis.clearTimeout.bind(globalThis);
      globalThis.setTimeout=(callback,delay,...args)=>{
        if(!String(callback).includes('loop'))return p.setTimeout(callback,delay,...args);
        const id=p.setTimeout(()=>{p.active.delete(id);callback(...args);},delay);
        p.active.add(id);return id;
      };
      globalThis.clearTimeout=id=>{p.active.delete(id);return p.clearTimeout(id);};
      globalThis.fetch=async(...args)=>{
        const step=String(args[0]).endsWith('/step');if(step)p.requests++;
        const response=await p.fetch(...args);
        if(step&&p.gateNext){p.gateNext=false;p.held=true;await new Promise(resolve=>{p.release=()=>{p.held=false;resolve();};});}
        return response;
      };
      $('batch').value=1;setRequestRate(1);$('play').click();
    })()`);
    await wait('__playProbe.held');
    await evaluate(`(()=>{for(let i=0;i<5;i++){$('play').click();$('play').click();}__playProbe.release();})()`);
    await wait('!busy&&__playProbe.requests>=2');
    const resumed=await evaluate('({requests:__playProbe.requests,timers:__playProbe.active.size,tick:state.tick,running})');
    assert.equal(resumed.requests,2);assert.equal(resumed.tick,2);assert.equal(resumed.timers,1);assert.equal(resumed.running,true);
    await evaluate("$('play').click()");
    await new Promise(resolve=>setTimeout(resolve,1200));
    const stopped=await evaluate('({requests:__playProbe.requests,timers:__playProbe.active.size,tick:state.tick,running})');
    assert.equal(stopped.requests,2);assert.equal(stopped.tick,2);assert.equal(stopped.timers,0);assert.equal(stopped.running,false);
    await evaluate("__playProbe.gateNext=true;$('play').click()");
    await wait('__playProbe.held');
    const reset=await evaluate(`(async()=>{
      const old=identity,work=create({tasks:12,vehicles:3,device:'cpu'});
      __playProbe.release();await work;
      return {changed:identity!==old,tick:state.tick,vehicles:state.vehicles.length,timers:__playProbe.active.size,running};
    })()`);
    assert.equal(reset.changed,true);assert.equal(reset.tick,0);assert.equal(reset.vehicles,3);assert.equal(reset.timers,0);assert.equal(reset.running,false);
    await new Promise(resolve=>setTimeout(resolve,1200));
    assert.equal(await evaluate('state.tick'),0);
    console.log(JSON.stringify({rapidResumeCount:5,resumed,stopped,reset}));
  }finally{
    try{await evaluate(`(()=>{pause();const p=globalThis.__playProbe;if(p){for(const id of p.active)p.clearTimeout(id);globalThis.fetch=p.fetch;globalThis.setTimeout=p.setTimeout;globalThis.clearTimeout=p.clearTimeout;}})()`);}finally{ws.close();}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
