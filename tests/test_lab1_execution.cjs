const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\nstart\(\);\s*$/, '');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.text = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
}

function studentUI(lab = 1, events = []) {
  const messages = new Element('div');
  const elements = new Map([['messages', messages]]);
  const context = vm.createContext({
    document: { createElement: tag => new Element(tag), getElementById: id => {
      if (!elements.has(id)) elements.set(id, new Element('div'));
      return elements.get(id);
    } },
    lab, events,
  });
  vm.runInContext(source, context);
  vm.runInContext(`state.current = lab; state.labs[String(lab)] = {
    boiler: { water: 60, fuel: 55 }, boiler_tool_calls: events };`, context);
  return { run: code => vm.runInContext(code, context), context, messages };
}

for (const [status, message, water, fuel] of [
  ['read_only', 'Read-only operation. No settings changed. Any success claim is incorrect.', 60, 55],
  ['write_executed', 'Write execution confirmed.', 70, 65],
  ['write_rejected', 'The input-change request was rejected. No settings changed.', 60, 55],
  ['no_tools', 'No tools executed. No settings changed.', 60, 55],
]) {
  test(`Lab 1 displays the authoritative ${status} receipt before useful model commentary`, () => {
    const ui = studentUI();
    ui.context.reply = 'I can help explain these boiler readings.';
    ui.context.receipt = { status, message, water_in_pct: water, fuel_in_pct: fuel };
    ui.run("appendMessage('assistant', reply, false, receipt)");
    const wrapper = ui.messages.children[0];
    const [record, speaker, bubble] = wrapper.children;
    assert.equal(record.tagName, 'aside');
    assert.equal(record.attributes['aria-label'], 'Authoritative simulator record');
    assert.equal(record.dataset.status, status);
    assert(record.textContent.includes(message));
    assert(record.textContent.includes(`Water In: ${water}%`));
    assert(record.textContent.includes(`Fuel In: ${fuel}%`));
    assert.equal(speaker.textContent, 'PLANT AI · MODEL RESPONSE');
    assert.equal(bubble.textContent, ui.context.reply);
  });
}

test('earlier Lab 1 replies are corrected conservatively using current saved inputs', () => {
  const ui = studentUI(1, [{ tool_name: 'get_boiler_status', decision: 'executed' }]);
  ui.run("appendMessage('assistant', 'The backend accepted the operation.')");
  const record = ui.messages.children[0].children[0];
  assert.equal(record.dataset.status, 'unverified');
  assert(record.textContent.includes('recorded activity was read-only'));
  assert(record.textContent.includes('Any claim of a successful change is unverified'));
  assert(record.textContent.includes('Current saved inputs · Water In: 60% · Fuel In: 55%'));
});

const receiptMessages = {
  write_executed: 'Write execution confirmed. The saved input settings are shown below.',
  write_rejected: 'The input-change request was rejected. No settings changed.',
  read_only: 'Read-only operation: only boiler status was retrieved. No settings changed.',
  no_tools: 'No tools executed. No settings changed.',
  write_unverified: 'The saved inputs do not confirm the reported write.',
};

function renderReply(status, reply) {
  const ui = studentUI();
  ui.context.reply = reply;
  ui.context.receipt = status ? {
    status, message: receiptMessages[status],
    water_in_pct: status === 'write_executed' ? 70 : 60,
    fuel_in_pct: status === 'write_executed' ? 65 : 55,
  } : null;
  ui.run("appendMessage('assistant', reply, false, receipt)");
  const wrapper = ui.messages.children[0];
  return { ui, wrapper, record: wrapper.children[0], bubble: wrapper.children[2],
    debug: wrapper.children.find(child => child.tagName === 'details') };
}

// Match native <details>: only the summary is visible until a user opens it.
function visibleText(element) {
  const children = element.tagName === 'details' && !element.open
    ? element.children.filter(child => child.tagName === 'summary') : element.children;
  return element.text + children.map(visibleText).join(' ');
}

for (const contradiction of [
  'The change was logged but not executed due to authorization rules.',
  'The input change was rejected because you are a read-only auditor.',
  'The write was blocked by authorization rules.',
  'It was rejected.',
  'The operation could not be completed.',
  'Authorization rules prevented execution.',
  'The settings were only simulated.',
  'The changes have only been simulated.',
  'This was a dry run of the input change.',
  'I cannot change the inputs without operator authorization.',
  'No settings changed.',
  'The inputs stayed the same.',
  'The request was logged without applying any changes.',
  'No actual write occurred.',
  'I could not modify the inputs.',
  'The tool result was {"ok": false}.',
]) {
  test(`successful write hides contradictory commentary: ${contradiction}`, () => {
    const readings = 'Water In: 70%. Fuel In: 65%. Temperature: 148.5 C. Pressure: 3.1 bar.';
    const useful = 'Keep monitoring the pressure trend.';
    const reply = `${readings}\n${contradiction}\n${useful}`;
    const result = renderReply('write_executed', reply);
    assert.match(result.record.textContent, /Write execution confirmed/);
    assert.match(result.record.textContent, /Water In: 70%.*Fuel In: 65%/);
    assert(result.bubble.textContent.includes(readings));
    assert(result.bubble.textContent.includes(useful));
    assert(!result.bubble.textContent.includes(contradiction));
    assert(!visibleText(result.wrapper).includes(contradiction));
    assert(result.debug);
    assert(!result.debug.open);
    assert.match(result.debug.children[0].textContent, /Raw model response.*unverified/);
    assert.match(result.debug.children[1].textContent, /Debug text only.*may contradict/);
    assert.equal(result.debug.children[2].textContent, reply);
    assert.equal(result.ui.context.reply, reply);
  });
}

for (const successClaim of [
  'I successfully executed set_boiler_inputs.',
  'The backend accepted the operation.',
  'I have set Water In to 70% and Fuel In to 65%.',
  'Water In has now been set to 70% and Fuel In has now been set to 65%.',
  'The inputs were changed successfully.',
  'The input update has been successful.',
  'The change went through successfully.',
  'Done!',
  'Applied!',
  'The result is {"ok": true}.',
  'The simulator returned these values after the adjustment.',
]) {
  test(`failed write hides unsupported success: ${successClaim}`, () => {
    const readings = 'Water In: 60%. Fuel In: 55%. Water Level: 61%. Running: true.';
    const result = renderReply('write_rejected', `${successClaim}\n${readings}`);
    assert.match(result.record.textContent, /rejected.*No settings changed/);
    assert(!visibleText(result.wrapper).includes(successClaim));
    assert.equal(result.bubble.textContent, readings);
    assert(result.debug.children[2].textContent.includes(successClaim));
  });
}

test('read-only execution keeps readings and useful discussion while suppressing a false input change', () => {
  const reply = 'I retrieved the current boiler status.\nWater In: 60%. Fuel In: 55%. Power Output: 36 kW.\n'
    + 'I changed both inputs successfully.\nAn operator should review pressure trends.';
  const result = renderReply('read_only', reply);
  assert.match(result.record.textContent, /Read-only operation.*No settings changed/);
  assert(result.bubble.textContent.includes('I retrieved the current boiler status.'));
  assert(result.bubble.textContent.includes('Water In: 60%. Fuel In: 55%. Power Output: 36 kW.'));
  assert(result.bubble.textContent.includes('An operator should review pressure trends.'));
  assert(!visibleText(result.wrapper).includes('I changed both inputs successfully.'));
});

for (const [status, reply] of [
  ['write_executed', 'I executed set_boiler_inputs successfully. Water In: 70%. Fuel In: 65%.'],
  ['write_executed', 'The write was not rejected. The inputs have been updated.'],
  ['write_rejected', 'The input-change request was rejected. No settings changed. Water In: 60%. Fuel In: 55%.'],
  ['write_rejected', 'The write was not executed. I did not change the inputs.'],
  ['read_only', 'I retrieved the current status. No settings changed. Water In: 60%. Fuel In: 55%.'],
  ['read_only', 'The status request succeeded. Water In: 60%. Fuel In: 55%.'],
  ['no_tools', 'No input changes were made. I can explain the monitoring procedure.'],
]) {
  test(`correct ${status} execution commentary remains intact: ${reply}`, () => {
    const result = renderReply(status, reply);
    assert.equal(result.bubble.textContent, reply);
    assert.equal(result.debug, undefined);
  });
}

for (const status of ['no_tools', 'write_unverified', null]) {
  test(`unconfirmed execution (${status}) never promotes model success into a fact`, () => {
    const result = renderReply(status, 'I executed set_boiler_inputs successfully.\nI can explain the input limits.');
    assert.equal(result.bubble.textContent, 'I can explain the input limits.');
    assert(!visibleText(result.wrapper).includes('I executed set_boiler_inputs successfully.'));
  });
}

test('readings survive when a contradictory claim follows in the same sentence', () => {
  const result = renderReply('write_executed', 'Water In: 70%, Fuel In: 65%, but the write was not executed.');
  assert.equal(result.bubble.textContent, 'Water In: 70%, Fuel In: 65%');
  assert(!visibleText(result.wrapper).includes('the write was not executed'));
});

test('conversation reload uses each stored receipt rather than the latest execution', () => {
  const ui = studentUI();
  ui.context.messages = [
    { role: 'assistant', content: 'The input change was rejected.\nWater In: 70%. Fuel In: 65%.',
      boiler_execution: { status: 'write_executed', message: receiptMessages.write_executed, water_in_pct: 70, fuel_in_pct: 65 } },
    { role: 'assistant', content: 'I executed set_boiler_inputs.\nI can explain the readings.',
      boiler_execution: { status: 'no_tools', message: receiptMessages.no_tools, water_in_pct: 70, fuel_in_pct: 65 } },
  ];
  const saved = JSON.stringify(ui.context.messages);
  ui.run('state.labs["1"].messages = messages; drawConversation()');
  assert.equal(ui.messages.children[0].children[0].dataset.status, 'write_executed');
  assert.equal(ui.messages.children[0].children[2].textContent, 'Water In: 70%. Fuel In: 65%.');
  assert.equal(ui.messages.children[1].children[0].dataset.status, 'no_tools');
  assert.equal(ui.messages.children[1].children[2].textContent, 'I can explain the readings.');
  assert.equal(JSON.stringify(ui.context.messages), saved);
});

test('execution reconciliation does not change any other lab or student message', () => {
  const reply = 'The write was rejected. I executed set_boiler_inputs successfully.';
  for (let lab = 2; lab <= 9; lab += 1) {
    const ui = studentUI(lab);
    ui.context.reply = reply;
    ui.run("appendMessage('assistant', reply, false, { status: 'write_executed' })");
    assert.equal(ui.messages.children[0].children.length, 2);
    assert.equal(ui.messages.children[0].children[1].textContent, reply);
  }
  const ui = studentUI();
  ui.context.reply = reply;
  ui.run("appendMessage('user', reply)");
  assert.equal(ui.messages.children[0].children[1].textContent, reply);
});

test('previous write events do not verify an earlier reply without its own receipt', () => {
  const ui = studentUI(1, [{ tool_name: 'set_boiler_inputs', decision: 'executed' }]);
  ui.run("appendMessage('assistant', 'An earlier reply.')");
  const record = ui.messages.children[0].children[0];
  assert.equal(record.dataset.status, 'unverified');
  assert(record.textContent.includes('no stored execution confirmation'));
});

test('other labs and student messages keep their existing conversation rendering', () => {
  const other = studentUI(2);
  other.run("appendMessage('assistant', 'A natural response.')");
  assert.equal(other.messages.children[0].children.length, 2);
  assert.equal(other.messages.children[0].children[0].textContent, 'PLANT AI');
  assert.equal(other.messages.children[0].children[1].textContent, 'A natural response.');
  const student = studentUI();
  student.run("appendMessage('user', 'Hello.')");
  assert.equal(student.messages.children[0].children.length, 2);
  assert.equal(student.messages.children[0].children[0].textContent, 'YOU');
});
