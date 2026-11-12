const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../static/trace-stream.js'),'utf8');

function transport(options={}) {
  const sources=[],notifications=[],timers=new Map(),listeners=new Map();
  let timerId=0,reservation=0;
  class FakeSource {
    constructor(url) { if(options.sourceError)throw Error('source failed');this.url=url;this.listeners=new Map();sources.push(this); }
    addEventListener(name,fn) { this.listeners.set(name,fn); }
    close() { this.closed=true; }
    emit(name,value,id=value?.event_id) { this.listeners.get(name)?.({data:typeof value==='string'?value:JSON.stringify(value),lastEventId:String(id??'')}); }
  }
  const context={Map,Set,Object,Number,JSON,Date:options.Date||Date,AbortController,EventSource:FakeSource,
    CustomEvent:class {constructor(name,{detail}){this.type=name;this.detail=detail;}},
    setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},
    clearTimeout(id){timers.delete(id);},
    fetch:options.fetch||(async()=>({ok:true,json:async()=>({trace_id:String(++reservation).padStart(32,'0')})})),
    addEventListener:(name,fn)=>listeners.set(name,fn),
    dispatchEvent:event=>{if(options.observerError)throw Error('observer failed');notifications.push(event);},
  };
  context.window=context;
  vm.createContext(context);vm.runInContext(source,context);
  const ui=context.ChatTraceTransport;
  ui.enable(true);
  return {ui,sources,notifications,timers,listeners,context,
    run(delay){const entry=[...timers].find(([,value])=>value.delay===delay);assert(entry);timers.delete(entry[0]);entry[1].fn();}};
}

function event(trace_id,event_id,extra={}) {
  return {trace_id,event_id,span_id:1,stage:'ollama',status:'running',timestamp:1000,
    duration_ms:2,reason_code:'request_started',persisted:true,...extra};
}

test('reservation exposes its ID and subscribes immediately without rendering UI',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  assert.match(handle.trace_id,/^[a-f0-9]{32}$/);
  assert.equal(view.sources[0].url,`/api/traces/${handle.trace_id}/events?after=0`);
  assert.equal(view.notifications[0].type,'panoptis:trace-start');
  assert.equal(view.ui.get(handle.trace_id).last_event_id,0);
});

test('ordered events update a frozen bounded projection',async()=>{
  const view=transport(),handle=await view.ui.start(9),current=view.sources[0];
  current.emit('stage',event(handle.trace_id,1,{ignored:'<private extra>'}));
  current.emit('stage',event(handle.trace_id,2,{status:'passed',reason_code:'model_response'}));
  const state=view.ui.get(handle.trace_id);
  assert.equal(state.last_event_id,2);assert.equal(state.events.length,2);
  assert.equal(state.events[0].ignored,undefined);
  assert(Object.isFrozen(state)&&Object.isFrozen(state.events)&&Object.isFrozen(state.events[0]));
});

test('duplicate and older frames cannot replace newer stage state',async()=>{
  const view=transport(),handle=await view.ui.start(9),current=view.sources[0];
  current.emit('stage',event(handle.trace_id,1));
  current.emit('stage',event(handle.trace_id,2,{status:'passed'}));
  current.emit('stage',event(handle.trace_id,1,{status:'blocked'}));
  current.emit('stage',event(handle.trace_id,2,{status:'blocked'}));
  const state=view.ui.get(handle.trace_id);
  assert.equal(state.events.length,2);assert.equal(state.events[1].status,'passed');
});

test('a gap reconnects from the last accepted ID and replays missing frames',async()=>{
  const view=transport(),handle=await view.ui.start(9),old=view.sources[0];
  old.emit('stage',event(handle.trace_id,1));
  old.emit('stage',event(handle.trace_id,3));
  assert(old.closed);assert.equal(view.ui.get(handle.trace_id).last_event_id,1);
  view.run(500);const current=view.sources[1];
  assert(current.url.endsWith('?after=1'));
  old.emit('stage',event(handle.trace_id,2,{status:'blocked'}));
  current.emit('stage',event(handle.trace_id,2));current.emit('stage',event(handle.trace_id,3));
  assert.equal(view.ui.get(handle.trace_id).last_event_id,3);
  assert.equal(view.ui.get(handle.trace_id).events[1].status,'running');
});

test('repeated connection error callbacks schedule only one reconnect',async()=>{
  const view=transport();await view.ui.start(9);
  view.sources[0].emit('error');view.sources[0].emit('error');
  assert.equal([...view.timers.values()].filter(timer=>timer.delay===500).length,1);
});

test('a finished span cannot regress to running even with a newer event ID',async()=>{
  const view=transport(),handle=await view.ui.start(9),current=view.sources[0];
  current.emit('stage',event(handle.trace_id,1));
  current.emit('stage',event(handle.trace_id,2,{status:'passed'}));
  current.emit('stage',event(handle.trace_id,3));
  assert.equal(view.ui.get(handle.trace_id).last_event_id,2);assert(current.closed);
  assert.equal(view.ui.get(handle.trace_id).events.at(-1).status,'passed');
});

test('foreign trace IDs and malformed frame fields never enter state',async()=>{
  for(const extra of [{trace_id:'f'.repeat(32)},{stage:'invented'},{status:'invented'},
    {duration_ms:-1},{reason_code:'private value with spaces'},{event_id:258},{span_id:-1}]) {
    const view=transport(),handle=await view.ui.start(9);
    view.sources[0].emit('stage',event(handle.trace_id,1,extra));
    assert.equal(view.ui.get(handle.trace_id).events.length,0);
  }
});

test('malformed JSON and mismatched SSE IDs reconnect without advancing the cursor',async()=>{
  for(const bad of ['{broken',null]) {
    const view=transport(),handle=await view.ui.start(9);
    view.sources[0].emit('stage',bad===null?event(handle.trace_id,1):bad,99);
    assert.equal(view.ui.get(handle.trace_id).last_event_id,0);assert(view.sources[0].closed);
  }
});

test('a durable terminal is accepted once and immediately cleans up the stream',async()=>{
  for(const status of ['passed','blocked','error']) {
    const view=transport(),handle=await view.ui.start(9),current=view.sources[0];
    current.emit('stage',event(handle.trace_id,1));
    current.emit('terminal',event(handle.trace_id,2,{span_id:0,stage:'trace',status,reason_code:'trace_completed'}));
    current.emit('terminal',event(handle.trace_id,2,{span_id:0,stage:'trace',status:'blocked'}));
    assert(current.closed);assert.equal(view.ui.get(handle.trace_id).terminal.status,status);
    assert.equal(view.ui.get(handle.trace_id).events.length,2);assert.equal(view.timers.size,0);
  }
});

test('temporary stream failures reconnect without claiming chat failed or advancing its durable cursor',async()=>{
  const view=transport(),handle=await view.ui.start(9),current=view.sources[0];
  current.emit('stage',event(handle.trace_id,1));
  current.emit('terminal',event(handle.trace_id,2,{span_id:0,stage:'trace',status:'error',
    reason_code:'stream_unavailable',scope:'stream',persisted:false}),undefined);
  const state=view.ui.get(handle.trace_id);
  assert.equal(state.last_event_id,1);assert.equal(state.terminal,null);assert.equal(state.stream_status,'reconnecting');
  assert.equal(view.notifications.filter(item=>item.type==='panoptis:trace-stream-error').length,1);
  assert.equal(view.notifications.at(-1).type,'panoptis:trace-stream-state');
  assert.equal(view.notifications.at(-1).detail.stream_status,'reconnecting');
  assert(current.closed);view.run(500);
  const replay=view.sources[1];assert(replay.url.endsWith('?after=1'));
  current.emit('terminal',event(handle.trace_id,2,{span_id:0,stage:'trace',status:'blocked'}));
  replay.emit('stage',event(handle.trace_id,2,{status:'passed',reason_code:'model_response'}));
  replay.emit('terminal',event(handle.trace_id,3,{span_id:0,stage:'trace',status:'passed',outcome:'NO_EXPLOIT'}));
  const completed=view.ui.get(handle.trace_id);
  assert.equal(completed.stream_status,'finished');assert.equal(completed.terminal.outcome,'NO_EXPLOIT');
  assert.deepEqual(Array.from(completed.events,event=>event.event_id),[1,2,3]);
  assert.equal(view.timers.size,0);
});

test('stream timeouts remain final and durable errors are never retried',async()=>{
  const expired=transport(),first=await expired.ui.start(9);
  expired.sources[0].emit('terminal',event(first.trace_id,1,{span_id:0,stage:'trace',status:'error',
    reason_code:'stream_timeout',scope:'stream',persisted:false}),undefined);
  assert.equal(expired.ui.get(first.trace_id).stream_status,'error');
  assert.equal(expired.ui.get(first.trace_id).terminal,null);assert.equal(expired.timers.size,0);
  const durable=transport(),second=await durable.ui.start(9);
  durable.sources[0].emit('terminal',event(second.trace_id,1,{span_id:0,stage:'trace',status:'error',
    reason_code:'trace_unavailable',outcome:null}));
  assert.equal(durable.ui.get(second.trace_id).stream_status,'finished');
  assert.equal(durable.ui.get(second.trace_id).terminal.status,'error');assert.equal(durable.timers.size,0);
});

test('temporary stream read retries retain the existing retry and lifetime bounds',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  for(let index=0;index<21;index++){
    view.sources.at(-1).emit('terminal',event(handle.trace_id,1,{span_id:0,stage:'trace',status:'error',
      reason_code:'stream_unavailable',scope:'stream',persisted:false}),undefined);
    if(index<20)view.run(500);
  }
  assert.equal(view.ui.get(handle.trace_id).stream_status,'error');assert.equal(view.timers.size,0);
  assert.equal(view.ui.get(handle.trace_id).last_event_id,0);
  let now=0;const old=transport({Date:{now:()=>now}}),first=await old.ui.start(9);now=900001;
  old.sources[0].emit('terminal',event(first.trace_id,1,{span_id:0,stage:'trace',status:'error',
    reason_code:'stream_unavailable',scope:'stream',persisted:false}),undefined);
  assert.equal(old.ui.get(first.trace_id).stream_status,'error');assert.equal(old.timers.size,0);
});

test('a new prompt supersedes old callbacks and keeps only one active source',async()=>{
  const view=transport(),first=await view.ui.start(9),old=view.sources[0],second=await view.ui.start(6);
  assert(old.closed);old.emit('stage',event(first.trace_id,1));
  assert.equal(view.ui.get(first.trace_id).events.length,0);
  view.sources[1].emit('stage',event(second.trace_id,1));
  assert.equal(view.ui.get(second.trace_id).last_event_id,1);
});

test('completed chat allows a bounded grace period for its terminal',async()=>{
  const view=transport(),handle=await view.ui.start(9);handle.complete();
  view.run(2500);assert(view.sources[0].closed);assert.equal(view.timers.size,0);
});

test('the response trace fence closes mismatched subscriptions',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  handle.validate('f'.repeat(32));assert(view.sources[0].closed);
  assert.equal(view.ui.get(handle.trace_id).stream_status,'superseded');
});

test('setup failures return null so ordinary chat can continue',async()=>{
  for(const fetch of [async()=>{throw Error('network failed');},async()=>({ok:false}),
    async()=>({ok:true,json:async()=>({trace_id:'unsafe'})})]) {
    const view=transport({fetch});assert.equal(await view.ui.start(9),null);assert.equal(view.sources.length,0);
  }
});

test('reservation setup is aborted after 750ms',async()=>{
  const view=transport({fetch:(_url,options)=>new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('aborted'))))});
  const pending=view.ui.start(9);view.run(750);assert.equal(await pending,null);assert.equal(view.timers.size,0);
});

test('capability negotiation leaves unsupported servers on their original chat flow',async()=>{
  const view=transport();view.ui.enable(false);assert.equal(await view.ui.start(9),null);assert.equal(view.sources.length,0);
});

test('cache, reconnects, and page lifetime have fixed bounds',async()=>{
  const view=transport();let first;
  for(let index=0;index<9;index++){const handle=await view.ui.start(9);if(!first)first=handle.trace_id;}
  assert.equal(view.ui.get(first),null);assert.equal(view.sources.filter(source=>!source.closed).length,1);
  view.listeners.get('pagehide')();assert.equal(view.sources.filter(source=>!source.closed).length,0);
  const retry=transport(),handle=await retry.ui.start(9);
  for(let index=0;index<21;index++){
    retry.sources.at(-1).emit('error');if(index<20)retry.run(500);
  }
  assert.equal(retry.ui.get(handle.trace_id).stream_status,'error');assert.equal(retry.timers.size,0);
});

test('observer and EventSource constructor failures do not reject setup',async()=>{
  const observer=transport({observerError:true}),handle=await observer.ui.start(9);
  observer.sources[0].emit('stage',event(handle.trace_id,1));assert.equal(observer.ui.get(handle.trace_id).last_event_id,1);
  const unavailable=transport({sourceError:true}),other=await unavailable.ui.start(9);
  assert(other);assert.equal(unavailable.ui.get(other.trace_id).stream_status,'error');assert.equal(unavailable.timers.size,0);
});

test('an explicitly unknown backend outcome remains unclassified rather than reconnecting',async()=>{
  const view=transport(),handle=await view.ui.start(9),current=view.sources[0];
  current.emit('terminal',event(handle.trace_id,1,{span_id:0,stage:'trace',status:'error',outcome:null,reason_code:'trace_unavailable'}));
  assert.equal(view.ui.get(handle.trace_id).terminal.outcome,null);
  assert.equal(view.ui.get(handle.trace_id).stream_status,'finished');
});

test('reservation metadata is validated, projected and frozen across start and get',async()=>{
  const view=transport({fetch:async()=>({ok:true,json:async()=>({trace_id:'1'.repeat(32),
    metadata:{lab_id:9,difficulty:2,attempt:4,private_receipt:'excluded'}})})});
  const handle=await view.ui.start(9),state=view.ui.get(handle.trace_id);
  assert.equal(JSON.stringify(state.metadata),JSON.stringify({lab_id:9,difficulty:2,attempt:4}));
  assert(Object.isFrozen(state.metadata)&&Object.isFrozen(handle.metadata));
  assert.equal(handle.metadata,state.metadata);
  assert.equal(view.notifications.find(item=>item.type==='panoptis:trace-start').detail.metadata,state.metadata);
});

test('valid event metadata can establish a legacy reservation binding without copying extra fields',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  assert.equal(view.ui.get(handle.trace_id).metadata,null);
  view.sources[0].emit('stage',event(handle.trace_id,1,{metadata:{lab_id:9,difficulty:1,attempt:3,hidden:'excluded'}}));
  const state=view.ui.get(handle.trace_id);
  assert.equal(state.metadata.attempt,3);assert.equal(state.metadata.hidden,undefined);
  assert.equal(state.events[0].metadata,state.metadata);assert(Object.isFrozen(state.events[0].metadata));
});

test('missing metadata stays a safe legacy null rather than inventing a difficulty',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  view.sources[0].emit('stage',event(handle.trace_id,1));
  assert.equal(view.ui.get(handle.trace_id).metadata,null);
  assert.equal(view.ui.get(handle.trace_id).events[0].metadata,null);
});

test('malformed reservation metadata cannot open a subscription',async()=>{
  for(const metadata of [{lab_id:9,difficulty:3,attempt:1},{lab_id:9,difficulty:0,attempt:0},
    {lab_id:9,difficulty:0,attempt:'1'},{lab_id:6,difficulty:0,attempt:1},[]]) {
    const view=transport({fetch:async()=>({ok:true,json:async()=>({trace_id:'1'.repeat(32),metadata})})});
    assert.equal(await view.ui.start(9),null);assert.equal(view.sources.length,0);assert.equal(view.timers.size,0);
  }
});

test('changed attempt, difficulty or lab metadata fences a stream before accepting its event',async()=>{
  for(const changed of [{lab_id:9,difficulty:0,attempt:2},{lab_id:9,difficulty:1,attempt:1},
    {lab_id:6,difficulty:0,attempt:1}]) {
    const view=transport({fetch:async()=>({ok:true,json:async()=>({trace_id:'1'.repeat(32),
      metadata:{lab_id:9,difficulty:0,attempt:1}})})}),handle=await view.ui.start(9);
    view.sources[0].emit('stage',event(handle.trace_id,1,{metadata:changed}));
    assert.equal(view.ui.get(handle.trace_id).last_event_id,0);
    assert.equal(view.ui.get(handle.trace_id).stream_status,'superseded');assert(view.sources[0].closed);
  }
});

test('terminal outcomes are retained only from the explicit backend enum',async()=>{
  for(const outcome of ['COMPLETED','MODEL_REFUSED','BACKEND_BLOCKED','NO_EXPLOIT','EXPLOIT_VERIFIED','ERROR']) {
    const view=transport(),handle=await view.ui.start(9);
    view.sources[0].emit('terminal',event(handle.trace_id,1,{span_id:0,stage:'trace',status:'passed',outcome}));
    assert.equal(view.ui.get(handle.trace_id).terminal.outcome,outcome);
    assert(Object.isFrozen(view.ui.get(handle.trace_id).terminal));
  }
  const invalid=transport(),handle=await invalid.ui.start(9);
  invalid.sources[0].emit('terminal',event(handle.trace_id,1,{span_id:0,stage:'trace',status:'passed',outcome:'UNSUPPORTED'}));
  assert.equal(invalid.ui.get(handle.trace_id).terminal,null);
});

test('stage frames cannot supply a terminal outcome',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  view.sources[0].emit('stage',event(handle.trace_id,1,{outcome:'EXPLOIT_VERIFIED'}));
  assert.equal(view.ui.get(handle.trace_id).events[0].outcome,undefined);
  assert.equal(view.ui.get(handle.trace_id).terminal,null);
});

test('leaving while a reservation is pending aborts it and ignores a late response',async()=>{
  for(const cancel of [view=>view.ui.cancelPending(),view=>view.ui.closeAll(),
    view=>view.ui.close('unknown'),view=>view.listeners.get('pagehide')()]) {
    let release,signal;
    const view=transport({fetch:(_url,options)=>{signal=options.signal;return new Promise(resolve=>{release=resolve;});}});
    const pending=view.ui.start(9);cancel(view);
    assert.equal(signal.aborted,true);
    release({ok:true,json:async()=>({trace_id:'1'.repeat(32)})});
    assert.equal(await pending,null);assert.equal(view.sources.length,0);assert.equal(view.timers.size,0);
  }
});

test('cancellation also fences a reservation whose JSON body resolves later',async()=>{
  let release,bodyStarted;
  const began=new Promise(resolve=>{bodyStarted=resolve;});
  const view=transport({fetch:async()=>({ok:true,json:()=>new Promise(resolve=>{release=resolve;bodyStarted();})})});
  const pending=view.ui.start(9);await began;
  view.ui.cancelPending();release({trace_id:'1'.repeat(32)});
  assert.equal(await pending,null);assert.equal(view.sources.length,0);
});

test('overlapping reservation responses cannot replace the newer subscription',async()=>{
  const pending=[];
  const view=transport({fetch:()=>new Promise(resolve=>pending.push(resolve))});
  const first=view.ui.start(9),second=view.ui.start(6);
  pending[1]({ok:true,json:async()=>({trace_id:'2'.repeat(32),metadata:{lab_id:6,difficulty:0,attempt:1}})});
  const handle=await second;
  pending[0]({ok:true,json:async()=>({trace_id:'1'.repeat(32)})});
  assert.equal(await first,null);assert.equal(view.sources.length,1);assert.equal(handle.trace_id,'2'.repeat(32));
});

test('resume retains events and cursor without another reservation or a second active source',async()=>{
  const view=transport(),first=await view.ui.start(9);
  view.sources[0].emit('stage',event(first.trace_id,1));view.ui.close(first.trace_id);
  const second=await view.ui.start(6),old=view.sources[1];
  assert.equal(view.ui.resume(first.trace_id),true);assert(old.closed);
  assert.equal(view.sources[2].url,`/api/traces/${first.trace_id}/events?after=1`);
  assert.equal(view.ui.get(first.trace_id).events.length,1);
  old.emit('stage',event(second.trace_id,1));assert.equal(view.ui.get(second.trace_id).last_event_id,0);
  view.sources[2].emit('stage',event(first.trace_id,2,{status:'passed'}));
  assert.equal(view.ui.get(first.trace_id).last_event_id,2);
  assert.equal(view.sources.filter(source=>!source.closed).length,1);
});

test('resume respects the original lifetime and does not reopen completed traces',async()=>{
  let now=0;
  const view=transport({Date:{now:()=>now}}),handle=await view.ui.start(9);
  view.ui.close(handle.trace_id);now=10000;
  assert.equal(view.ui.resume(handle.trace_id),true);
  assert([...view.timers.values()].some(timer=>timer.delay===890000));
  now=900001;assert.equal(view.ui.resume(handle.trace_id),false);assert.equal(view.timers.size,0);
  const completed=transport(),other=await completed.ui.start(9);
  completed.sources[0].emit('terminal',event(other.trace_id,1,{span_id:0,stage:'trace',status:'passed'}));
  completed.ui.closeAll();assert.equal(completed.ui.get(other.trace_id).stream_status,'finished');
  assert.equal(completed.ui.resume(other.trace_id),false);
});

test('stream state notifications describe connection changes without fabricated pipeline events',async()=>{
  const view=transport(),handle=await view.ui.start(9);
  view.sources[0].emit('open');view.sources[0].emit('error');view.run(500);view.ui.close(handle.trace_id);
  const states=view.notifications.filter(item=>item.type==='panoptis:trace-stream-state').map(item=>item.detail.stream_status);
  assert.deepEqual(states,['connecting','open','reconnecting','connecting','closed']);
  assert.equal(view.notifications.filter(item=>item.type==='panoptis:trace-event').length,0);
});

test('an evicted incomplete trace restores from a witnessed cursor without reserving another prompt',async()=>{
  let reservations=0,now=1000;
  const view=transport({Date:{now:()=>now},fetch:async()=>({ok:true,json:async()=>({
    trace_id:String(++reservations).padStart(32,'0'),metadata:{lab_id:9,difficulty:0,attempt:1}})})});
  const first=await view.ui.start(9),bound=view.ui.get(first.trace_id).metadata;
  view.sources[0].emit('stage',event(first.trace_id,1,{metadata:bound}));view.ui.close(first.trace_id);
  for(let index=0;index<8;index++)await view.ui.start(9);
  assert.equal(view.ui.get(first.trace_id),null);assert.equal(view.ui.resume(first.trace_id),false);
  const count=reservations;now=2000;
  assert.equal(view.ui.restore(first.trace_id,{lab:9,metadata:bound,after:1,started:1000}),true);
  assert.equal(reservations,count);assert.equal(view.sources.at(-1).url,`/api/traces/${first.trace_id}/events?after=1`);
  assert.equal(view.ui.get(first.trace_id).events.length,0);assert.equal(view.ui.get(first.trace_id).last_event_id,1);
  assert.equal(view.sources.filter(source=>!source.closed).length,1);
  view.sources.at(-1).emit('terminal',event(first.trace_id,2,{stage:'trace',span_id:0,status:'passed',
    outcome:'NO_EXPLOIT',metadata:bound}));
  assert.equal(view.ui.get(first.trace_id).terminal.outcome,'NO_EXPLOIT');
});

test('restore rejects missing, malformed or inconsistent witnesses without opening a source',async()=>{
  const good={lab:9,metadata:{lab_id:9,difficulty:1,attempt:2},after:3,started:1000};
  for(const witness of [undefined,null,{},[],{...good,lab:6},{...good,after:-1},{...good,after:258},
    {...good,after:1.5},{...good,started:2001},{...good,started:-1},{...good,started:NaN},
    {...good,metadata:{lab_id:9,difficulty:3,attempt:2}}]) {
    const view=transport({Date:{now:()=>2000}});
    assert.equal(view.ui.restore('a'.repeat(32),witness),false);assert.equal(view.sources.length,0);
    assert.equal(view.timers.size,0);
  }
  const invalid=transport({Date:{now:()=>2000}});
  assert.equal(invalid.ui.restore('not-a-trace',good),false);
  assert.equal(invalid.ui.resume('a'.repeat(32)),false);
});

test('restored replay checks backend metadata and cannot merge a different attempt',async()=>{
  const view=transport({Date:{now:()=>2000}}),id='a'.repeat(32);
  const bound={lab_id:9,difficulty:0,attempt:1};
  assert.equal(view.ui.restore(id,{lab:9,metadata:bound,after:4,started:1000}),true);
  view.sources.at(-1).emit('stage',event(id,5,{metadata:{...bound,attempt:2}}));
  assert.equal(view.ui.get(id).last_event_id,4);assert.equal(view.ui.get(id).stream_status,'superseded');
  assert.equal(view.ui.get(id).events.length,0);
});

test('old traces get a five-second replay window without extending the ordinary live lifetime',async()=>{
  let now=0;
  const view=transport({Date:{now:()=>now}}),handle=await view.ui.start(9);
  view.ui.close(handle.trace_id);now=900001;
  assert.equal(view.ui.resume(handle.trace_id),false);
  assert.equal(view.ui.restore(handle.trace_id,{lab:9,metadata:{lab_id:9,difficulty:0,attempt:1},after:0,started:now}),true);
  assert([...view.timers.values()].some(timer=>timer.delay===5000));
  view.sources.at(-1).emit('error');
  assert([...view.timers.values()].some(timer=>timer.delay===500));
  view.run(500);assert(view.sources.at(-1).url.endsWith('?after=0'));
  view.run(5000);assert.equal(view.ui.get(handle.trace_id).stream_status,'timeout');
  assert.equal(view.ui.resume(handle.trace_id),false);assert.equal(view.timers.size,0);
});

test('completed local traces never reopen through restore and known metadata cannot be replaced',async()=>{
  const view=transport(),handle=await view.ui.start(9),now=Date.now();
  const witness={lab:9,metadata:{lab_id:9,difficulty:0,attempt:1},after:0,started:now};
  view.sources[0].emit('terminal',event(handle.trace_id,1,{stage:'trace',span_id:0,status:'passed',metadata:witness.metadata}));
  assert.equal(view.ui.restore(handle.trace_id,witness),false);assert.equal(view.sources.length,1);
  const other=transport({fetch:async()=>({ok:true,json:async()=>({trace_id:'a'.repeat(32),metadata:witness.metadata})})});
  const cached=await other.ui.start(9);
  assert.equal(other.ui.restore(cached.trace_id,{...witness,metadata:{...witness.metadata,difficulty:2}}),false);
  assert.equal(other.sources.length,1);
});
