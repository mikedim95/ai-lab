'use strict';

// Transport and bounded state only. Consumers can observe these events later;
// this module never renders UI or changes chat, verification, flags, or scores.
(() => {
  let enabled = false;
  let active = null;
  let setupGeneration = 0;
  let pendingSetup = null;
  const traces = new Map();
  const stages = new Set(['input','session','policy','context','rag','ollama',
    'tool_authorization','tool_execution','output_protection','exploit_verification','trace']);
  const statuses = new Set(['waiting','running','passed','blocked','skipped','error']);
  const terminalStatuses = new Set(['passed','blocked','error']);
  const outcomes = new Set(['COMPLETED','MODEL_REFUSED','BACKEND_BLOCKED','NO_EXPLOIT','EXPLOIT_VERIFIED','ERROR']);

  function metadata(value) {
    if (value === undefined || value === null) return null;
    if (typeof value !== 'object' || Array.isArray(value)
        || !Number.isSafeInteger(value.lab_id) || value.lab_id < 1 || value.lab_id > 9
        || !Number.isSafeInteger(value.difficulty) || value.difficulty < 0 || value.difficulty > 2
        || !Number.isSafeInteger(value.attempt) || value.attempt < 1) return undefined;
    return Object.freeze({lab_id:value.lab_id,difficulty:value.difficulty,attempt:value.attempt});
  }

  function sameMetadata(first, second) {
    return first.lab_id === second.lab_id && first.difficulty === second.difficulty && first.attempt === second.attempt;
  }

  function notify(name, value) {
    try { window.dispatchEvent(new CustomEvent(name, {detail:Object.freeze({...value})})); }
    catch { /* Observers cannot affect chat execution. */ }
  }

  function stateChanged(record, status) {
    if (record.stream_status === status) return;
    record.stream_status = status;
    notify('panoptis:trace-stream-state', {trace_id:record.trace_id,lab:record.lab,
      metadata:record.metadata,stream_status:status});
  }

  function cancelPending() {
    setupGeneration++;
    if (pendingSetup) {
      const cancelled = pendingSetup;
      pendingSetup = null;
      clearTimeout(cancelled.timeout);
      try { cancelled.controller.abort(); } catch { /* Reservation cleanup is independent of chat. */ }
    }
  }

  function connect(record) {
    if (record.closed || record.terminal) return;
    const generation = ++record.generation;
    let source;
    try { source = new EventSource(`/api/traces/${record.trace_id}/events?after=${record.last_event_id}`); }
    catch { stop(record, 'error'); return; }
    record.source = source;
    stateChanged(record, 'connecting');

    function retry() {
      if (generation !== record.generation || record.closed) return;
      record.generation++;
      source.close();
      const expired = record.replay_until == null ? Date.now()-record.started > 900000 : Date.now() >= record.replay_until;
      if (++record.retries > 20 || expired) { stop(record, 'error'); return; }
      stateChanged(record, 'reconnecting');
      record.retryTimer = setTimeout(() => connect(record), 500);
    }

    source.addEventListener('open', () => {
      if (generation === record.generation && !record.closed) stateChanged(record, 'open');
    });
    for (const type of ['stage','terminal']) source.addEventListener(type, event => {
      if (generation !== record.generation || record.closed) return;
      let value;
      try { value = JSON.parse(event.data); } catch { retry(); return; }
      if (value.trace_id !== record.trace_id || !Number.isSafeInteger(value.event_id) || value.event_id < 1
          || value.event_id > 257 || !Number.isSafeInteger(value.span_id) || value.span_id < 0
          || !stages.has(value.stage) || !statuses.has(value.status)
          || !Number.isFinite(value.timestamp) || !Number.isFinite(value.duration_ms) || value.duration_ms < 0
          || typeof value.reason_code !== 'string' || !/^[a-z][a-z0-9_]{0,63}$/.test(value.reason_code)
          || (type === 'terminal') !== (value.stage === 'trace')) { retry(); return; }
      const incomingMetadata = metadata(value.metadata);
      if (incomingMetadata === undefined) { retry(); return; }
      if (incomingMetadata && (incomingMetadata.lab_id !== record.lab
          || (record.metadata && !sameMetadata(record.metadata, incomingMetadata)))) {
        stop(record, 'superseded'); return;
      }
      if (type === 'terminal' && value.outcome !== undefined && value.outcome !== null && !outcomes.has(value.outcome)) { retry(); return; }
      if (type === 'terminal' && value.persisted === false && value.scope === 'stream') {
        notify('panoptis:trace-stream-error', {trace_id:record.trace_id,metadata:record.metadata,reason_code:value.reason_code});
        // A temporary stream read failure says nothing about the still-running
        // chat. Replay from the last durable cursor using the existing bounds.
        if (value.reason_code === 'stream_unavailable') retry();
        else stop(record, 'error');
        return;
      }
      if (value.persisted !== true || String(value.event_id) !== event.lastEventId) { retry(); return; }
      if (value.event_id <= record.last_event_id) return;
      if (value.event_id !== record.last_event_id + 1) { retry(); return; }
      if (type === 'terminal' && !terminalStatuses.has(value.status)) { retry(); return; }
      const previous = record.spans.get(value.span_id);
      if (type === 'stage' && previous && (previous.stage !== value.stage
          || terminalStatuses.has(previous.status) || previous.status === 'skipped'
          || (previous.status === 'running' && value.status === 'waiting'))) { retry(); return; }
      if (!record.metadata && incomingMetadata) record.metadata = incomingMetadata;
      // Project only the protocol fields; ignore arbitrary server properties.
      const item = Object.freeze({trace_id:value.trace_id,event_id:value.event_id,span_id:value.span_id,
        stage:value.stage,status:value.status,timestamp:value.timestamp,duration_ms:value.duration_ms,
        reason_code:value.reason_code,persisted:true,metadata:record.metadata,
        ...(type === 'terminal' && value.outcome !== undefined ? {outcome:value.outcome} : {})});
      record.last_event_id = item.event_id;
      record.events.push(item);
      if (type === 'terminal') record.terminal = item;
      else record.spans.set(item.span_id, item);
      notify('panoptis:trace-event', item);
      if (type === 'terminal') stop(record, 'finished');
    });
    source.addEventListener('end', () => {
      if (generation === record.generation) stop(record, record.terminal ? 'finished' : 'closed');
    });
    source.addEventListener('error', retry);
  }

  function stop(record, status = 'closed') {
    record.closed = true;
    record.generation++;
    record.source?.close();
    clearTimeout(record.retryTimer); clearTimeout(record.graceTimer); clearTimeout(record.lifeTimer);
    stateChanged(record, record.terminal ? 'finished' : status);
    if (active === record) active = null;
  }

  function remember(record) {
    traces.delete(record.trace_id);
    traces.set(record.trace_id, record);
    while (traces.size > 8) {
      const first = traces.keys().next().value;
      stop(traces.get(first)); traces.delete(first);
    }
  }

  async function start(lab) {
    if (!enabled || typeof EventSource !== 'function' || typeof AbortController !== 'function') return null;
    cancelPending();
    const ticket = setupGeneration;
    if (active) stop(active, 'superseded');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 750);
    const setup = {controller,timeout};
    pendingSetup = setup;
    try {
      const response = await fetch(`/api/labs/${lab}/traces`, {method:'POST',
        headers:{'Content-Type':'application/json'},body:'{}',signal:controller.signal});
      if (ticket !== setupGeneration || pendingSetup !== setup) return null;
      if (!response.ok) return null;
      const value = await response.json();
      if (ticket !== setupGeneration || pendingSetup !== setup) return null;
      if (typeof value.trace_id !== 'string' || !/^[a-f0-9]{32}$/.test(value.trace_id)) return null;
      const initialMetadata = metadata(value.metadata);
      if (initialMetadata === undefined || (initialMetadata && initialMetadata.lab_id !== lab)) return null;
      const record = {trace_id:value.trace_id,lab,events:[],spans:new Map(),last_event_id:0,
        metadata:initialMetadata,generation:0,retries:0,started:Date.now(),closed:false,terminal:null};
      remember(record);
      active = record;
      record.lifeTimer = setTimeout(() => stop(record, 'timeout'), 900000);
      notify('panoptis:trace-start', {trace_id:record.trace_id,lab,metadata:record.metadata});
      connect(record);
      return Object.freeze({trace_id:record.trace_id,
        metadata:record.metadata,
        close:() => stop(record),
        complete:() => {
          if (!record.closed) {
            clearTimeout(record.graceTimer);
            record.graceTimer = setTimeout(() => stop(record), 2500);
          }
        },
        validate:id => { if (id && id !== record.trace_id) stop(record, 'superseded'); }});
    } catch { return null; }
    finally { clearTimeout(timeout); if (pendingSetup === setup) pendingSetup = null; }
  }

  function resume(id) {
    const record = traces.get(id);
    if (!record || record.terminal || record.retries > 20) return false;
    const remaining = 900000-(Date.now()-record.started);
    if (remaining <= 0) { stop(record, 'timeout'); return false; }
    cancelPending();
    if (active && active !== record) stop(active, 'superseded');
    if (!record.closed && active === record) return true;
    record.replay_until = null;
    record.closed = false;
    active = record;
    clearTimeout(record.graceTimer);
    clearTimeout(record.lifeTimer);
    record.lifeTimer = setTimeout(() => stop(record, 'timeout'), remaining);
    connect(record);
    return !record.closed;
  }

  function restore(id, witness) {
    if (!enabled || typeof EventSource !== 'function' || typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)
        || !witness || typeof witness !== 'object' || Array.isArray(witness)) return false;
    const bound = metadata(witness.metadata), now = Date.now();
    if (!bound || !Number.isSafeInteger(witness.lab) || witness.lab !== bound.lab_id
        || !Number.isSafeInteger(witness.after) || witness.after < 0 || witness.after > 257
        || !Number.isFinite(witness.started) || witness.started < 0 || witness.started > now) return false;
    const cached = traces.get(id);
    if (cached && (cached.terminal || cached.lab !== witness.lab
        || (cached.metadata && !sameMetadata(cached.metadata, bound)))) return false;
    cancelPending();
    const prior = active;
    if (prior) stop(prior, 'superseded');
    if (cached && cached !== prior) stop(cached);
    // The UI retains its already accepted stages. This small replacement only
    // reconnects their known cursor to the authenticated read-only SSE route.
    // It never reserves a trace or restarts the original chat request.
    const record = {trace_id:id,lab:witness.lab,metadata:bound,events:[],spans:new Map(),
      last_event_id:witness.after,generation:0,retries:0,started:cached?Math.min(cached.started,witness.started):witness.started,
      replay_until:now+5000,closed:false,terminal:null};
    remember(record);
    active = record;
    record.lifeTimer = setTimeout(() => stop(record, 'timeout'), 5000);
    connect(record);
    return !record.closed;
  }

  window.ChatTraceTransport = Object.freeze({
    enable:value => { enabled = value === true; if (!enabled) cancelPending(); }, start, resume, restore, cancelPending,
    get:id => {
      const record = traces.get(id);
      return record ? Object.freeze({trace_id:id,lab:record.lab,metadata:record.metadata,last_event_id:record.last_event_id,
        events:Object.freeze([...record.events]),terminal:record.terminal,stream_status:record.stream_status}) : null;
    },
    close:id => { cancelPending(); const record = traces.get(id); if (record) stop(record); },
    closeAll:() => { cancelPending(); for (const record of traces.values()) stop(record); },
  });
  window.addEventListener('pagehide', () => window.ChatTraceTransport.closeAll());
})();
