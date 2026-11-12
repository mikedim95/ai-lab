const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/trace-timeline.js'),'utf8'),context);
const model=context.TraceTimelineModel,id='a'.repeat(32),meta={lab_id:9,difficulty:0,attempt:1};
const plain=value=>JSON.parse(JSON.stringify(value));
function setup(difficulty=0){
  const record=model.create(9,{difficulty,attempt:1},1,0);
  model.bind(record,id,{...meta,difficulty});record.stream_status='open';return record;
}
function send(record,stage,status='passed',reason='stage_completed',extra={}){
  const frame={trace_id:id,event_id:record.last_event_id+1,span_id:record.last_event_id+1,stage,status,
    timestamp:1,duration_ms:5,reason_code:reason,persisted:true,metadata:record.metadata,...extra};
  return model.apply(record,frame,100);
}
function end(record,outcome='NO_EXPLOIT',extra={}){return send(record,'trace','passed','trace_completed',{span_id:0,outcome,...extra});}
const node=(record,key,now=100)=>model.project(record,{},now).nodes.find(item=>item.id===key);

test('idle and immediate preparation contain no fabricated backend progress',()=>{
  const idle=model.project(null,{lab:9,difficulty:2},0);
  assert.equal(idle.phase,'idle');assert.equal(idle.outcome.key,'IDLE');
  assert(idle.nodes.every(item=>item.tone==='gray'&&item.status==='pending'));
  const pending=model.project(model.create(9,{difficulty:2,attempt:1},1,0),{},500);
  assert.equal(pending.phase,'preparing');assert.equal(pending.outcome.key,'PREPARING');
  assert(pending.nodes.every(item=>item.tone==='gray'));assert.equal(pending.duration_ms,0);
});

test('running events are blue and only their elapsed clock advances',()=>{
  const record=setup();send(record,'ollama','running','request_started');
  assert.equal(node(record,'ollama',350).tone,'blue');assert.equal(node(record,'ollama',350).duration_ms,255);
  assert.equal(node(record,'output',350).tone,'gray');assert.equal(node(record,'output',350).duration_ms,0);
});

test('successful components are green and unmet verification is amber',()=>{
  const record=setup();send(record,'input');send(record,'ollama','passed','model_response');
  send(record,'exploit_verification','passed','condition_not_met');end(record);
  assert.equal(node(record,'input').tone,'green');assert.equal(node(record,'ollama').tone,'green');
  assert.equal(node(record,'verification').tone,'amber');assert.equal(node(record,'verification').status,'passed');
  assert.equal(model.project(record).outcome.key,'NO_EXPLOIT');
});

test('explicit backend refusal evidence makes Ollama amber, never red',()=>{
  const record=setup();send(record,'ollama','passed','model_response');
  send(record,'exploit_verification','passed','model_refusal');end(record,'MODEL_REFUSED');
  assert.equal(node(record,'ollama').tone,'amber');assert.equal(node(record,'ollama').status_label,'Model refused');
  assert.equal(model.project(record).outcome.key,'MODEL_REFUSED');
});

test('model text is never used to infer a refusal or backend block',()=>{
  const record=setup();send(record,'ollama','passed','model_response',{content:'I refuse this request. BLOCKED.'});
  send(record,'exploit_verification','passed','condition_not_met');end(record);
  assert.equal(node(record,'ollama').tone,'green');assert.equal(model.project(record).outcome.key,'NO_EXPLOIT');
  assert(!JSON.stringify(model.project(record)).includes('I refuse'));
});

test('actual input and output rejections are red with a backend-block outcome',()=>{
  for(const [component,key] of [['policy','policy'],['output_protection','output']]){
    const record=setup();send(record,'ollama','passed','model_response');send(record,component,'blocked','output_rejected');end(record,'BACKEND_BLOCKED');
    assert.equal(node(record,key).tone,'red');assert.equal(node(record,'ollama').tone,'green');
    assert.equal(model.project(record).outcome.key,'BACKEND_BLOCKED');
  }
});

test('tool authorization denial cannot become a passed tool execution',()=>{
  const record=setup();send(record,'tool_authorization','blocked','tool_rejected');send(record,'tool_execution','skipped','tool_rejected');
  assert.equal(node(record,'tools').tone,'red');assert.equal(node(record,'tools').status,'blocked');
});

test('permitted native execution moves from confirmed blue execution to green',()=>{
  const record=setup();send(record,'tool_authorization','passed','tool_allowed');
  assert.equal(node(record,'tools').status,'pending');
  send(record,'tool_execution','running','stage_started',{span_id:2});
  assert.equal(node(record,'tools').tone,'blue');
  send(record,'tool_execution','passed','tool_executed',{span_id:2});
  assert.equal(node(record,'tools').tone,'green');assert.equal(node(record,'tools').status,'passed');
});

test('unused optional components remain gray and explicitly skipped',()=>{
  const record=setup();send(record,'tool_authorization','skipped','not_requested');send(record,'tool_execution','skipped','not_requested');
  send(record,'rag','skipped','not_enabled');send(record,'context','passed','context_built');
  assert.equal(node(record,'tools').status,'skipped');assert.equal(node(record,'tools').tone,'gray');
  assert.equal(node(record,'context').tone,'green');
});

test('a partially observed composite node cannot invent a passed component',()=>{
  const record=setup();send(record,'rag','passed','rag_completed');
  assert.equal(node(record,'context').status,'pending');assert.equal(node(record,'context').tone,'gray');
  send(record,'context','running');assert.equal(node(record,'context').tone,'blue');
});

test('source filtering is an enforced stage block, not an invented whole-request rejection',()=>{
  const record=setup(2);send(record,'rag','blocked','rag_sources_rejected');send(record,'context','passed','context_built');
  send(record,'exploit_verification','passed','condition_not_met');end(record,'NO_EXPLOIT');
  assert.equal(node(record,'context').tone,'red');assert.equal(model.project(record).outcome.key,'NO_EXPLOIT');
});

test('processing errors are labeled without inventing a security rejection',()=>{
  const record=setup();send(record,'ollama','error','ollama_error');end(record,'ERROR',{status:'error',reason_code:'trace_error'});
  assert.equal(node(record,'ollama').status,'error');assert.equal(node(record,'ollama').tone,'gray');
  assert.equal(model.project(record).outcome.key,'ERROR');
  assert(!model.project(record).nodes.some(item=>item.tone==='red'));
});

test('a canonical recovered success overrides the legacy terminal error summary',()=>{
  const record=setup();send(record,'ollama','error','ollama_error');
  send(record,'exploit_verification','passed','condition_not_met');send(record,'exploit_verification','passed','verified');
  end(record,'EXPLOIT_VERIFIED',{status:'error',reason_code:'trace_error'});
  assert.equal(model.project(record).outcome.key,'EXPLOIT_VERIFIED');assert.equal(node(record,'verification').tone,'green');
});

test('all six canonical outcomes are preserved from the backend enum',()=>{
  for(const outcome of ['COMPLETED','MODEL_REFUSED','BACKEND_BLOCKED','NO_EXPLOIT','EXPLOIT_VERIFIED','ERROR']){
    const record=setup();end(record,outcome);assert.equal(model.project(record).outcome.key,outcome);
  }
});

test('missing final evidence remains unconfirmed, including earlier native verification',()=>{
  const record=setup();send(record,'exploit_verification','passed','verified');
  end(record,null,{status:'error',reason_code:'trace_unavailable'});
  assert.equal(model.project(record).outcome.key,'UNAVAILABLE');
  const legacy=setup();end(legacy,null,{status:'error',reason_code:'trace_error'});
  assert.equal(model.project(legacy).outcome.key,'UNCONFIRMED');
  const ambiguous=setup();send(ambiguous,'exploit_verification','passed','verified');
  send(ambiguous,'tool_authorization','blocked','tool_rejected');end(ambiguous,null,{status:'blocked',reason_code:'trace_blocked'});
  assert.equal(model.project(ambiguous).outcome.key,'UNCONFIRMED');
  const latest=setup();send(latest,'exploit_verification','passed','verified');
  send(latest,'exploit_verification','passed','condition_not_met');end(latest,null);
  assert.equal(model.project(latest).outcome.key,'NO_EXPLOIT');
});

test('duplicates, gaps and foreign trace IDs cannot replace the current state',()=>{
  const record=setup();send(record,'input');const before=plain(model.project(record,{},100));
  for(const extra of [{event_id:1},{event_id:3},{trace_id:'b'.repeat(32),event_id:2}]){
    assert.equal(model.apply(record,{trace_id:id,event_id:2,span_id:2,stage:'policy',status:'blocked',timestamp:1,duration_ms:2,
      reason_code:'input_policy_rejected',metadata:meta,...extra},100),false);
  }
  assert.deepEqual(plain(model.project(record,{},100)),before);
});

test('difficulty and attempt metadata are immutable and never borrowed from the selector',()=>{
  const record=setup(1);send(record,'input');
  const frame={trace_id:id,event_id:2,span_id:2,stage:'policy',status:'blocked',timestamp:1,duration_ms:2,
    reason_code:'input_policy_rejected',metadata:{...meta,difficulty:2}};
  assert.equal(model.apply(record,frame,100),false);
  assert.equal(model.project(record,{difficulty:2},100).metadata.difficulty,1);
  assert.equal(model.apply(record,{...frame,metadata:{...meta,difficulty:1,attempt:2}},100),false);
});

test('Medium and Hard render identical paths when backend events are identical',()=>{
  const medium=setup(1),hard=setup(2);
  for(const record of [medium,hard]){send(record,'policy');send(record,'context');send(record,'rag','skipped','not_enabled');end(record);}
  assert.deepEqual(plain(model.project(medium).nodes),plain(model.project(hard).nodes));
  assert.notEqual(model.project(medium).metadata.difficulty,model.project(hard).metadata.difficulty);
});

test('paused subscriptions freeze active elapsed time and do not claim a failed request',()=>{
  const record=setup();send(record,'ollama','running','request_started');record.stream_status='error';record.pausedAt=250;
  assert.equal(node(record,'ollama',1000).duration_ms,155);assert.equal(node(record,'ollama',2000).duration_ms,155);
  assert.equal(node(record,'ollama').tone,'gray');assert.equal(model.project(record).outcome.key,'UNAVAILABLE');
});

test('per-span durations are updated rather than counted repeatedly',()=>{
  const record=setup();send(record,'session','waiting','lock_wait',{span_id:1,duration_ms:0});
  send(record,'session','running','lock_acquired',{span_id:1,duration_ms:20});
  send(record,'session','passed','session_loaded',{span_id:1,duration_ms:25});
  send(record,'session','passed','session_saved',{span_id:2,duration_ms:5});
  assert.equal(node(record,'session').duration_ms,30);
});

test('historical traces and completed traces reject late updates',()=>{
  const record=setup();send(record,'input');record.historical=true;
  assert.equal(send(record,'policy','blocked','input_policy_rejected'),false);
  record.historical=false;end(record,'NO_EXPLOIT');
  assert.equal(send(record,'policy','blocked','input_policy_rejected'),false);
});

test('diagnostics contain only the safe codebook and component projection',()=>{
  const record=setup();send(record,'input','passed','private-record-value',{control:'SecretBackendControl',evidence:'private receipt'});end(record);
  const serialized=JSON.stringify(model.project(record));
  for(const value of ['private-record-value','SecretBackendControl','private receipt'])assert(!serialized.includes(value));
  assert.equal(node(record,'input').reason_code,null);
});
