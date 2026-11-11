const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/\nstart\(\);\s*$/, '');
const bulletin = { id: 'sb-b01-047', title: 'SB-B01-047 — Contractor Service Bulletin', content: 'Saved maintenance bulletin.', is_user_injected: true };
const original = { id: 'inspection-standard', title: 'Inspection standard', content: 'Original maintenance reference.', is_user_injected: false };
const copy = value => JSON.parse(JSON.stringify(value));

function record({ saved = true, synced = true, current = true, enabled = true, last = null } = {}) {
  return { id: 3, documents: saved ? [copy(original), copy(bulletin)] : [copy(original)],
    document_sync: { [original.id]: { saved: true, synced: true, matches_saved: true },
      ...(saved || synced ? { [bulletin.id]: { saved, synced, matches_saved: saved && synced && current } } : {}) },
    kb_revision: !saved && !synced ? 0 : !synced ? 1 : current ? 1 : 2, synced_revision: synced ? 1 : 0, use_kb: enabled,
    messages: [], last_result: last, completed: false, exploited: false, flag: null };
}

function studentUI(lab = record()) {
  const elements = new Map(), all = [];
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.style = {}; this.value = ''; this.text = ''; this.listeners = {}; all.push(this); }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    append(...children) { children.forEach(child => { child.parentElement = this; this.children.push(child); }); }
    prepend(...children) { children.reverse().forEach(child => { child.parentElement = this; this.children.unshift(child); }); }
    replaceChildren(...children) { this.children.forEach(child => { child.parentElement = null; }); this.children = []; this.text = ''; this.append(...children); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; if (this.id) elements.delete(this.id); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    querySelectorAll() { return []; }
    focus() {}
    showModal() {}
  }
  const get = id => {
    if (id === 'lab3-editor-status') return elements.get(id) || null;
    if (!elements.has(id)) { const element = new Element('div'); element.id = id; }
    return elements.get(id);
  };
  const context = vm.createContext({ document: { getElementById: get, createElement: tag => new Element(tag),
    createTextNode: text => ({ textContent: text }), querySelector: () => null,
    querySelectorAll: selector => all.filter(element => selector === '.lab3-document-status' && element.className?.includes('lab3-document-status')) },
    window: { confirm: () => true }, setTimeout: () => 0, clearTimeout() {} });
  vm.runInContext(source, context);
  context.initial = copy(lab);
  const run = text => vm.runInContext(text, context);
  run(`state.current = 3; state.catalog = Array.from({length:9},(_,i)=>({id:i+1,has_kb:[3,7,8].includes(i+1),points:150}));
    state.labs['3'] = initial; updateControls = drawNavigation = () => {};
    drawLab = () => { drawWorkspace(); drawKnowledge(); }; drawLab();`);
  const status = (id = bulletin.id) => JSON.parse(run(`JSON.stringify(lab3DocumentStatus(${JSON.stringify(id)}))`));
  const respond = next => { context.fetch = async () => ({ok: true, json: async () => ({lab: copy(next), summary: {score:0,completed:[]}})}); };
  return { context, run, get, status, respond, all };
}

test('fresh originals show retrieval disabled while the unsaved incoming bulletin is not saved', () => {
  const ui = studentUI(record({saved:false,synced:false,enabled:false}));
  assert.equal(ui.status(original.id).state, 'disabled');
  assert.equal(ui.status().label, 'Not saved');
  assert(!ui.get('workspace-content').textContent.includes('Not indexed'));
  assert.match(ui.get('kb-status').textContent, /Retrieval disabled/);
});

test('editing without saving shows local changes and makes no backend request', () => {
  const ui = studentUI();
  const before = ui.run("JSON.stringify(state.labs['3'])");
  ui.context.fetch = () => { throw new Error('Editing must not save'); };
  ui.context.draft = {...bulletin,content:'Local draft change.'};
  ui.run('state.editors.set(3,draft); refreshLab3DocumentStatuses()');
  assert.equal(ui.status().label, 'Unsaved changes');
  assert.equal(ui.run("JSON.stringify(state.labs['3'])"), before);
  assert.match(ui.get('lab3-editor-status').textContent, /earlier synced version is unchanged/);
});

test('opening an unchanged editor does not claim unsaved changes', () => {
  const ui = studentUI();
  ui.context.draft = copy(bulletin);
  ui.run('state.editors.set(3,draft); refreshLab3DocumentStatuses()');
  assert.equal(ui.status().state, 'synced');
});

test('saving a new document reports saved but awaiting synchronization', async () => {
  const ui = studentUI(record({saved:false,synced:false,enabled:false}));
  const next = record({synced:false,current:false,enabled:false});
  let sent;
  ui.context.docs = next.documents;
  ui.context.fetch = async (url, options) => {
    sent = {url,body:JSON.parse(options.body)};
    return {ok:true,json:async()=>({lab:next,summary:{score:0}})};
  };
  await ui.run('updateKnowledge({documents:docs,clearEditor:true})');
  assert.equal(sent.url, '/api/labs/3/kb');
  assert.equal(sent.body.sync, false);
  assert.equal(sent.body.use_kb, false);
  assert.equal(ui.status().state, 'saved_unsynced');
  assert.match(ui.get('kb-notice').textContent, /Document saved.*Retrieval is disabled.*Sync and enable/);
});

test('sync and enable confirms an active snapshot and immediately updates document badges', async () => {
  const ui = studentUI(record({synced:false,current:false,enabled:false}));
  ui.respond(record());
  await ui.run('updateKnowledge({sync:true,useKB:true})');
  assert.equal(ui.status().state, 'synced');
  assert.match(ui.get('kb-notice').textContent, /Synced snapshot active/);
  assert(ui.all.some(element => element.className?.includes('lab3-document-status') && element.textContent === 'Synced · Retrieval enabled'));
});

test('saving a later edit explains that the older synced version remains available', async () => {
  const ui = studentUI();
  ui.respond(record({current:false}));
  await ui.run('updateKnowledge({clearEditor:true})');
  assert.equal(ui.status().state, 'saved_newer');
  assert.match(ui.status().detail, /older synced version is still available/);
  assert.match(ui.get('kb-notice').textContent, /Document saved.*earlier synced snapshot/);
});

test('an unchanged document stays synced when another document has unsynced changes', () => {
  const ui = studentUI(record({current:false}));
  assert.equal(ui.status(original.id).state, 'synced');
  assert.equal(ui.status().state, 'saved_newer');
});

test('disabling retrieval changes the status while preserving the synced version', async () => {
  const ui = studentUI();
  ui.respond(record({enabled:false}));
  await ui.run('updateKnowledge({useKB:false})');
  assert.equal(ui.status().state, 'disabled');
  assert.match(ui.status().detail, /version is synced/);
  assert.match(ui.get('kb-notice').textContent, /Retrieval disabled/);
});

test('refresh reconstructs saved and synced state without an old success notice', () => {
  const lab = record({current:false});
  const ui = studentUI(lab);
  assert.equal(ui.status().state, 'saved_newer');
  assert.match(ui.get('kb-notice').textContent, /Saved changes need synchronization.*earlier synced snapshot/);
});

test('student UI instances do not share drafts, statuses or saved documents', () => {
  const first = studentUI();
  first.context.draft = {...bulletin,content:'First student draft.'};
  first.run('state.editors.set(3,draft); refreshLab3DocumentStatuses()');
  const second = studentUI(record({saved:false,synced:false,enabled:false}));
  assert.equal(first.status().state, 'unsaved');
  assert.equal(second.status().state, 'not_saved');
  assert.equal(second.run('state.editors.size'), 0);
});

test('pending saves do not optimistically claim that a document is synced', async () => {
  const ui = studentUI(record({synced:false,current:false}));
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ok:true,json:async()=>({lab:record(),summary:{score:0}})}); });
  const pending = ui.run('updateKnowledge({sync:true,useKB:true})');
  assert.equal(ui.status().state, 'saved_unsynced');
  release();
  await pending;
  assert.equal(ui.status().state, 'synced');
});

test('failed saves keep local edits and the previous synchronized status', async () => {
  const ui = studentUI();
  ui.context.draft = {...bulletin,content:'Unsaved draft.'};
  ui.run('state.editors.set(3,draft); refreshLab3DocumentStatuses()');
  ui.context.fetch = async () => ({ok:false,json:async()=>({error:'Fixture save failure'})});
  await ui.run('updateKnowledge({clearEditor:true})');
  assert.equal(ui.status().state, 'unsaved');
  assert.equal(ui.run("state.labs['3'].document_sync['sb-b01-047'].matches_saved"), true);
});

for (const [current, label] of [[true,'Current synced snapshot'],[false,'Earlier synced snapshot'],[null,'Snapshot version unavailable']]) {
  test(`retrieval feedback uses backend source counts and ${label.toLowerCase()}`, () => {
    const last = {triggered:false,message:'No exploit verified.',seconds:0.1,kb_used:true,
      sources:[{id:bulletin.id,title:bulletin.title,injected:true},{id:original.id,title:original.title,injected:false}],retrieval_snapshot_current:current};
    const ui = studentUI(record({last}));
    ui.run('drawConversation()');
    const summary = ui.get('attempt-result').children.find(child => child.className === 'lab3-retrieval-summary').textContent;
    assert.match(summary, /2 documents \(1 of yours\)/);
    assert(summary.includes(label));
    assert(!/followed|trusted|receipt|sha256|evaluator/i.test(summary));
  });
}

test('zero retrieved documents do not claim that a snapshot was used', () => {
  const ui = studentUI(record({last:{triggered:false,message:'No exploit verified.',seconds:0.1,kb_used:false,sources:[],retrieval_snapshot_current:null}}));
  ui.run('drawConversation()');
  assert.match(ui.get('attempt-result').textContent, /0 documents \(none of yours\)/);
  assert(!ui.get('attempt-result').textContent.includes('Current synced snapshot'));
});

test('a saved removal still warns that an older synced version can be retrieved', () => {
  const ui = studentUI(record({saved:false,synced:true,current:false}));
  assert.equal(ui.status().state, 'saved_newer');
  assert.match(ui.status().detail, /older synced version is still available/);
});

test('missing per-document metadata produces an unknown status instead of inventing a sync result', () => {
  const lab = record();
  delete lab.document_sync;
  const ui = studentUI(lab);
  assert.equal(ui.status().state, 'unknown');
});
