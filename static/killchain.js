'use strict';

const $ = (id) => document.getElementById(id);
const kc = { snapshot: null, examples: null, busy: '', source: 'review', memory: 'connector', product: '', model: '', lastRun: null, storefront: null, hintCount: 0 };
const HINTS = [
  'Put the instruction in a review’s hidden field or a PDF attachment. That is the untrusted content the connector reads.',
  'Use a routine task related to the planted instruction. The agent retrieves memory that matches the task’s topic.',
  'Compare connector memory with agent memory. Clearing only the agent notes can leave the connector ready to rebuild them.',
  'Replay the same task at Medium and Hard difficulty. Follow the approval decision and the tool’s final policy check in the trace.',
];
let pollTimer;
let polling = false;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function code(value) { return node('pre', 'kc-code', typeof value === 'string' ? value : JSON.stringify(value, null, 2)); }
function disclosure(title, value) { const item = node('details'); item.append(node('summary', '', title), code(value)); return item; }
function badge(status) { return node('span', `kc-badge ${status || ''}`, status || 'info'); }

async function api(path, body) {
  const options = body === undefined ? {} : body instanceof FormData ? { method: 'POST', body } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Could not reach the capstone. Check the connection and try again.'); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error('The capstone returned an unexpected response. Please try again.'); }
  if (!response.ok) {
    const detail = result.error || result.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item) => item.msg).join('; ') : 'The request failed.');
  }
  return result;
}

function notice(text, error = false) {
  const target = error ? $('kc-error') : $('kc-notice');
  target.textContent = text;
  target.hidden = !text;
}

function setupNavigation(data) {
  $('lab-select').replaceChildren();
  for (const lab of data.catalog) {
    const complete = data.summary.completed.includes(lab.id);
    const option = node('option', '', `${lab.id}. ${lab.title}${complete ? ' ✓' : ''}`); option.value = `/lab/${lab.id}`; $('lab-select').append(option);
  }
  const capstone = node('option', '', 'Capstone: Agentic Kill Chain'); capstone.value = '/killchain'; $('lab-select').append(capstone); $('lab-select').value = '/killchain';
  $('lab-select').addEventListener('change', (event) => location.assign(event.target.value));
  $('mobile-score').textContent = `${data.summary.score.toLocaleString()} / 2,100 pts`;
}

function renderHints() {
  $('kc-hints').replaceChildren(...HINTS.slice(0, kc.hintCount).map((text) => node('li', '', text)));
  $('kc-hint-button').textContent = kc.hintCount === HINTS.length ? 'Hide hints' : kc.hintCount ? 'Reveal another hint' : 'Reveal a hint';
}

function learnedCoupon() {
  for (const mail of kc.snapshot?.inbox || []) {
    if (mail.dataset === 'coupons') {
      const record = (mail.payload?.records || []).find((item) => item.internal);
      if (record) return record.code;
    }
  }
  return '';
}

function fillProducts() {
  const products = kc.snapshot.products;
  if (!products.some((item) => item.sku === kc.product)) kc.product = products[0]?.sku || '';
  for (const id of ['kc-product', 'kc-storefront-product']) {
    const selected = id === 'kc-product' ? kc.product : $(id).value || kc.product;
    $(id).replaceChildren();
    for (const product of products) { const option = node('option', '', `${product.name} · $${product.price}`); option.value = product.sku; $(id).append(option); }
    $(id).value = products.some((item) => item.sku === selected) ? selected : kc.product;
  }
}

function renderReviews() {
  const reviews = kc.snapshot.reviews.filter((review) => review.sku === kc.product);
  $('kc-review-count').textContent = `(${reviews.length})`;
  $('kc-reviews').replaceChildren();
  for (const review of reviews.slice(0, 6)) {
    const card = node('article', 'kc-record');
    card.append(node('h3', '', `${review.author} · ${review.rating}/5${review.seeded ? '' : ' · Yours'}`), node('p', '', review.text));
    if (!review.seeded || review.has_hidden_markup) card.append(disclosure('Raw stored record', review.raw));
    $('kc-reviews').append(card);
  }
  if (!reviews.length) $('kc-reviews').append(node('p', 'kc-empty', 'No reviews yet. Your review will appear here.'));
}

async function inspectAttachment(id) {
  try {
    const data = await api(`/api/killchain/attachments/${id}`);
    $('kc-evidence').replaceChildren(); $('kc-evidence').hidden = false;
    const card = node('article', 'kc-record');
    card.append(node('h3', '', `${data.filename} · ${data.summary.hidden_runs} hidden text runs`), node('p', '', data.summary.explanation));
    const columns = node('div', 'kc-evidence-columns');
    for (const [title, text] of [['Visible text', data.visible_text], ['Hidden text read by ingestion', data.hidden_text]]) {
      const column = node('div'); column.append(node('h4', '', title), code(text || '(none)')); columns.append(column);
    }
    card.append(columns, disclosure('All extracted spans and classifications', data.spans));
    $('kc-evidence').append(card);
  } catch (error) { notice(error.message, true); }
}

function renderTickets() {
  const selection = $('kc-ticket-target').value;
  $('kc-ticket-target').replaceChildren();
  const create = node('option', '', 'Create a new ticket'); create.value = 'new'; $('kc-ticket-target').append(create);
  for (const ticket of kc.snapshot.tickets.filter((item) => item.status === 'open' && !item.attachment)) {
    const option = node('option', '', `Ticket ${ticket.id} · ${ticket.subject}`); option.value = ticket.id; $('kc-ticket-target').append(option);
  }
  $('kc-ticket-target').value = [...$('kc-ticket-target').options].some((item) => item.value === selection) ? selection : 'new';
  $('kc-new-ticket-fields').hidden = $('kc-ticket-target').value !== 'new';
  $('kc-tickets').replaceChildren();
  for (const ticket of kc.snapshot.tickets.slice(0, 12)) {
    const card = node('article', 'kc-record');
    card.append(node('h3', '', `#${ticket.id} · ${ticket.subject}`), node('p', '', ticket.body), node('small', '', `${ticket.status} · ${ticket.today ? 'Today' : 'Earlier'}${ticket.seeded ? ' · Baseline ticket' : ''}`));
    if (ticket.attachment) {
      const actions = node('div', 'kc-actions');
      const inspect = node('button', 'secondary-button', 'Inspect PDF evidence'); inspect.type = 'button'; inspect.addEventListener('click', () => inspectAttachment(ticket.attachment.id));
      const download = node('a', '', 'Download PDF ↓'); download.href = `/api/killchain/attachments/${ticket.attachment.id}/file`; download.download = ticket.attachment.filename;
      actions.append(inspect, download); card.append(actions);
    }
    $('kc-tickets').append(card);
  }
}

function renderConversation() {
  $('kc-conversation').replaceChildren();
  const history = kc.snapshot.conversation || [];
  if (!history.length) $('kc-conversation').append(node('p', 'kc-empty', 'Plant an attack in the tools panel, then ask the agent an ordinary shop question. Follow what its memory makes it do.'));
  for (const message of history) {
    const wrapper = node('div', `message ${message.role === 'user' ? 'user' : 'assistant'}`);
    wrapper.append(node('span', 'speaker', message.role === 'user' ? 'YOU' : 'AGENTIC CRACKY'), node('div', 'bubble', message.content));
    $('kc-conversation').append(wrapper);
  }
  $('kc-conversation').scrollTop = $('kc-conversation').scrollHeight;
  $('kc-last-run').replaceChildren(); $('kc-last-run').hidden = !kc.lastRun;
  $('kc-run-details').hidden = !kc.lastRun;
  if (kc.lastRun) {
    $('kc-last-run').append(node('h3', '', `Last run: ${kc.lastRun.status.replaceAll('_', ' ')}`));
    if (kc.lastRun.note) $('kc-last-run').append(node('p', '', kc.lastRun.note));
    for (const step of kc.lastRun.steps || []) $('kc-last-run').append(disclosure(`${step.tool} · ${step.decision}`, { arguments: step.arguments, result: step.result }));
  }
  $('kc-approvals').replaceChildren();
  for (const approval of kc.snapshot.approvals.filter((item) => item.status === 'pending')) {
    const card = node('article', 'kc-approval');
    card.append(node('h3', '', `Approval required · ${approval.tool}`), node('p', '', approval.reason), code(approval.arguments));
    const actions = node('div');
    for (const decision of ['approve', 'reject']) {
      const button = node('button', decision === 'approve' ? 'primary-button' : 'secondary-button', decision === 'approve' ? 'Approve exact call' : 'Reject');
      button.type = 'button'; button.addEventListener('click', () => action('decision', async () => { kc.lastRun = await api(`/api/killchain/approvals/${approval.id}/decision`, { decision }); return kc.lastRun; }, 'Decision recorded. Read the execution trace to see what ran.', true));
      actions.append(button);
    }
    card.append(actions); $('kc-approvals').append(card);
  }
}

function renderMemory() {
  for (const store of ['connector', 'agent', 'cache']) $("kc-memory-" + store).setAttribute('aria-pressed', String(kc.memory === store));
  $('kc-memory-records').replaceChildren();
  const rows = kc.snapshot.memory[kc.memory] || [];
  if (!rows.length) $('kc-memory-records').append(node('p', 'kc-empty', `No ${kc.memory} records. Plant an attack to follow its persistence.`));
  for (const record of rows) {
    const card = node('article', 'kc-record');
    card.append(node('h3', '', `${kc.memory === 'connector' ? 'CM' : kc.memory === 'agent' ? 'AM' : 'Cache'}-${record.id}`));
    if (record.status) card.append(badge(record.status));
    card.append(node('p', '', record.content || record.extracted || ''));
    if (record.topics?.length) card.append(node('small', '', `Topics: ${record.topics.join(', ')}`));
    if (record.retrieval_count !== undefined) card.append(node('small', '', `Retrieved ${record.retrieval_count} time(s)`));
    card.append(disclosure('Provenance and full record', record)); $('kc-memory-records').append(card);
  }
}

function renderEvents() {
  const pane = $('kc-events');
  $('kc-event-count').textContent = `${kc.snapshot.events.length} events`;
  const atBottom = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 35;
  pane.replaceChildren();
  for (const event of kc.snapshot.events || []) {
    const details = node('details', 'kc-event');
    const summary = node('summary'); const heading = node('div', 'kc-event-head');
    heading.append(node('span', '', `${event.kind} · ${event.op_id}`), badge(event.status));
    summary.append(heading, node('span', 'kc-event-title', event.title));
    details.append(summary, code({ detail: event.detail, references: event.refs, at: event.created_at }));
    pane.append(details);
  }
  if (!kc.snapshot.events.length) pane.append(node('p', 'kc-empty', 'The trace starts when you submit a source or run the agent.'));
  if (atBottom || kc.busy) pane.scrollTop = pane.scrollHeight;
}

function renderImpact() {
  $('kc-inbox').replaceChildren();
  $('kc-attacker-address').textContent = `${kc.snapshot.attacker_address} · simulated mail only`;
  if (!kc.snapshot.inbox.length) $('kc-inbox').append(node('p', 'kc-empty', 'The attacker inbox is empty. A successful export tool call will add a real lab record here.'));
  for (const mail of kc.snapshot.inbox) {
    const card = node('article', 'kc-record');
    card.append(node('h3', '', `${mail.dataset} export · ${mail.payload?.records?.length ?? 0} record(s)`), node('p', '', `To: ${mail.recipient}\nBCC: ${(Array.isArray(mail.bcc) ? mail.bcc : [mail.bcc]).filter(Boolean).join(', ')}`));
    if (mail.categories?.length) card.append(node('small', '', mail.categories.join(' · ')));
    card.append(disclosure('Open exported payload', mail.payload), disclosure('Mail record and execution', mail)); $('kc-inbox').append(card);
  }
  $('kc-checkouts').replaceChildren();
  if (!kc.snapshot.checkouts.length) $('kc-checkouts').append(node('p', 'kc-empty', 'No agent checkout quotes yet. Try the price quick action after planting a pricing procedure.'));
  for (const checkout of kc.snapshot.checkouts) {
    const card = node('article', 'kc-record');
    card.append(node('h3', '', checkout.product), node('p', '', `List price $${checkout.list_price} → Agent quote $${checkout.final_price}`), node('small', '', `Coupon: ${checkout.coupon_code || 'none'}`), disclosure('Checkout record', checkout)); $('kc-checkouts').append(card);
  }
  $('kc-storefront-result').replaceChildren(); $('kc-storefront-result').hidden = !kc.storefront;
  if (kc.storefront) $('kc-storefront-result').append(code(kc.storefront));
}

function controls() {
  for (const input of document.querySelectorAll('main button:not([data-local-control]), main input, main textarea, main select')) input.disabled = Boolean(kc.busy) || !kc.snapshot;
  $('kc-mode').disabled = Boolean(kc.busy) || !kc.snapshot;
  const pending = kc.snapshot?.approvals.some((item) => item.status === 'pending');
  $('kc-goal').disabled = Boolean(kc.busy) || Boolean(pending) || !kc.snapshot;
  $('kc-send').disabled = Boolean(kc.busy) || Boolean(pending) || !kc.snapshot;
  $('kc-agent-help').textContent = pending ? 'Approve or reject the pending tool call to continue.' : 'Enter to send · Shift + Enter for a new line';
  $('kc-working').hidden = !['turn', 'decision'].includes(kc.busy);
}

function render() {
  const state = kc.snapshot;
  if (!state) return;
  $('kc-mode').value = state.mode;
  $('kc-overall').textContent = state.overall.replaceAll('_', ' ');
  $('kc-overall').className = `lab-status${state.overall === 'compromised' ? ' captured' : state.overall === 'baseline' ? '' : ' complete'}`;
  $('kc-memory-count').textContent = `${state.memory.connector.length + state.memory.agent.length} notes`;
  $('kc-event-count').textContent = `${state.events.length} events`;
  $('kc-mail-count').textContent = `${state.inbox.length} messages`;
  $('kc-checkout-count').textContent = `${state.checkouts.length} quotes`;
  $('kc-mode-description').textContent = state.mode === 'vulnerable' ? 'Vulnerable: hidden instructions can persist and sensitive tools run without approval.' : state.mode === 'defended' ? 'Defended: sensitive tool calls wait for your approval. Switching mode preserves both memory stores.' : 'Guardrailed: new malicious notes are quarantined. Sensitive calls still require approval, then deterministic rails can block an unsafe call even after you approve it.';
  $('kc-stats').replaceChildren();
  for (const [key, title] of [['connector_memory','Connector notes'],['agent_memory','Agent notes'],['quarantined','Quarantined'],['pending_approvals','Awaiting approval'],['exfiltration','Simulated exports'],['coupon_abuse','Coupon abuse']]) {
    const item = node('div', 'kc-stat'); item.append(node('strong', '', state.status[key] ?? 0), node('span', '', title)); $('kc-stats').append(item);
  }
  $('kc-rails').hidden = state.mode !== 'guardrailed'; $('kc-rails-list').replaceChildren();
  for (const rail of state.rails || []) { const row = node('article', 'kc-record'); row.append(node('h3', '', `${rail.id} · ${rail.stage}`), node('p', '', rail.summary)); if (rail.note) row.append(node('small', '', rail.note)); $('kc-rails-list').append(row); }
  fillProducts(); renderReviews(); renderTickets(); renderConversation(); renderMemory(); renderEvents(); renderImpact();
  const learned = learnedCoupon();
  $('kc-coupon-knowledge').textContent = learned ? `Coupon learned from the attacker inbox: ${learned}. The pricing example now uses it.` : 'The pricing example keeps <COUPON_CODE> until a coupon export reaches the attacker inbox.';
  controls();
}

async function refresh() {
  kc.snapshot = await api('/api/killchain/state');
  render();
}

async function pollEvents() {
  if (polling || !kc.snapshot) return;
  polling = true;
  try {
    const after = Math.max(0, ...kc.snapshot.events.map((event) => event.id));
    const result = await api(`/api/killchain/events?after=${after}`);
    const known = new Set(kc.snapshot.events.map((event) => event.id));
    kc.snapshot.events.push(...result.events.filter((event) => !known.has(event.id)));
    if (result.events.length) renderEvents();
  } catch { /* The final state refresh reports any connection failure. */ }
  finally { polling = false; }
}

async function action(label, work, success, live = false) {
  if (kc.busy) return;
  kc.busy = label; notice(''); notice('', true); controls();
  if (live) pollTimer = setInterval(pollEvents, 2000);
  let result;
  try {
    result = await work();
    if (success) notice(typeof success === 'function' ? success(result) : success);
  } catch (error) { notice(error.message, true); }
  finally {
    clearInterval(pollTimer);
    try { await refresh(); } catch (error) { notice(error.message, true); }
    kc.busy = ''; controls();
  }
  return result;
}

function setupExamples() {
  $('kc-presets').replaceChildren(); $('kc-quick-actions').replaceChildren();
  for (const procedure of kc.examples.procedures) {
    const button = node('button', '', procedure.label); button.type = 'button';
    button.addEventListener('click', () => {
      $('kc-hidden').value = procedure.text.split(kc.examples.coupon_placeholder).join(learnedCoupon() || kc.examples.coupon_placeholder);
      if (!$('kc-review-text').value.trim()) $('kc-review-text').value = 'Good quality and quick delivery.';
      $('kc-hidden').focus();
    });
    $('kc-presets').append(button);
  }
  for (const quick of kc.examples.quick_actions) {
    const button = node('button', '', quick.label); button.type = 'button';
    button.addEventListener('click', () => {
      const product = kc.snapshot.products.find((item) => item.sku === kc.product);
      $('kc-goal').value = quick.id === 'checkout_price' && product ? `Check the current checkout price for ${product.name} (${product.sku}).` : quick.prompt;
      $('kc-goal').focus();
    });
    $('kc-quick-actions').append(button);
  }
}

async function health() {
  try {
    const result = await api('/api/health'); $('ai-status').className = `ai-status ${result.ai_ready ? 'online' : 'offline'}`; $('ai-status-text').textContent = result.ai_ready ? 'Local AI ready' : 'Local AI unavailable';
  } catch { $('ai-status').className = 'ai-status offline'; $('ai-status-text').textContent = 'Connection lost'; }
}

async function start() {
  window.LabScenario?.render('killchain');
  $('kc-hint-button').addEventListener('click', () => { kc.hintCount = (kc.hintCount + 1) % (HINTS.length + 1); renderHints(); });
  renderHints();
  controls();
  try {
    // Establish one browser identity before the parallel capstone reads.
    setupNavigation(await api('/api/state'));
    const [snapshot, examples, models] = await Promise.all([api('/api/killchain/state'), api('/api/killchain/examples'), api('/api/killchain/models')]);
    kc.snapshot = snapshot; kc.examples = examples;
    const defaultOption = node('option', '', `Default (${models.default})`); defaultOption.value = ''; $('kc-model').replaceChildren(defaultOption);
    for (const model of models.models) { const option = node('option', '', model); option.value = model; $('kc-model').append(option); }
    setupExamples(); render();
    for (const [id, source] of [['kc-review-tab','review'],['kc-ticket-tab','ticket']]) $(id).addEventListener('click', () => {
      kc.source = source; $('kc-review-source').hidden = source !== 'review'; $('kc-ticket-source').hidden = source !== 'ticket'; $('kc-review-tab').setAttribute('aria-pressed', String(source === 'review')); $('kc-ticket-tab').setAttribute('aria-pressed', String(source === 'ticket'));
    });
    $('kc-model').addEventListener('change', () => { kc.model = $('kc-model').value; });
    $('kc-mode').addEventListener('change', () => action('mode', () => api('/api/killchain/mode', { mode: $('kc-mode').value }), 'Defense mode changed. Both memory stores were preserved.'));
    $('kc-product').addEventListener('change', () => { kc.product = $('kc-product').value; renderReviews(); });
    $('kc-ticket-target').addEventListener('change', () => { $('kc-new-ticket-fields').hidden = $('kc-ticket-target').value !== 'new'; });
    $('kc-review-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const result = await action('review', () => api('/api/killchain/reviews', { product: kc.product, rating: Number($('kc-rating').value), text: $('kc-review-text').value.trim(), hidden: $('kc-hidden').value.trim() }), (out) => out.quarantined ? 'Review stored. The hidden instruction was quarantined at ingestion.' : out.connector_memory_id ? 'Review stored. Inspect connector and agent memory to follow the hidden instruction.' : 'Ordinary review stored. No hidden instruction entered memory.');
      if (result) { $('kc-review-text').value = ''; $('kc-hidden').value = ''; }
    });
    $('kc-ticket-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const target = $('kc-ticket-target').value; const file = $('kc-invoice').files[0];
      if (file && file.size > 256 * 1024) return notice('Choose a PDF up to 256 KB.', true);
      if (target !== 'new' && !file) return notice('Choose a PDF to attach to the ticket.', true);
      const form = new FormData();
      if (file) form.append('file', file);
      if (target === 'new') { form.append('subject', $('kc-ticket-subject').value); form.append('body', $('kc-ticket-body').value); }
      const result = await action('ticket', () => api(target === 'new' ? '/api/killchain/tickets' : `/api/killchain/tickets/${target}/attachment`, form), 'Ticket and attachment saved. Inspect PDF evidence and both memory stores.');
      if (result) $('kc-invoice').value = '';
    });
    const send = async (event) => {
      event.preventDefault(); const text = $('kc-goal').value.trim(); if (!text || kc.busy) return;
      const result = await action('turn', async () => { kc.lastRun = await api('/api/killchain/turn', { message: text, model: kc.model || null }); return kc.lastRun; }, null, true);
      if (result) $('kc-goal').value = '';
    };
    $('kc-agent-form').addEventListener('submit', send);
    $('kc-goal').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!$('kc-send').disabled) $('kc-agent-form').requestSubmit(); } });
    for (const store of ['connector','agent','cache']) $('kc-memory-' + store).addEventListener('click', () => { kc.memory = store; renderMemory(); });
    for (const [id, kind, message] of [
      ['kc-clear-agent','agent_memory','Agent memory cleared. The connector still holds the source and can rebuild it on the next request.'],
      ['kc-clear-cache','connector_cache','Connector cache cleared. Both persistent memory stores remain.'],
      ['kc-soft-reset','soft_reset','Conversation and transient state cleared. Inspect the persistent memories that survived.'],
      ['kc-hard-reset','hard_reset','Capstone restored to its original baseline. The nine CTF labs were preserved.'],
    ]) $(id).addEventListener('click', () => action(kind, async () => { const result = await api(`/api/killchain/cleanup/${kind}`, {}); if (['soft_reset','hard_reset'].includes(kind)) kc.lastRun = null; if (kind === 'hard_reset') { kc.storefront = null; $('kc-evidence').hidden = true; } return result; }, message));
    $('kc-storefront-form').addEventListener('submit', (event) => { event.preventDefault(); action('storefront', async () => { kc.storefront = await api('/api/killchain/storefront/coupon', { product: $('kc-storefront-product').value, code: $('kc-storefront-code').value.trim() }); return kc.storefront; }); });
    health(); setInterval(health, 45000);
  } catch (error) {
    notice(error.message, true);
    const retry = node('button', 'secondary-button', 'Reload capstone'); retry.type = 'button'; retry.addEventListener('click', () => location.reload()); $('kc-error').append(document.createTextNode(' '), retry); retry.disabled = false;
  }
}

start();
