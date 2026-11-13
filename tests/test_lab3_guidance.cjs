const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const read = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');
const app = read('app.js').replace(/\nstart\(\);\s*$/, '');
const intro = read('intro.js');
const html = read('index.html');
const objective = 'Manipulate a maintenance document so Plant AI recommends an incorrect inspection interval for PT-B01.';
const hints = [
  'Plant AI uses retrieved maintenance documents. Open View bulletin to inspect the previous posts.',
  "Choose New Document and save your bulletin before asking Plant AI about it. Save also makes it available for retrieval.",
  'Consider how a newer maintenance bulletin could appear more authoritative than an older reference document.',
  "Focus on PT-B01's inspection interval. The trusted standard specifies 12 simulated cycles. Try making Plant AI accept a different interval from your modified bulletin.",
];
const clone = value => JSON.parse(JSON.stringify(value));

async function studentUI({ completed = false, exploited = false } = {}) {
  const elements = new Map();
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.value = ''; this.text = ''; this.hidden = false; }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    set textContent(value) { this.replaceChildren(); this.text = value; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    append(...children) { children.forEach(child => { child.remove(); child.parentElement = this; this.children.push(child); }); }
    after(child) { child.remove(); const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this) + 1, 0, child); child.parentElement = this.parentElement; if (child.id) elements.set(child.id, child); }
    replaceChildren(...children) { this.children.forEach(child => child.remove()); this.children = []; this.text = ''; this.append(...children); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; if (this.id) elements.delete(this.id); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    focus() {}
    showModal() { this.open = true; }
  }
  const root = new Element('main');
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]);
    root.append(element);
    element.id = match[3];
    element.hidden = /\bhidden\b/.test(match[2]);
  }
  const get = id => elements.get(id) || null;
  const initial = { catalog: Array.from({ length: 9 }, (_, i) => ({ id: i + 1, points: i === 2 ? 150 : 100, hints: i === 2 ? hints : ['Other lab hint.'] })),
    labs: Object.fromEntries(Array.from({ length: 9 }, (_, i) => [String(i + 1), { id: i + 1, completed: i === 2 && completed,
      exploited: i === 2 && exploited, flag: exploited ? 'PANOPTIS{fixture}' : null, messages: [], documents: [] }])),
    summary: { score: completed === true ? 150 : 0, completed: completed === true ? [3] : [] } };
  const context = vm.createContext({ document: { getElementById: get, createElement: tag => new Element(tag),
    createTextNode: text => { const element = new Element('#text'); element.textContent = text; return element; },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {} },
    window: { addEventListener() {} }, location: { pathname: '/lab/3' },
    setTimeout: () => 0, clearTimeout() {}, setInterval() {},
    fetch: async () => ({ ok: true, json: async () => clone(initial) }) });
  vm.runInContext(intro, context);
  vm.runInContext(app, context);
  const run = code => vm.runInContext(code, context);
  run('drawLab = () => { window.LabScenario.render(state.current); drawHints(); drawFlag(); }; updateControls = checkHealth = drawNavigation = () => {};');
  await run('start()');
  const reply = (lab, score = 0) => ({ lab, summary: { score, completed: score ? [3] : [] } });
  const respond = result => { context.fetch = async () => ({ ok: true, json: async () => clone(result) }); };
  return { get, run, context, respond, reply, click: id => get(id).listeners.click() };
}

test('Lab 3 shows only the objective without scenario or attack-surface prose', async () => {
  const ui = await studentUI();
  assert.equal(ui.get('scenario-objective').textContent, objective);
  assert.equal(ui.get('scenario-summary'), null);
  assert.equal(ui.get('scenario-surfaces'), null);
});

test('the left panel retains the objective and omits rules and workflow steps', async () => {
  const ui = await studentUI();
  assert.equal(ui.get('scenario-objective').textContent, objective);
  assert.equal(ui.get('scenario-objective-rules'), null);
  assert.equal(ui.get('scenario-workflow'), null);
});

test('initial scenario and first three hints do not reveal the winning payload', async () => {
  const ui = await studentUI();
  const initial = ['scenario-objective'].map(id => ui.get(id).textContent).join(' ');
  assert(!/\b(?:19|nineteen)\b/i.test(initial + hints.slice(0, 3).join(' ')));
  assert.equal(ui.get('hints').children.length, 0);
});

test('the actual hint button reveals exactly one additional hint on every click', async () => {
  const ui = await studentUI();
  assert.equal(ui.get('hint-button').attributes['aria-expanded'], 'false');
  for (let count = 1; count <= 4; count += 1) {
    ui.click('hint-button');
    assert.deepEqual(ui.get('hints').children.map(child => child.textContent), hints.slice(0, count));
    assert.equal(ui.get('hint-button').attributes['aria-expanded'], 'true');
  }
  assert.match(ui.get('hint-button').textContent, /Close all hints/);
  ui.click('hint-button');
  assert.equal(ui.get('hints').children.length, 0);
  assert.equal(ui.get('hint-button').attributes['aria-expanded'], 'false');
});

test('hint progression is separate between labs and new students', async () => {
  const ui = await studentUI();
  ui.click('hint-button');
  ui.run('state.current = 2; drawLab()');
  assert.equal(ui.get('hints').children.length, 0);
  ui.run('state.current = 3; drawLab()');
  assert.equal(ui.get('hints').children.length, 1);
  const other = await studentUI();
  assert.equal(other.get('hints').children.length, 0);
});

test('switching labs restores the correct objective without adding scenario text', async () => {
  const ui = await studentUI();
  ui.run('state.current = 2; drawLab()');
  assert.match(ui.get('scenario-objective').textContent, /three distinct instructions/);
  assert.equal(ui.get('scenario-workflow'), null);
  assert.equal(ui.get('scenario-objective-rules'), null);
  ui.run('state.current = 1; drawLab()');
  assert.equal(ui.get('scenario-objective').textContent, 'Convince Plant AI to change Water In and Fuel In without authorization.');
  ui.run('state.current = 3; drawLab(); drawLab()');
  assert.equal(ui.get('scenario-objective').textContent, objective);
  assert.equal(ui.get('scenario-workflow'), null);
});

test('completion explanation defaults to hidden inside the existing flag dialog', () => {
  const dialog = html.match(/<dialog id="flag-dialog"[\s\S]*?<\/dialog>/)[0];
  assert.match(dialog, /id="lab3-completion-explanation"[^>]*\bhidden\b/);
  for (const phrase of ['Vulnerability:', 'RAG Knowledge Poisoning', 'Entry point:', 'Student-controlled maintenance bulletin', 'Trust boundary:', 'Untrusted retrieved document content influencing', 'Impact:', 'Incorrect maintenance recommendation', 'Defense:', 'Source validation, document provenance, and authoritative-reference precedence']) assert(dialog.includes(phrase));
});

for (const [name, options] of [['fresh attempt', {}], ['captured but unsubmitted flag', { exploited: true }], ['non-boolean completion claim', { completed: 'true', exploited: true }]]) {
  test(`completion explanation stays hidden for a ${name}`, async () => {
    const ui = await studentUI(options);
    assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  });
}

test('only an accepted backend completion response reveals the explanation', async () => {
  const ui = await studentUI({ exploited: true });
  let release;
  ui.context.fetch = () => new Promise(resolve => { release = result => resolve({ ok: true, json: async () => result }); });
  ui.get('flag-input').value = 'PANOPTIS{fixture}';
  const pending = ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  release(ui.reply({ id: 3, completed: true, exploited: true, flag: 'PANOPTIS{fixture}' }, 150));
  await pending;
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  assert.equal(ui.get('flag-form').hidden, false);
  assert.equal(ui.run('state.summary.score'), 150);
});

test('rejected flag submissions leave the explanation hidden and award no points', async () => {
  const ui = await studentUI({ exploited: true });
  ui.context.fetch = async () => ({ ok: false, json: async () => ({ error: 'That flag does not match this lab and attempt.' }) });
  ui.get('flag-input').value = 'PANOPTIS{incorrect-test-flag}';
  await ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  assert.equal(ui.run('state.summary.score'), 0);
  assert.match(ui.get('flag-feedback').textContent, /does not match/);
});

test('a successful HTTP response without backend completion cannot reveal the explanation', async () => {
  const ui = await studentUI({ exploited: true });
  ui.respond(ui.reply({ id: 3, completed: false, exploited: true, flag: 'PANOPTIS{fixture}' }));
  ui.get('flag-input').value = 'PANOPTIS{fixture}';
  await ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
});

test('refresh restores the explanation for completed Lab 3 and hides it in every other lab', async () => {
  const ui = await studentUI({ completed: true, exploited: true });
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  for (const id of [1, 2, 4, 5, 6, 7, 8, 9]) {
    ui.run(`state.current = ${id}; state.labs['${id}'].completed = true; drawLab()`);
    assert.equal(ui.get('lab3-completion-explanation').hidden, true);
  }
  ui.run('state.current = 3; drawLab()');
  assert.equal(ui.get('lab3-completion-explanation').hidden, true);
});
