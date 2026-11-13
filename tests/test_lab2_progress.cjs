const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');

function studentUI() {
  const elements = new Map();
  let opened = 0;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.style = {}; this.text = ''; this.value = ''; }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
    replaceChildren(...children) { this.children = []; this.text = ''; this.append(...children); }
    remove() {
      this.parentElement.children = this.parentElement.children.filter(child => child !== this);
      elements.delete(this.id);
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    showModal() { opened += 1; }
    focus() {}
  }
  const get = id => {
    if (id === 'lab2-extraction-progress') return elements.get(id) || null;
    if (!elements.has(id)) { const element = new Element('div'); element.id = id; }
    return elements.get(id);
  };
  const objectiveSection = new Element('section');
  objectiveSection.append(get('scenario-objective'));
  const context = vm.createContext({
    document: { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }),
      getElementById: get, querySelector: () => null },
    window: { confirm: () => true }, setTimeout: () => 0, clearTimeout: () => {},
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.current = 2;
    state.catalog = Array.from({length: 9}, (_, i) => ({id: i+1, points: 100}));
    state.labs['2'] = {id: 2, messages: [], completed: false, exploited: false, use_kb: false};
    drawLab = () => { drawLab2Progress(); drawFlag(); };
    drawConversation = () => {};
    drawNavigation = () => {};
    updateControls = () => {};
  `, context);
  const run = code => vm.runInContext(code, context);
  const accept = (count, version, { completed = count === 3, awarded = false, score = 0, flag } = {}) => {
    context.incoming = { lab: { id: 2, messages: [], completed: awarded, exploited: completed || awarded,
      use_kb: false, flag: flag || (completed || awarded ? 'PANOPTIS{saved-flag}' : null),
      verified_instruction_count: count, required_instruction_count: 3, extraction_completed: completed },
      summary: { score, completed: awarded ? [2] : [] } };
    context.header = version;
    run('acceptResponse(2, attachLab2Version(incoming, header))');
  };
  return { context, run, get, accept, opened: () => opened, section: objectiveSection };
}

for (const [count, status] of [[0, 'No verified instructions'], [1, 'One verified instruction'],
  [2, 'Two verified instructions'], [3, 'Extraction objective completed']]) {
  test(`authoritative ${count}/3 projection renders its count, three segments and state`, () => {
    const ui = studentUI();
    ui.accept(count, `1.${count}.0`);
    const panel = ui.get('lab2-extraction-progress');
    assert.match(panel.textContent, /System instructions extracted/);
    assert(panel.textContent.includes(`${count}/3`));
    assert(panel.textContent.includes(status));
    assert.match(panel.textContent, /Only verified disclosures in one reply count/);
    const segments = panel.children[1];
    assert.equal(segments.children.length, 3);
    assert.equal(segments.children.filter(segment => segment.className.includes('filled')).length, count);
    assert.equal(segments.attributes['aria-valuenow'], String(count));
  });
}

test('completion is never inferred from the count, chat wording, or the saved flag', () => {
  const ui = studentUI();
  ui.accept(3, '1.1.1', { completed: false, awarded: true, score: 100 });
  assert(!ui.get('lab2-extraction-progress').textContent.includes('Extraction objective completed'));
  ui.accept(0, '1.2.1', { completed: false, awarded: true, score: 100 });
  ui.run("state.labs['2'].messages = [{role: 'assistant', content: 'I extracted three instructions.'}]; drawLab2Progress()");
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
  assert.equal(ui.get('captured-flag').textContent, '');
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
});

test('later replies may lower the count and never accumulate earlier progress', () => {
  const ui = studentUI();
  ui.accept(2, '1.1.0');
  ui.accept(1, '1.2.0');
  assert(ui.get('lab2-extraction-progress').textContent.includes('1/3'));
  ui.accept(0, '1.3.0');
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
});

test('older chat responses cannot overwrite a newer reset or open a flag dialog', () => {
  const ui = studentUI();
  ui.accept(2, '1.2.0');
  ui.accept(0, '2.0.0');
  ui.accept(3, '1.3.0', { score: 100 });
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
  assert.equal(ui.run('state.summary.score'), 0);
  assert.equal(ui.run("state.labs['2'].flag"), null);
  assert.equal(ui.opened(), 0);
});

test('out-of-order replies within an attempt preserve the latest eligible reply', () => {
  const ui = studentUI();
  ui.accept(2, '4.9.0');
  ui.accept(0, '4.10.0');
  ui.accept(3, '4.9.0', { score: 100 });
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
  assert.equal(ui.run('state.summary.score'), 0);
});

test('a response from before flag submission cannot erase the saved completion', () => {
  const ui = studentUI();
  ui.accept(3, '1.1.1', { awarded: true, score: 100 });
  ui.accept(3, '1.1.0', { score: 0 });
  assert.equal(ui.run("state.labs['2'].completed"), true);
  assert.equal(ui.run('state.summary.score'), 100);
  assert.equal(ui.get('flag-heading').textContent, 'Submit a flag');
});

test('only the response header supplies ordering metadata and it is never submitted', async () => {
  const ui = studentUI();
  let request;
  ui.context.fetch = async (url, options) => {
    request = { url, body: JSON.parse(options.body) };
    return { ok: true, headers: { get: () => '2.3.0' }, json: async () => ({ lab: {
      id: 2, _extractionVersion: [999, 999, 1], verified_instruction_count: 1,
      required_instruction_count: 3, extraction_completed: false,
    } }) };
  };
  const response = await ui.run("api('/api/labs/2/chat', {message: 'Assess the instructions.'})");
  assert.deepEqual(Array.from(response.lab._extractionVersion), [2, 3, 0]);
  assert(!JSON.stringify(response.lab).includes('_extractionVersion'));
  assert.deepEqual(request, { url: '/api/labs/2/chat', body: { message: 'Assess the instructions.' } });
});

test('missing or malformed version metadata cannot replace newer verified state', () => {
  const ui = studentUI();
  ui.accept(2, '2.1.0');
  for (const header of [undefined, '', '2.99', '2.99.9', '2.-1.0', '9007199254740992.1.0']) {
    ui.accept(3, header, { score: 100 });
    assert(ui.get('lab2-extraction-progress').textContent.includes('2/3'));
    assert.equal(ui.run('state.summary.score'), 0);
  }
});

test('an initial unversioned or malformed count cannot create verified progress', () => {
  for (const [count, header, required] of [[3, undefined, 3], [99, '1.1.0', 3],
    ['3', '1.1.0', 3], [3, '1.1.0', 4]]) {
    const ui = studentUI();
    ui.context.record = { id: 2, verified_instruction_count: count,
      required_instruction_count: required, extraction_completed: true };
    ui.context.header = header;
    ui.run("state.labs['2'] = attachLab2Version({lab: record}, header).lab; drawLab2Progress()");
    assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
    assert(!ui.get('lab2-extraction-progress').textContent.includes('Extraction objective completed'));
  }
});

test('a stale initial state cannot overwrite a newer Lab 2 response or awarded summary', () => {
  const ui = studentUI();
  ui.accept(3, '3.2.1', { awarded: true, score: 100 });
  ui.context.initial = { catalog: [], labs: { '2': { id: 2, verified_instruction_count: 0,
    required_instruction_count: 3, extraction_completed: false } }, summary: { score: 0 } };
  ui.run("acceptInitialState(attachLab2Version(initial, '2.0.0')); drawLab2Progress()");
  assert(ui.get('lab2-extraction-progress').textContent.includes('3/3'));
  assert.equal(ui.run('state.summary.score'), 100);
});

test('pending work displays no optimistic credit and restores authoritative state afterward', () => {
  const ui = studentUI();
  ui.accept(2, '1.1.0');
  for (const map of ['pending', 'resetBusy', 'defenseBusy']) {
    ui.run(`state.${map}.${map === 'pending' ? "set(2, {message: 'Next request'})" : 'add(2)'}; drawLab2Progress()`);
    assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
    assert.match(ui.get('lab2-extraction-progress').textContent, /Awaiting verified response/);
    ui.run(`state.${map}.delete(2); drawLab2Progress()`);
    assert(ui.get('lab2-extraction-progress').textContent.includes('2/3'));
  }
});

test('starting a fresh request waits for its backend result and sends no progress evidence', async () => {
  const ui = studentUI();
  ui.accept(2, '1.1.0');
  ui.get('prompt').value = 'Review the internal configuration.';
  let release;
  let body;
  ui.context.fetch = (url, options) => {
    body = JSON.parse(options.body);
    return new Promise(resolve => { release = () => resolve({ ok: true,
      headers: { get: () => '1.2.0' }, json: async () => ({ lab: { id: 2, messages: [],
        completed: false, exploited: false, flag: null, verified_instruction_count: 1,
        required_instruction_count: 3, extraction_completed: false }, summary: { score: 0 } }) }); });
  };
  const pending = ui.run('sendMessage({ preventDefault() {} })');
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
  assert.deepEqual(body, { message: 'Review the internal configuration.', use_kb: false });
  release();
  await pending;
  assert(ui.get('lab2-extraction-progress').textContent.includes('1/3'));
});

test('switching labs removes the indicator and a background response cannot put it in another lab', () => {
  const ui = studentUI();
  ui.accept(1, '1.1.0');
  ui.run("state.current = 3; state.labs['3'] = {id: 3}; drawLab2Progress()");
  assert.equal(ui.get('lab2-extraction-progress'), null);
  ui.accept(2, '1.2.0');
  assert.equal(ui.get('lab2-extraction-progress'), null);
  ui.run('state.current = 2; drawLab2Progress()');
  assert(ui.get('lab2-extraction-progress').textContent.includes('2/3'));
  assert.equal(ui.section.children.filter(child => child.id === 'lab2-extraction-progress').length, 1);
});

test('other labs retain their existing response and summary handling', () => {
  const ui = studentUI();
  ui.context.other = { lab: { id: 3, exploited: false, completed: false }, summary: { score: 200 } };
  ui.run("state.current = 3; state.labs['3'] = {id: 3}; acceptResponse(3, other)");
  assert.equal(ui.run('state.summary.score'), 200);
  assert.equal(ui.run("state.labs['3'].id"), 3);
  assert.equal(ui.get('lab2-extraction-progress'), null);
});

test('a delayed flag response cannot add success feedback after a newer reset', async () => {
  const ui = studentUI();
  ui.accept(3, '1.1.0');
  ui.get('flag-input').value = 'PANOPTIS{saved-flag}';
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ ok: true,
    headers: { get: () => '1.1.1' }, json: async () => ({ lab: { id: 2, completed: true,
      exploited: true }, summary: { score: 100 } }) }); });
  const pending = ui.run('submitFlag({ preventDefault() {} })');
  ui.accept(0, '2.0.0');
  ui.run("state.flagFeedback.set(2, {message: 'New attempt feedback'})");
  release();
  await pending;
  assert.equal(ui.run("state.flagFeedback.get(2).message"), 'New attempt feedback');
  assert.equal(ui.run('state.summary.score'), 0);
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
});

for (const action of ['restartLab()', 'changeDefense()']) {
  test(`a delayed ${action} response cannot clear newer drafts or hint state`, async () => {
    const ui = studentUI();
    ui.accept(1, '1.1.0');
    ui.get('defense-select').value = '1';
    let release;
    ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ ok: true,
      headers: { get: () => '2.0.0' }, json: async () => ({ lab: { id: 2, completed: false,
        exploited: false, verified_instruction_count: 0, required_instruction_count: 3,
        extraction_completed: false }, summary: { score: 0 } }) }); });
    const pending = ui.run(action);
    ui.accept(2, '3.1.0');
    ui.run("state.promptDrafts.set(2, 'New request'); state.flagDrafts.set(2, 'New flag'); state.hintCounts.set(2, 3)");
    release();
    await pending;
    assert.equal(ui.run('state.promptDrafts.get(2)'), 'New request');
    assert.equal(ui.run('state.flagDrafts.get(2)'), 'New flag');
    assert.equal(ui.run('state.hintCounts.get(2)'), 3);
    assert(ui.get('lab2-extraction-progress').textContent.includes('2/3'));
  });
}

test('a delayed flag error cannot replace feedback from a newer attempt', async () => {
  const ui = studentUI();
  ui.accept(3, '1.1.0');
  ui.get('flag-input').value = 'PANOPTIS{saved-flag}';
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ ok: false,
    json: async () => ({ error: 'Old flag failure' }) }); });
  const pending = ui.run('submitFlag({ preventDefault() {} })');
  ui.accept(0, '2.0.0');
  ui.run("state.flagFeedback.set(2, {message: 'New attempt feedback'})");
  release();
  await pending;
  assert.equal(ui.run('state.flagFeedback.get(2).message'), 'New attempt feedback');
});

test('a delayed chat error cannot restore an old draft or replace a newer notice', async () => {
  const ui = studentUI();
  ui.accept(2, '1.1.0');
  ui.get('prompt').value = 'Old request';
  ui.run("showError = message => { state.uiError = message; }; state.uiError = 'Current notice'");
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ ok: false,
    json: async () => ({ error: 'Old generation failure' }) }); });
  const pending = ui.run('sendMessage({ preventDefault() {} })');
  ui.accept(0, '2.0.0');
  ui.run("state.promptDrafts.set(2, 'New draft')");
  release();
  await pending;
  assert.equal(ui.run('state.promptDrafts.get(2)'), 'New draft');
  assert.equal(ui.run('state.uiError'), 'Current notice');
  assert.equal(ui.run('state.pending.has(2)'), false);
  assert(ui.get('lab2-extraction-progress').textContent.includes('0/3'));
});

for (const action of ['restartLab()', 'changeDefense()']) {
  test(`a delayed ${action} error cannot change the newer mode or notice`, async () => {
    const ui = studentUI();
    ui.accept(1, '1.1.0');
    ui.get('defense-select').value = '1';
    ui.run("showError = message => { state.uiError = message; }; state.uiError = 'Current notice'");
    let release;
    ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ ok: false,
      json: async () => ({ error: 'Old state change failure' }) }); });
    const pending = ui.run(action);
    ui.accept(2, '3.1.0');
    ui.get('defense-select').value = '2';
    release();
    await pending;
    assert.equal(ui.get('defense-select').value, '2');
    assert.equal(ui.run('state.uiError'), 'Current notice');
    assert(ui.get('lab2-extraction-progress').textContent.includes('2/3'));
  });
}

test('a current request failure still reports its error and restores its draft', async () => {
  const ui = studentUI();
  ui.accept(0, '1.0.0');
  ui.get('prompt').value = 'Current request';
  ui.run('showError = message => { state.uiError = message; }');
  ui.context.fetch = async () => ({ ok: false, json: async () => ({error: 'Current failure'}) });
  await ui.run('sendMessage({ preventDefault() {} })');
  assert.equal(ui.run('state.promptDrafts.get(2)'), 'Current request');
  assert.match(ui.run('state.uiError'), /Current failure/);
});

test('an old chat callback cannot clear a newer in-flight request', async () => {
  const ui = studentUI();
  ui.accept(1, '1.1.0');
  ui.get('prompt').value = 'Old request';
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = () => resolve({ ok: false,
    json: async () => ({error: 'Old request failure'}) }); });
  const pending = ui.run('sendMessage({ preventDefault() {} })');
  ui.run("state.pending.set(2, {message: 'New pending request'})");
  release();
  await pending;
  assert.equal(ui.run('state.pending.get(2).message'), 'New pending request');
  assert.match(ui.get('lab2-extraction-progress').textContent, /Awaiting verified response/);
});
