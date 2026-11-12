'use strict';

(() => {
  const definitions = Object.freeze([
    {id:'input',label:'Input',parts:['input'],description:'The backend validates the submitted request.'},
    {id:'session',label:'Session',parts:['session'],description:'The backend loads and saves the current student attempt.'},
    {id:'policy',label:'Difficulty / Policy',parts:['policy'],description:'The backend applies the selected input policy.'},
    {id:'context',label:'Context / RAG',parts:['context','rag'],description:'The backend assembles context and, when enabled, retrieves reference material.'},
    {id:'ollama',label:'Ollama',parts:['ollama'],description:'The local model processes the supplied context.'},
    {id:'tools',label:'Tool Execution',parts:['tool_authorization','tool_execution'],description:'Native tool requests are checked and executed only when the backend permits them.'},
    {id:'output',label:'Output Protection',parts:['output_protection'],description:'The backend applies the selected output handling policy.'},
    {id:'verification',label:'Exploit Verification',parts:['exploit_verification'],description:'The backend evaluates this attempt against the lab objective.'},
  ].map(item=>Object.freeze({...item,parts:Object.freeze(item.parts)})));
  const components = {input:'Request validation',session:'Student session',policy:'Input policy',context:'Context assembly',
    rag:'Reference retrieval',ollama:'Local model',tool_authorization:'Tool authorization',tool_execution:'Tool execution',
    output_protection:'Output protection',exploit_verification:'Objective verification'};
  const reasonText = Object.freeze({
    stage_started:'The component started.',stage_completed:'The component finished.',lock_wait:'Waiting for the backend session.',
    lock_acquired:'The backend session is available.',input_validated:'The request format was accepted.',invalid_input:'The request format was rejected.',
    request_rejected:'The backend rejected this operation.',generation_busy:'The backend could not admit another model request.',
    session_loaded:'The current attempt was loaded.',session_saved:'The current attempt was saved.',policy_allowed:'The input policy allowed processing.',
    input_policy_rejected:'Input protection rejected the request.',rag_completed:'Reference retrieval finished.',
    rag_sources_rejected:'Reference screening excluded one or more sources.',not_enabled:'This component was not enabled.',
    input_rejected:'This component was skipped after input rejection.',context_built:'The model context was assembled.',
    request_started:'A real model request started.',model_response:'The model returned a response.',model_tool_request:'The model returned a native tool request.',
    non_production_model:'No production Ollama request ran.',tool_allowed:'The backend permitted the tool operation.',
    authorization_bypassed:'The selected configuration allowed the tool operation.',tool_rejected:'The backend denied the tool operation.',
    tool_executed:'The native tool operation finished.',tool_dispatch_allowed:'The native tool request was accepted for checking.',
    not_requested:'No native tool operation was requested.',output_allowed:'Output protection allowed the response.',
    output_rejected:'Output protection rejected or filtered the response.',verified:'The backend verified the current lab objective.',
    condition_not_met:'The check completed; the lab objective was not verified.',model_refusal:'Backend evidence explicitly identified a model-generated refusal.',
    stale_attempt:'The backend rejected an operation from an earlier attempt.',pipeline_error:'A processing operation failed.',
    ollama_error:'A model request failed. This is not evidence of a security rejection.',not_reached:'This component was not reached.',
    protection_rejected:'The evaluator was skipped after backend protection.',trace_completed:'Request processing finished.',
    trace_blocked:'A backend component enforced a rejection.',trace_error:'A processing error was recorded.',
    trace_unavailable:'Complete execution evidence is unavailable.',reservation_expired:'Trace setup expired.',
    stream_unavailable:'Live trace delivery is unavailable.',stream_timeout:'Live trace delivery reached its time limit.',
  });
  const outcomes = Object.freeze({
    COMPLETED:{label:'COMPLETED',tone:'green',description:'The request was processed normally.'},
    MODEL_REFUSED:{label:'MODEL REFUSED',tone:'amber',description:'Backend evidence confirms that the model explicitly refused the request.'},
    BACKEND_BLOCKED:{label:'BACKEND BLOCKED',tone:'red',description:'A backend control rejected the request, a tool operation, or the output.'},
    NO_EXPLOIT:{label:'NO EXPLOIT',tone:'amber',description:'Processing completed; the current lab objective was not verified.'},
    EXPLOIT_VERIFIED:{label:'EXPLOIT VERIFIED',tone:'green',description:'The backend verified this attempt’s winning condition. Flag submission remains separate.'},
    ERROR:{label:'ERROR',tone:'gray',description:'The request failed. A processing error does not by itself establish a security block.'},
  });
  const validMetadata = value => value && Number.isInteger(value.lab_id) && value.lab_id>=1 && value.lab_id<=9
    && Number.isInteger(value.difficulty) && value.difficulty>=0 && value.difficulty<=2
    && Number.isSafeInteger(value.attempt) && value.attempt>=1;
  const sameMetadata = (a,b) => a.lab_id===b.lab_id && a.difficulty===b.difficulty && a.attempt===b.attempt;
  const stageNames = new Set(definitions.flatMap(item=>item.parts));
  const statuses = new Set(['waiting','running','passed','blocked','skipped','error']);
  const finished = new Set(['passed','blocked','skipped','error']);
  const liveStates = new Set(['open','connecting','preparing']);

  function create(lab,context,generation,now=0) {
    return {lab,context:{...context},generation,trace_id:null,metadata:null,spans:new Map(),terminal:null,
      last_event_id:0,stream_status:'preparing',startedAt:now,pausedAt:null,historical:false,selected:null};
  }

  function metadataFits(record,value) {
    return validMetadata(value) && value.lab_id===record.lab
      && (record.context.attempt==null || record.context.attempt===value.attempt)
      && record.context.difficulty===value.difficulty && (!record.metadata || sameMetadata(record.metadata,value));
  }

  function bind(record,trace_id,metadata) {
    if (!/^[a-f0-9]{32}$/.test(trace_id||'') || record.historical) return false;
    if (metadata && !metadataFits(record,metadata)) return false;
    record.trace_id=trace_id;
    if (metadata) record.metadata=Object.freeze({...metadata});
    record.stream_status='connecting';
    return true;
  }

  function apply(record,event,now=0) {
    if (record.historical || record.terminal || event?.trace_id!==record.trace_id
        || !Number.isSafeInteger(event.event_id) || event.event_id!==record.last_event_id+1
        || event.event_id>257 || !Number.isSafeInteger(event.span_id) || event.span_id<0
        || (!stageNames.has(event.stage)&&event.stage!=='trace') || !statuses.has(event.status)
        || !Number.isFinite(event.duration_ms) || event.duration_ms<0 || !Number.isFinite(event.timestamp)) return false;
    if (event.metadata && !metadataFits(record,event.metadata)) return false;
    const previous=record.spans.get(event.span_id);
    if (event.stage!=='trace' && previous && (previous.stage!==event.stage || finished.has(previous.status)
        || (previous.status==='running'&&event.status==='waiting'))) return false;
    if (event.metadata) record.metadata=Object.freeze({...event.metadata});
    if(record.last_event_id===0)record.startedAt=now-event.duration_ms;
    const item=Object.freeze({event_id:event.event_id,span_id:event.span_id,stage:event.stage,status:event.status,
      duration_ms:event.duration_ms,timestamp:event.timestamp,receivedAt:now,
      reason_code:Object.hasOwn(reasonText,event.reason_code)?event.reason_code:null,
      outcome:event.stage==='trace'&&Object.hasOwn(outcomes,event.outcome)?event.outcome:null});
    record.last_event_id=event.event_id;
    if (event.stage==='trace') {
      record.terminal=item;record.stream_status='finished';
    } else record.spans.set(event.span_id,item);
    return true;
  }

  function outcome(record) {
    if (!record||record.idle) return {key:'IDLE',label:'READY',tone:'gray',description:'Submit a prompt to inspect its real execution path.'};
    if (!record.terminal) {
      if (record.historical) return {key:'PAUSED',label:'PREVIOUS ATTEMPT',tone:'gray',description:'This trace belongs to an earlier attempt. Live updates are stopped.'};
      if (['error','timeout','unavailable','closed','superseded'].includes(record.stream_status))
        return {key:'UNAVAILABLE',label:'TRACE UNAVAILABLE',tone:'gray',description:'The request and its trace are independent. No execution outcome is inferred from a stream failure.'};
      if (record.stream_status==='reconnecting') return {key:'RECONNECTING',label:'RECONNECTING',tone:'gray',description:'Waiting to replay missed backend events.'};
      if (!record.trace_id) return {key:'PREPARING',label:'PREPARING TRACE',tone:'gray',description:'Waiting for the backend trace. No processing stage has been assumed.'};
      return {key:'RUNNING',label:'PROCESSING',tone:'blue',description:'Following confirmed backend events in this conversation turn.'};
    }
    if (record.terminal.outcome) return {key:record.terminal.outcome,...outcomes[record.terminal.outcome]};
    const spans=[...record.spans.values()];
    if (record.terminal.reason_code==='trace_unavailable') return {key:'UNAVAILABLE',label:'TRACE UNAVAILABLE',tone:'gray',description:'The backend could not provide a complete execution outcome.'};
    if (record.terminal.status==='error') return {key:'UNCONFIRMED',label:'ERROR RECORDED',tone:'gray',description:'A processing error was recorded; the final request outcome was not reported.'};
    const check=spans.filter(item=>item.stage==='exploit_verification'&&item.status==='passed')
      .sort((a,b)=>a.event_id-b.event_id).at(-1);
    const enforced=spans.some(item=>item.status==='blocked'&&item.stage!=='rag');
    if(enforced&&check?.reason_code==='verified')return {key:'UNCONFIRMED',label:'TRACE COMPLETE',tone:'gray',description:'Verification and rejection events were recorded; the final request outcome was not reported.'};
    let key=enforced?'BACKEND_BLOCKED':check?.reason_code==='verified'?'EXPLOIT_VERIFIED':null;
    if (!key&&check?.reason_code==='model_refusal') key='MODEL_REFUSED';
    if (!key&&check?.reason_code==='condition_not_met') key='NO_EXPLOIT';
    if (!key&&record.terminal.status==='passed') key='COMPLETED';
    return key?{key,...outcomes[key]}:{key:'UNCONFIRMED',label:'DEFENSE APPLIED',tone:'amber',description:'A defended stage was recorded; the overall request outcome was not reported.'};
  }

  function nodeProjection(record,definition,now) {
    const spans=record?[...record.spans.values()].filter(item=>definition.parts.includes(item.stage)):[];
    const live=record&&!record.terminal&&!record.historical&&liveStates.has(record.stream_status);
    const duration=spans.reduce((sum,item)=>sum+item.duration_ms+
      (!record?.terminal&&['waiting','running'].includes(item.status)
        ?Math.max(0,(live?now:record.pausedAt??item.receivedAt)-item.receivedAt):0),0);
    const blocked=spans.find(item=>item.status==='blocked');
    const active=spans.filter(item=>['running','waiting'].includes(item.status)).at(-1);
    const error=spans.find(item=>item.status==='error');
    const allSeen=definition.parts.every(part=>spans.some(item=>item.stage===part));
    const anyPassed=spans.some(item=>item.status==='passed');
    const allSkipped=allSeen&&spans.every(item=>item.status==='skipped');
    const refusal=record&&([...record.spans.values()].some(item=>item.stage==='exploit_verification'&&item.reason_code==='model_refusal')
      || record.terminal?.outcome==='MODEL_REFUSED');
    const decisive=blocked||error||active||spans.at(-1);
    let status='pending',tone='gray',label='Pending';
    if (blocked) { status='blocked';tone='red';label='Blocked'; }
    else if (active&&live) { status=active.status;tone=active.status==='running'?'blue':'gray';label=active.status==='running'?'Processing':'Waiting'; }
    else if (error) { status='error';label='Error'; }
    else if (active) { status='unconfirmed';label=record?.terminal?'Unfinished':'Last seen running'; }
    else if (definition.id==='ollama'&&refusal&&anyPassed) { status='passed';tone='amber';label='Model refused'; }
    else if (definition.id==='verification'&&anyPassed&&record?.terminal?.outcome!=='EXPLOIT_VERIFIED'
        &&['condition_not_met','model_refusal'].includes(spans.filter(item=>item.status==='passed').at(-1)?.reason_code)) {
      status='passed';tone='amber';label='Not verified';
    } else if (allSeen&&anyPassed) { status='passed';tone='green';label='Passed'; }
    else if (allSkipped) { status='skipped';label='Skipped'; }
    return Object.freeze({id:definition.id,label:definition.label,status,status_label:label,tone,duration_ms:duration,
      reason_code:decisive?.reason_code||null,description:definition.description,
      spans:Object.freeze(spans.map(item=>Object.freeze({component:components[item.stage],status:item.status,
        duration_ms:item.duration_ms,reason_code:item.reason_code,event_id:item.event_id}))),
      explanation:blocked?'A backend control enforced a rejection here.':error?'A processing error was recorded; no security rejection is inferred.'
        :label==='Model refused'?'The model returned a response containing an explicitly evidenced refusal.'
        :definition.id==='verification'&&tone==='amber'?'Verification ran successfully, but the current objective was not verified.'
        :status==='skipped'?'Backend events confirm this optional component did not execute.'
        :status==='pending'?'No execution result has been reported for this component.'
        :status==='unconfirmed'?'The last confirmed operation has no recorded finish event.'
        :record?.metadata?.difficulty===0&&['policy','output'].includes(definition.id)&&status==='passed'
          ?`The backend completed the permissive Easy ${definition.id==='policy'?'input':'output'} policy.`
        :definition.description});
  }

  function project(record,context={},now=0) {
    const nodes=definitions.map(definition=>nodeProjection(record,definition,now));
    const final=outcome(record);
    const metadata=record?.metadata||null;
    return Object.freeze({lab:record?.lab||context.lab,trace_id:record?.trace_id||null,metadata,
      phase:!record||record.idle?'idle':record.terminal?'completed':['PREPARING'].includes(final.key)?'preparing'
        :['UNAVAILABLE','PAUSED','RECONNECTING'].includes(final.key)?'paused':'running',
      historical:record?.historical||false,nodes:Object.freeze(nodes),outcome:Object.freeze(final),
      duration_ms:record?.terminal?.duration_ms??(record?.trace_id&&record.last_event_id>0
        ?Math.max(0,(liveStates.has(record.stream_status)&&!record.historical?now:record.pausedAt??record.startedAt)-record.startedAt):0),
      stream_status:record?.stream_status||'idle'});
  }

  globalThis.TraceTimelineModel=Object.freeze({definitions,create,bind,apply,project,reasonText});
  if (typeof document==='undefined') return;
  const root=document.getElementById('execution-timeline');
  if (!root) return;
  document.body.classList.add('has-execution-timeline');
  const $=id=>document.getElementById(id);
  const write=(id,value)=>{const element=$(id);if(element.textContent!==value)element.textContent=value;return element;};
  const records=new Map(),views=new Map(),buttons=new Map(),expanded=new Map();
  let currentLab=null,sequence=0,timer=null;
  const clock=()=>performance.now();
  const contextOf=(lab,value={})=>({lab,attempt:Number.isSafeInteger(value.attempt)?value.attempt:null,
    difficulty:[0,1,2].includes(value.difficulty)?value.difficulty:0});
  const text=(tag,className,value)=>{const item=document.createElement(tag);if(className)item.className=className;if(value!==undefined)item.textContent=value;return item;};
  const duration=value=>`${Math.round(value).toLocaleString()} ms`;
  const difficulty=value=>['Easy · Vulnerable','Medium · Hardened','Hard · Guardrailed'][value]||'Difficulty unavailable';
  for (const definition of definitions) {
    const li=text('li','trace-step'),button=text('button','trace-node');button.type='button';
    button.dataset.traceNode=definition.id;button.setAttribute('aria-controls','trace-inspector');button.setAttribute('aria-pressed','false');
    const icon=text('span','trace-node-icon','·');icon.setAttribute('aria-hidden','true');
    button.append(icon,text('span','trace-node-title',definition.label),text('span','trace-node-status','Pending'),text('span','trace-node-duration','—'));
    li.append(button);$('trace-nodes').append(li);buttons.set(definition.id,button);
  }

  function closeRecord(record) {
    if (record?.trace_id) globalThis.ChatTraceTransport?.close(record.trace_id);
    if (record&&!record.terminal) { record.pausedAt=clock();record.stream_status='closed'; }
  }

  function hydrate(record) {
    const cached=globalThis.ChatTraceTransport?.get(record.trace_id);
    if (!cached) return;
    for (const event of cached.events) apply(record,event,clock());
    record.stream_status=record.terminal?'finished':cached.stream_status;
  }

  function renderInspector(projection,record) {
    const inspector=$('trace-inspector'),selected=projection.nodes.find(item=>item.id===record?.selected);
    inspector.hidden=!selected;
    if (!selected) return;
    write('trace-inspector-title',selected.label);
    write('trace-inspector-status',selected.status_label);
    $('trace-inspector-status').dataset.tone=selected.tone;
    $('trace-inspector-duration').textContent=selected.spans.length?duration(selected.duration_ms):'Not reported';
    write('trace-inspector-description',selected.explanation);
    write('trace-inspector-reason',selected.reason_code||'Not reported');
    write('trace-inspector-explanation',reasonText[selected.reason_code]||'Only backend-confirmed diagnostic information is shown.');
    const rows=selected.spans.map(span=>{
      const row=text('li','trace-diagnostic');
      row.append(text('span','',span.component),text('span','',`${span.status} · ${duration(span.duration_ms)}`));
      return row;
    });
    const signature=JSON.stringify(selected.spans);
    if($('trace-inspector-events').dataset.signature!==signature){
      $('trace-inspector-events').replaceChildren(...rows);$('trace-inspector-events').dataset.signature=signature;
    }
  }

  function render() {
    const record=records.get(currentLab),context=views.get(currentLab)||{};
    const snapshot=project(record,context,clock());
    root.dataset.state=snapshot.phase;root.dataset.traceId=snapshot.trace_id||'';
    root.dataset.historical=String(snapshot.historical);
    root.dataset.live=String(snapshot.nodes.some(item=>item.tone==='blue'));
    $('trace-content').hidden=!expanded.get(currentLab);
    $('trace-toggle').setAttribute('aria-expanded',String(Boolean(expanded.get(currentLab))));
    $('trace-toggle').textContent=expanded.get(currentLab)?'Collapse':'Show pipeline';
    write('trace-outcome-label',snapshot.outcome.label);$('trace-outcome-label').dataset.tone=snapshot.outcome.tone;
    write('trace-summary',snapshot.outcome.description);
    $('trace-difficulty').textContent=snapshot.metadata
      ?`${difficulty(snapshot.metadata.difficulty)} · Attempt ${snapshot.metadata.attempt}${snapshot.historical?' · Previous attempt':''}`
      :`${record&&!record.idle?'Requested: ':'Selected: '}${difficulty(context.difficulty)}${record&&!record.idle?' · Awaiting backend metadata':''}`;
    $('trace-total-duration').textContent=snapshot.trace_id&&record.last_event_id>0?duration(snapshot.duration_ms):'—';
    let stopped=false;
    for (let index=0;index<snapshot.nodes.length;index++) {
      const item=snapshot.nodes[index],button=buttons.get(item.id),li=button.parentElement;
      button.dataset.tone=item.tone;button.dataset.status=item.status;
      button.setAttribute('aria-pressed',String(record?.selected===item.id));
      button.querySelector('.trace-node-icon').textContent=({green:'✓',blue:'·',red:'!',amber:'◇'})[item.tone]||(item.status==='skipped'?'–':'·');
      button.querySelector('.trace-node-status').textContent=item.status_label;
      button.querySelector('.trace-node-duration').textContent=item.spans.length?duration(item.duration_ms):'—';
      button.setAttribute('aria-label',`${item.label}: ${item.status_label}${item.spans.length&&!['running','waiting'].includes(item.status)?', '+duration(item.duration_ms):''}`);
      if(item.status==='blocked')stopped=true;
      const next=snapshot.nodes[index+1];
      li.dataset.connection=!stopped&&next&&item.status==='passed'&&next.status==='blocked'?'blocked'
        :!stopped&&next&&item.status==='passed'&&next.status==='passed'?'complete':'pending';
    }
    renderInspector(snapshot,record);
    const ticking=record&&!record.terminal&&!record.historical&&liveStates.has(record.stream_status)
      &&[...record.spans.values()].some(item=>['waiting','running'].includes(item.status));
    if(ticking&&!timer)timer=setInterval(render,200);
    if(!ticking&&timer){clearInterval(timer);timer=null;}
  }

  function showLab(lab,value) {
    const context=contextOf(lab,value),previous=records.get(currentLab);
    if(currentLab!==lab){closeRecord(previous);globalThis.ChatTraceTransport?.cancelPending();}
    currentLab=lab;views.set(lab,context);
    const record=records.get(lab);
    if(record){
      const changed=record.context.difficulty!==context.difficulty
        ||(record.context.attempt!=null&&context.attempt!=null&&record.context.attempt!==context.attempt);
      if(changed){closeRecord(record);record.historical=true;}
      else if(!record.historical&&!record.terminal&&record.trace_id){
        const transport=globalThis.ChatTraceTransport;
        const resumed=transport?.resume(record.trace_id);
        if(!resumed&&record.metadata)transport?.restore?.(record.trace_id,{lab:record.lab,metadata:record.metadata,
          after:record.last_event_id,started:record.wallStarted||Date.now()});
        hydrate(record);
      }
    }
    render();
  }

  function begin(lab,value) {
    closeRecord(records.get(lab));globalThis.ChatTraceTransport?.cancelPending();
    const context=contextOf(lab,value),record=create(lab,context,++sequence,clock());
    record.wallStarted=Date.now();
    records.set(lab,record);views.set(lab,context);expanded.set(lab,true);currentLab=lab;render();
    root.querySelector('.trace-track').scrollLeft=0;
    return Object.freeze({lab,generation:record.generation});
  }

  function attach(ticket,handle) {
    const record=records.get(ticket?.lab);
    if(!record||record.generation!==ticket.generation||record.historical||currentLab!==ticket.lab){handle?.close();return false;}
    if(!handle||!globalThis.ChatTraceTransport?.get(handle.trace_id)
        ||!bind(record,handle.trace_id,handle.metadata)){record.stream_status='unavailable';handle?.close();render();return false;}
    hydrate(record);render();return true;
  }

  function reset(lab,value) {
    const record=records.get(lab);
    if(record){closeRecord(record);record.historical=true;record.generation=++sequence;}
    views.set(lab,contextOf(lab,value));render();
  }

  function finish(ticket) {
    const record=records.get(ticket?.lab);
    if(!record||record.generation!==ticket.generation)return;
    if(record.trace_id)hydrate(record);else record.stream_status='unavailable';
    if(currentLab===ticket.lab)render();
  }

  root.addEventListener('click',event=>{
    const button=event.target.closest('[data-trace-node]');
    if(!button)return;
    let record=records.get(currentLab);
    if(!record){record=create(currentLab,views.get(currentLab)||{},++sequence,clock());record.stream_status='unavailable';record.idle=true;records.set(currentLab,record);}
    record.selected=record.selected===button.dataset.traceNode?null:button.dataset.traceNode;render();
  });
  $('trace-inspector-close').addEventListener('click',()=>{
    const record=records.get(currentLab),selected=record?.selected;if(record)record.selected=null;render();
    buttons.get(selected)?.focus();
  });
  $('trace-toggle').addEventListener('click',()=>{expanded.set(currentLab,!expanded.get(currentLab));render();});
  $('trace-nodes').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const values=[...buttons.values()],index=values.indexOf(document.activeElement);if(index<0)return;
    event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?values.length-1
      :(index+(event.key==='ArrowRight'?1:-1)+values.length)%values.length;values[next].focus();
  });
  window.addEventListener('panoptis:trace-event',event=>{
    const record=records.get(currentLab);
    if(!record||record.historical||event.detail.trace_id!==record.trace_id)return;
    // Window notifications are wake-ups, not execution evidence. Only consume
    // the transport's validated SSE cache, never a forged DOM event payload.
    hydrate(record);render();
  });
  window.addEventListener('panoptis:trace-stream-state',event=>{
    const record=records.get(currentLab);
    if(!record||record.historical||record.terminal||event.detail.trace_id!==record.trace_id)return;
    const canonical=globalThis.ChatTraceTransport?.get(record.trace_id);
    if(!canonical)return;
    record.stream_status=canonical.stream_status;
    if(!liveStates.has(record.stream_status))record.pausedAt=clock();
    render();
  });
  window.addEventListener('panoptis:trace-stream-error',event=>{
    const record=records.get(currentLab),canonical=record&&globalThis.ChatTraceTransport?.get(record.trace_id);
    if(record?.trace_id===event.detail.trace_id&&canonical?.stream_status==='error'){record.stream_status='error';render();}
  });
  window.addEventListener('pagehide',()=>{if(timer){clearInterval(timer);timer=null;}closeRecord(records.get(currentLab));});
  globalThis.TraceTimeline=Object.freeze({showLab,begin,bind:attach,finish,reset,
    snapshot:()=>project(records.get(currentLab),views.get(currentLab)||{},clock())});
})();
