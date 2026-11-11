const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/\nstart\(\);\s*$/, '');
const note = { id: 'handover-note', title: 'Maintenance notebook', content: 'Fictional bearing inspection completed.' };
const copy = value => JSON.parse(JSON.stringify(value));
const lab = (attempt = 1, revision = 0, extra = {}) => ({ id: 8, attempt, state_version: [attempt, revision],
  documents: [], messages: [], use_kb: false, kb_revision: 0, synced_revision: 0,
  defense_level: 0, completed: false, exploited: false, last_result: null, ...extra });
const response = record => ({ lab: record, summary: { score: 0, completed: [] }, request_id: 'request-current' });

function ui(record = lab()) {
  const elements = new Map();
  class Element {
    constructor() { this.value = ''; this.hidden = true; this.textContent = ''; }
    focus() {}
    showModal() {}
  }
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({ document: { getElementById: get, querySelector: () => null },
    window: { confirm: () => true }, setTimeout: () => 0, clearTimeout() {}, initial: copy(record) });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`state.current = 8; state.labs['8'] = initial;
    drawLab = drawNavigation = updateControls = drawConversation = drawWorkspace = () => {};`);
  const requests = [];
  const respond = value => { context.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return { ok: true, json: async () => copy(value) };
  }; };
  const accept = record => { context.incoming = response(record); return run('acceptResponse(8, incoming)'); };
  const pending = () => {
    let release;
    context.fetch = () => new Promise(resolve => { release = (value, ok = true) => resolve({ ok, json: async () => value }); });
    return (...args) => release(...args);
  };
  return { run, context, get, requests, respond, accept, pending };
}

test('save sends sync=false and preserves the current retrieval revision', async () => {
  const view = ui(lab(1, 0, { synced_revision: 3 }));
  view.context.note = note;
  view.respond(response(lab(1, 1, { documents: [note], kb_revision: 4, synced_revision: 3 })));
  await view.run('updateKnowledge({documents:[note],clearEditor:true})');
  assert.equal(view.requests[0].url, '/api/labs/8/kb');
  assert.equal(view.requests[0].body.sync, false);
  assert.deepEqual(view.requests[0].body.documents, [note]);
  assert.equal(view.run("state.labs['8'].synced_revision"), 3);
});

test('sync explicitly sends saved documents and enables retrieval', async () => {
  const view = ui(lab(1, 1, { documents: [note], kb_revision: 4, synced_revision: 3 }));
  view.respond(response(lab(1, 2, { documents: [note], use_kb: true, kb_revision: 4, synced_revision: 4 })));
  await view.run('updateKnowledge({sync:true,useKB:true})');
  assert.deepEqual(view.requests[0].body, { documents: [note], sync: true, use_kb: true });
  assert.equal(view.run("state.labs['8'].synced_revision"), 4);
});

test('chat uses this lab retrieval toggle and accepts a matching request result', async () => {
  const view = ui(lab(1, 0, { use_kb: true }));
  view.get('prompt').value = 'Explain the maintenance handover.';
  const result = response(lab(1, 1, { use_kb: true, last_result: { request_id: 'request-current', defense_level: 0 } }));
  view.respond(result);
  await view.run('sendMessage({preventDefault(){}})');
  assert.equal(view.requests[0].url, '/api/labs/8/chat');
  assert.equal(view.requests[0].body.use_kb, true);
  assert.equal(view.run("state.labs['8'].last_result.request_id"), result.request_id);
});

test('a mismatched request result cannot replace the current response', async () => {
  const view = ui();
  view.get('prompt').value = 'Explain the maintenance handover.';
  view.respond(response(lab(1, 1, { last_result: { request_id: 'unrelated-request' } })));
  await view.run('sendMessage({preventDefault(){}})');
  assert.equal(view.run("state.labs['8'].last_result"), null);
  assert.match(view.get('error').textContent, /does not match/);
});

test('old attempts, old revisions, and unversioned responses cannot replace current state', () => {
  const view = ui(lab(3, 7, { defense_level: 2 }));
  for (const old of [lab(2, 99), lab(3, 6), { id: 8, messages: [] }]) {
    assert.equal(view.accept(old), false);
    assert.equal(view.run("state.labs['8'].defense_level"), 2);
    assert.equal(view.run("state.labs['8'].state_version[1]"), 7);
  }
  assert.equal(view.accept(lab(3, 8, { defense_level: 2 })), undefined);
  assert.equal(view.run("state.labs['8'].state_version[1]"), 8);
});

test('late initial state cannot undo a newer attempt or difficulty', () => {
  const view = ui(lab(2, 3, { defense_level: 2 }));
  view.context.old = { catalog: [], labs: { '8': lab(1, 2) }, summary: { score: 0 } };
  view.run('acceptInitialState(old)');
  assert.equal(view.run("state.labs['8'].attempt"), 2);
  assert.equal(view.run("state.labs['8'].defense_level"), 2);
});

test('a delayed chat response cannot replace a later synchronized snapshot', async () => {
  const view = ui();
  view.get('prompt').value = 'Explain the maintenance handover.';
  const release = view.pending();
  const pending = view.run('sendMessage({preventDefault(){}})');
  view.accept(lab(1, 2, { synced_revision: 2 }));
  release(response(lab(1, 1, { last_result: { request_id: 'request-current' } })));
  await pending;
  assert.equal(view.run("state.labs['8'].synced_revision"), 2);
  assert.equal(view.run("state.labs['8'].last_result"), null);
});

test('an older pending chat cannot clear or replace a newer pending request', async () => {
  const view = ui();
  view.get('prompt').value = 'Explain the maintenance handover.';
  const release = view.pending();
  const pending = view.run('sendMessage({preventDefault(){}})');
  view.run("state.pending.set(8,{message:'New request'})");
  release(response(lab(1, 1, { last_result: { request_id: 'request-current' } })));
  await pending;
  assert.equal(view.run('state.pending.get(8).message'), 'New request');
  assert.equal(view.run("state.labs['8'].last_result"), null);
});

test('a stale chat failure cannot restore a draft or error after a newer attempt', async () => {
  const view = ui();
  view.get('prompt').value = 'Old maintenance question.';
  const release = view.pending();
  const pending = view.run('sendMessage({preventDefault(){}})');
  view.accept(lab(2, 2));
  view.run("state.promptDrafts.set(8,'New draft')");
  release({ error: 'Old request error' }, false);
  await pending;
  assert.equal(view.run('state.promptDrafts.get(8)'), 'New draft');
  assert.equal(view.get('error').hidden, true);
});

test('a stale document-save response cannot clear a newer editor or notice', async () => {
  const view = ui();
  const release = view.pending();
  const pending = view.run("updateKnowledge({documents:[],clearEditor:true,notice:'Old notice'})");
  view.accept(lab(2, 2));
  view.run("state.editors.set(8,{title:'New draft'});state.kbNotices.set(8,'Current notice')");
  release(response(lab(1, 1)));
  await pending;
  assert.equal(view.run('state.editors.get(8).title'), 'New draft');
  assert.equal(view.run('state.kbNotices.get(8)'), 'Current notice');
});

test('a late reset response cannot clear drafts from a newer attempt', async () => {
  const view = ui();
  const release = view.pending();
  const pending = view.run('restartLab()');
  view.accept(lab(3, 4));
  view.run("state.promptDrafts.set(8,'Current draft')");
  release(response(lab(2, 2)));
  await pending;
  assert.equal(view.run('state.promptDrafts.get(8)'), 'Current draft');
  assert.equal(view.run("state.labs['8'].attempt"), 3);
});

test('difficulty responses retain the effective server-selected level', async () => {
  const view = ui();
  view.get('defense-select').value = '2';
  view.respond(response(lab(2, 1, { defense_level: 2 })));
  await view.run('changeDefense()');
  assert.deepEqual(view.requests[0].body, { level: 2 });
  assert.equal(view.run("state.labs['8'].defense_level"), 2);
  assert.equal(view.accept(lab(1, 99, { defense_level: 0 })), false);
});

test('a stale flag success cannot overwrite a newer attempt or its feedback', async () => {
  const view = ui(lab(1, 1, { exploited: true, flag: 'fixture' }));
  view.get('flag-input').value = 'fixture';
  const release = view.pending();
  const pending = view.run('submitFlag({preventDefault(){}})');
  view.accept(lab(2, 2));
  view.run("state.flagFeedback.set(8,{message:'Current feedback'})");
  release(response(lab(1, 2, { completed: true, exploited: true })));
  await pending;
  assert.equal(view.run('state.flagFeedback.get(8).message'), 'Current feedback');
  assert.equal(view.run("state.labs['8'].completed"), false);
});

test('a stale flag failure cannot overwrite current feedback', async () => {
  const view = ui(lab(1, 1, { exploited: true, flag: 'fixture' }));
  view.get('flag-input').value = 'fixture';
  const release = view.pending();
  const pending = view.run('submitFlag({preventDefault(){}})');
  view.accept(lab(2, 2));
  view.run("state.flagFeedback.set(8,{message:'Current feedback'})");
  release({ error: 'Old flag error' }, false);
  await pending;
  assert.equal(view.run('state.flagFeedback.get(8).message'), 'Current feedback');
});

test('receiving a Lab 8 result while viewing another lab preserves that lab', () => {
  const view = ui();
  view.run("state.current=7;state.labs['7']={id:7,defense_level:1,documents:[]}");
  view.accept(lab(1, 1));
  assert.equal(view.run('state.current'), 7);
  assert.equal(view.run("state.labs['7'].defense_level"), 1);
});
