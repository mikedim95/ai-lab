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

function studentUI(lab = record(), labId = 3) {
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
    showModal() { this.open = true; }
    close() { this.open = false; }
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
  context.labId = labId;
  const run = text => vm.runInContext(text, context);
  run(`state.current = labId; state.catalog = Array.from({length:9},(_,i)=>({id:i+1,has_kb:[3,7,8].includes(i+1),points:150}));
    state.labs[String(labId)] = initial; updateControls = drawNavigation = () => {};
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
  assert.equal(ui.get('document-list').children.length, 1);
});

test('editing without saving shows local changes and makes no backend request', () => {
  const ui = studentUI();
  const before = ui.run("JSON.stringify(state.labs['3'])");
  ui.context.fetch = () => { throw new Error('Editing must not save'); };
  ui.context.draft = {...bulletin,content:'Local draft change.'};
  ui.run('state.editors.set(3,draft); drawKnowledge()');
  assert.equal(ui.status().label, 'Unsaved changes');
  assert.equal(ui.run("JSON.stringify(state.labs['3'])"), before);
  assert.equal(ui.get('document-content').value, 'Local draft change.');
});

test('opening an unchanged editor does not claim unsaved changes', () => {
  const ui = studentUI();
  ui.context.draft = copy(bulletin);
  ui.run('state.editors.set(3,draft); drawKnowledge()');
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
  assert.equal(ui.run('state.editors.size'), 0);
});

test('sync and enable confirms an active snapshot and immediately updates document badges', async () => {
  const ui = studentUI(record({synced:false,current:false,enabled:false}));
  ui.respond(record());
  await ui.run('updateKnowledge({sync:true,useKB:true})');
  assert.equal(ui.status().state, 'synced');
  assert.equal(ui.run("state.labs['3'].kb_revision === state.labs['3'].synced_revision"), true);
});

test('saving a later edit explains that the older synced version remains available', async () => {
  const ui = studentUI();
  ui.respond(record({current:false}));
  await ui.run('updateKnowledge({clearEditor:true})');
  assert.equal(ui.status().state, 'saved_newer');
  assert.match(ui.status().detail, /older synced version is still available/);
  assert.equal(ui.run("state.labs['3'].synced_revision"), 1);
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
  assert.equal(ui.run("state.labs['3'].use_kb"), false);
});

test('refresh reconstructs saved and synced state without an old success notice', () => {
  const lab = record({current:false});
  const ui = studentUI(lab);
  assert.equal(ui.status().state, 'saved_newer');
  assert.deepEqual(JSON.parse(ui.run("JSON.stringify(state.labs['3'].documents)")), lab.documents);
});

test('student UI instances do not share drafts, statuses or saved documents', () => {
  const first = studentUI();
  first.context.draft = {...bulletin,content:'First student draft.'};
  first.run('state.editors.set(3,draft); drawKnowledge()');
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
  ui.run('state.editors.set(3,draft); drawKnowledge()');
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

function bulletinRecord(id) {
  const documents = [copy(original), {id:'format-reference',title:'Post format',content:'Use short paragraphs.',is_user_injected:false}];
  return {...record({saved:false,synced:false,enabled:false}), id, attempt:1, state_version:[1,0],
    documents, reference_documents:copy(documents)};
}

for (const id of [3,7,8]) {
  test(`Lab ${id}: two previous posts open read-only and New Document always starts blank`, () => {
    const initial = bulletinRecord(id), ui = studentUI(initial,id);
    ui.run('openKnowledge()');
    assert.equal(ui.get('document-list').children.length,2);
    assert.equal(ui.get('document-editor').hidden,true);
    ui.context.fetch = () => { throw Error('Viewing must not write'); };
    ui.run('newBulletin()');
    assert.equal(ui.get('document-title').value,'');
    assert.equal(ui.get('document-content').value,'');
    ui.run(`state.editors.get(${id}).title='Discarded draft'; backToBulletins(); newBulletin()`);
    assert.equal(ui.get('document-title').value,'');
    assert.equal(ui.get('document-content').value,'');
    assert.deepEqual(JSON.parse(ui.run(`JSON.stringify(state.labs['${id}'].documents)`)),initial.documents);
  });

  test(`Lab ${id}: final Save adds the third post and synchronizes/enables in one confirmed request`, async () => {
    const initial = bulletinRecord(id), ui = studentUI(initial,id);
    ui.run(`openKnowledge();newBulletin();Object.assign(state.editors.get(${id}),{title:'Student bulletin',content:'A public test note.'})`);
    let calls=0;
    ui.context.fetch = async (url,options) => {
      calls+=1;
      const body=JSON.parse(options.body);
      assert.equal(url,`/api/labs/${id}/kb`);
      assert.equal(body.sync,true);assert.equal(body.use_kb,true);
      assert.deepEqual(body.documents.slice(0,2),initial.documents);
      assert.equal(body.documents.length,3);
      const documents=body.documents.map((doc,i)=>i<2?doc:{...doc,id:'student-bulletin',is_user_injected:true});
      return {ok:true,json:async()=>({lab:{...initial,documents,use_kb:true,kb_revision:1,synced_revision:1,state_version:[1,1]},summary:{score:0,completed:[]}})};
    };
    await ui.run('saveBulletin({preventDefault(){}})');
    assert.equal(calls,1);
    assert.equal(ui.get('knowledge-dialog').open,false);
    assert.equal(ui.run(`state.editors.has(${id})`),false);
    assert.equal(ui.run(`state.labs['${id}'].documents.length`),3);
    assert.equal(ui.run('state.summary.score'),0);
  });

  test(`Lab ${id}: pending and duplicate saves keep the modal and draft until acknowledgement`, async () => {
    const initial=bulletinRecord(id),ui=studentUI(initial,id);
    ui.run('openKnowledge();newBulletin()');
    let calls=0,release;
    ui.context.fetch=()=>{calls+=1;return new Promise(resolve=>{release=resolve;});};
    const pending=ui.run('saveBulletin({preventDefault(){}})');
    await ui.run('saveBulletin({preventDefault(){}})');
    assert.equal(calls,1);assert.equal(ui.get('knowledge-dialog').open,true);
    assert.equal(ui.run(`state.editors.has(${id})`),true);
    assert.equal(ui.run(`state.labs['${id}'].documents.length`),2);
    release({ok:false,json:async()=>({error:'Save failed in fixture'})});
    await pending;
    assert.equal(ui.get('knowledge-dialog').open,true);
    assert.equal(ui.get('document-error').hidden,false);
    assert.equal(ui.get('document-error').textContent,'Save failed in fixture');
    assert.equal(ui.run(`state.editors.has(${id})`),true);
    assert.deepEqual(JSON.parse(ui.run(`JSON.stringify(state.labs['${id}'])`)),initial);
  });

  test(`Lab ${id}: a late old-attempt Save cannot overwrite a restarted lab or its new draft`, async () => {
    const initial=bulletinRecord(id),ui=studentUI(initial,id);
    ui.run('openKnowledge();newBulletin()');
    let release;
    ui.context.fetch=()=>new Promise(resolve=>{release=resolve;});
    const pending=ui.run('saveBulletin({preventDefault(){}})');
    ui.context.restarted={...copy(initial),attempt:2,state_version:[2,0]};
    ui.run(`state.labs['${id}']=restarted;state.editors.delete(${id});newBulletin();state.editors.get(${id}).title='New attempt draft'`);
    release({ok:true,json:async()=>({lab:{...initial,kb_revision:1,synced_revision:1,use_kb:true,state_version:[1,1]},summary:{score:0}})});
    await pending;
    assert.equal(ui.run(`state.labs['${id}'].attempt`),2);
    assert.equal(ui.run(`state.editors.get(${id}).title`),'New attempt draft');
    assert.equal(ui.get('knowledge-dialog').open,true);
  });
}
