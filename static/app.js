'use strict';

const $ = (id) => document.getElementById(id);
const state = { catalog: [], labs: {}, summary: { completed: [], score: 0 }, current: 1,
  pending: new Map(), kbBusy: new Set(), flagBusy: new Set(), resetBusy: new Set(),
  promptDrafts: new Map(), flagDrafts: new Map(), editors: new Map(), hintCounts: new Map(),
  kbNotices: new Map(), flagFeedback: new Map(), defenseBusy: new Set() };
let errorTimer;
let boilerPanel;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function currentLab() { return state.labs[String(state.current)]; }
function metadata(id = state.current) { return state.catalog[id - 1]; }
function routeId() { return Number(location.pathname.match(/^\/lab\/([1-9])\/?$/)?.[1] || 1); }

async function api(path, body) {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Could not reach the lab. Check the connection and try again.'); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error('The lab returned an unexpected response. Please reload and try again.'); }
  if (!response.ok) throw new Error(result.error || 'This request could not be completed.');
  return result;
}

function showError(message) {
  clearTimeout(errorTimer);
  $('error').textContent = message;
  $('error').hidden = false;
  errorTimer = setTimeout(() => { $('error').hidden = true; }, 16000);
}

function isOrdinaryClick(event) { return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey; }
function drawNavigation() {
  $('lab-select').replaceChildren();
  for (const item of state.catalog) {
    const completed = state.summary.completed.includes(item.id);
    const option = node('option', '', `${item.id}. ${item.title}${completed ? ' ✓' : ''}`);
    option.value = item.id;
    $('lab-select').append(option);
  }
  const capstoneOption = node('option', '', 'Capstone: Agentic Kill Chain');
  capstoneOption.value = 'killchain';
  $('lab-select').append(capstoneOption);
  $('lab-select').value = state.current;
  $('mobile-score').textContent = `${state.summary.score.toLocaleString()} / 2,100 pts`;
  for (const [id, target] of [['previous-lab', state.current - 1], ['next-lab', state.current + 1]]) {
    const enabled = target >= 1 && target <= 10;
    $(id).setAttribute('aria-disabled', String(!enabled));
    if (enabled) $(id).href = target === 10 ? '/killchain' : `/lab/${target}`;
    else $(id).removeAttribute('href');
  }
}

function appendMessage(role, content, pending = false) {
  const wrapper = node('div', `message ${role}${pending ? ' pending' : ''}`);
  wrapper.append(node('span', 'speaker', role === 'user' ? 'YOU' : 'CRACKY'));
  const bubble = node('div', 'bubble');
  if (pending) {
    const dots = node('span', 'thinking-dots');
    dots.setAttribute('aria-hidden', 'true');
    dots.append(node('i'), node('i'), node('i'));
    bubble.append(dots, node('span', '', 'Thinking'), node('span', 'thinking-time', '0s'));
  } else bubble.textContent = content;
  wrapper.append(bubble);
  $('messages').append(wrapper);
}

function drawConversation() {
  const record = currentLab();
  const pending = state.pending.get(state.current);
  $('messages').replaceChildren();
  if (!record.messages.length && !pending) {
    const empty = node('div', 'empty-chat');
    empty.append(node('h3', '', 'Start a conversation.'), node('p', '', metadata().has_kb ? 'Plant a document in the knowledge base, then ask Cracky about it.' : 'Ask Cracky a question, or try your first attack. Use the hints in the scenario panel.'));
    $('messages').append(empty);
  }
  for (const message of record.messages) appendMessage(message.role, message.content);
  if (pending) { appendMessage('user', pending.message); appendMessage('assistant', '', true); }
  $('messages').scrollTop = $('messages').scrollHeight;
  $('turn-count').textContent = `${Math.floor(record.messages.length / 2)} turns`;
  $('attempt-result').replaceChildren();
  $('attempt-result').hidden = !record.last_result || Boolean(pending);
  if (record.last_result && !pending) {
    const result = record.last_result;
    $('attempt-result').className = `attempt-result${record.exploited ? ' success' : result.defense_blocked ? ' blocked' : ''}`;
    $('attempt-result').append(node('p', '', result.message));
    const details = node('details');
    details.append(node('summary', '', `Attempt details · ${result.seconds}s`));
    const lines = node('ul');
    if (result.defense_blocked) lines.append(node('li', '', result.defense_reason || 'The selected defenses blocked this attempt.'));
    else lines.append(node('li', '', result.triggered ? 'Exploit verified.' : 'No exploit detected.'));
    if (metadata().has_kb) {
      lines.append(node('li', '', result.kb_used ? `${result.sources.length} synced document(s) were retrieved.` : 'No knowledge-base documents were used.'));
      for (const source of result.sources) lines.append(node('li', '', `${source.title} · ${source.injected ? 'Your document' : 'Original reference'}`));
    }
    details.append(lines);
    $('attempt-result').append(details);
  }
}

function drawHints() {
  const count = state.hintCounts.get(state.current) || 0;
  const hints = metadata().hints;
  $('hints').replaceChildren();
  for (const hint of hints.slice(0, count)) $('hints').append(node('li', '', hint));
  const label = count === 0 ? 'Reveal a hint' : count < hints.length ? 'Reveal another hint' : 'Hide hints';
  $('hint-button').replaceChildren(document.createTextNode(label), node('span', '', count === hints.length ? '−' : '+'));
  $('hint-button').setAttribute('aria-expanded', String(count > 0));
  $('hint-button').setAttribute('aria-controls', 'hints');
}

function drawFlag() {
  const record = currentLab();
  $('flag-button').textContent = record.completed ? '✓ Completed' : record.exploited ? 'Flag captured' : 'Submit flag';
  $('flag-button').className = `tool-button${record.exploited ? ' captured' : ''}`;
  $('flag-heading').textContent = record.completed ? 'Lab complete.' : record.exploited ? 'You found the flag.' : 'Find it. Submit it.';
  $('flag-instructions').textContent = record.completed ? `${metadata().points} points earned. You can keep experimenting or move to the next lab.` : record.exploited ? 'The server verified the exploit. Use the captured flag below to claim your points.' : 'Trigger the exploit in chat to reveal this lab’s flag. Submit it here to earn the points.';
  $('captured-panel').hidden = !record.exploited;
  $('captured-flag').textContent = record.flag || '';
  $('use-captured-flag').hidden = record.completed;
  $('flag-form').hidden = record.completed;
  $('flag-input').value = state.flagDrafts.get(state.current) || '';
  $('award-points').textContent = `+${metadata().points}`;
  const feedback = state.flagFeedback.get(state.current);
  $('flag-feedback').hidden = !feedback;
  $('flag-feedback').textContent = feedback?.message || '';
  $('flag-feedback').className = `flag-feedback${feedback?.error ? ' error-text' : ''}`;
}

function drawKnowledge() {
  const record = currentLab();
  $('knowledge-button').hidden = !metadata().has_kb;
  $('knowledge-button').textContent = record.kb_revision !== record.synced_revision ? 'Knowledge base · Unsynced' : record.use_kb ? 'Knowledge base · On' : 'Knowledge base';
  $('knowledge-panel').hidden = !metadata().has_kb;
  if (!metadata().has_kb) return;
  const changed = record.kb_revision !== record.synced_revision;
  $('kb-status').textContent = changed ? 'Changes waiting to sync' : 'Up to date';
  $('kb-status').className = `kb-status${changed ? ' unsynced' : ''}`;
  $('use-kb').checked = record.use_kb;
  $('document-list').replaceChildren();
  for (const doc of record.documents) {
    const row = node('div', 'document-row');
    const heading = node('div');
    heading.append(node('h3', '', doc.title), node('span', 'document-tag', doc.is_user_injected ? 'Your document' : 'Original'));
    row.append(heading, node('small', '', `${doc.content.length.toLocaleString()} characters`));
    const actions = node('div', 'document-actions');
    const edit = node('button', '', 'Edit');
    edit.type = 'button'; edit.setAttribute('aria-label', `Edit ${doc.title}`);
    edit.addEventListener('click', () => { state.editors.set(state.current, { id: doc.id, title: doc.title, content: doc.content }); drawKnowledge(); updateControls(); $('document-title').focus(); });
    const remove = node('button', '', 'Delete');
    remove.type = 'button'; remove.setAttribute('aria-label', `Delete ${doc.title}`);
    remove.addEventListener('click', () => updateKnowledge({ documents: currentLab().documents.filter((item) => item.id !== doc.id), notice: 'Document removed. Sync to update the assistant’s references.' }));
    actions.append(edit, remove); row.append(actions); $('document-list').append(row);
  }
  const draft = state.editors.get(state.current);
  $('document-editor').hidden = !draft;
  $('editor-placeholder').hidden = Boolean(draft);
  if (draft) { $('document-title').value = draft.title; $('document-content').value = draft.content; }
  $('kb-notice').textContent = state.kbNotices.get(state.current) || (changed ? 'Saved edits are waiting to be synced.' : record.use_kb ? 'Synced documents are enabled for the next message.' : 'The original references are synced. Retrieval is currently off.');
}

function updateControls() {
  const record = currentLab();
  const ownPending = state.pending.has(state.current);
  const anyPending = state.pending.size > 0;
  const changingDefense = state.defenseBusy.has(state.current);
  $('send').disabled = anyPending || changingDefense;
  $('send').replaceChildren(document.createTextNode(ownPending ? 'Thinking…' : anyPending ? 'AI busy' : 'Send '));
  if (!anyPending) $('send').append(node('span', '', '↗'));
  $('prompt').disabled = ownPending;
  $('composer-help').textContent = anyPending && !ownPending ? 'The AI is replying in another lab. You can still explore.' : 'Enter to send · Shift + Enter for a new line';
  $('restart-lab').disabled = ownPending || state.resetBusy.has(state.current) || changingDefense;
  $('defense-select').disabled = ownPending || changingDefense || state.resetBusy.has(state.current);
  $('flag-input').disabled = !record.exploited || state.flagBusy.has(state.current);
  $('submit-flag').disabled = !record.exploited || state.flagBusy.has(state.current);
  $('use-captured-flag').disabled = state.flagBusy.has(state.current);
  const kbBusy = state.kbBusy.has(state.current);
  for (const input of $('document-editor').querySelectorAll('input, textarea, button')) input.disabled = kbBusy;
  for (const button of $('document-list').querySelectorAll('button')) button.disabled = kbBusy;
  $('new-document').disabled = kbBusy || Boolean(state.editors.get(state.current));
  $('use-kb').disabled = kbBusy;
  $('sync-kb').disabled = kbBusy || Boolean(state.editors.get(state.current));
  $('sync-kb').title = state.editors.has(state.current) ? 'Save or cancel the document before syncing.' : 'Sync saved documents and enable them for this lab.';
}

function drawWorkspace() {
  const target = $('workspace-content');
  $('workspace-label').textContent = `Lab ${String(state.current).padStart(2, '0')}`;
  if (state.current === 1) {
    if (!boilerPanel) {
      boilerPanel = document.createElement('boiler-panel');
      boilerPanel.id = 'boiler-panel';
      boilerPanel.setAttribute('embedded', '');
    }
    if (boilerPanel.parentElement !== target) target.replaceChildren(boilerPanel);
    boilerPanel.setContext({ labId: 1, defense: currentLab().defense_level || 0 });
  } else if (!target.querySelector('.workspace-empty')) {
    const placeholder = node('div', 'workspace-empty');
    const icon = node('span', 'workspace-empty-icon', '◇');
    icon.setAttribute('aria-hidden', 'true');
    placeholder.append(icon, node('p', '', 'No workspace added yet.'));
    target.replaceChildren(placeholder);
  }
}

function drawLab() {
  const item = metadata();
  const record = currentLab();
  document.title = `${item.title} — Nine Labs`;
  $('lab-counter').textContent = `LAB ${String(item.id).padStart(2, '0')} OF 09`;
  $('lab-title').textContent = item.title;
  $('lab-description').textContent = item.description;
  $('difficulty').textContent = item.difficulty;
  $('difficulty').className = `difficulty ${item.difficulty}`;
  $('owasp').textContent = item.owasp_ref;
  $('points').textContent = `${item.points} points`;
  $('defense-select').value = String(record.defense_level || 0);
  $('chat-context').textContent = state.current === 1 ? 'Boiler monitoring assistant' : ['Vulnerable assistant', 'Hardened assistant', 'Guardrailed assistant'][record.defense_level || 0];
  $('lab-status').textContent = record.completed ? '✓ Completed' : record.exploited ? 'Flag captured' : record.messages.length ? 'In progress' : 'Ready to try';
  $('lab-status').className = `lab-status${record.completed ? ' complete' : record.exploited ? ' captured' : ''}`;
  $('prompt').value = state.promptDrafts.get(state.current) || '';
  window.LabScenario?.render(state.current);
  drawWorkspace();
  drawNavigation(); drawConversation(); drawFlag(); drawHints(); drawKnowledge(); updateControls();
}

function navigate(id, push = true) {
  if (!Number.isInteger(id) || id < 1 || id > 9) return;
  state.promptDrafts.set(state.current, $('prompt').value);
  state.flagDrafts.set(state.current, $('flag-input').value);
  state.current = id;
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  if (push && location.pathname !== `/lab/${id}`) history.pushState(null, '', `/lab/${id}`);
  $('error').hidden = true;
  drawLab();
  $('lab-title').focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: 'instant' });
}

function acceptResponse(id, response) {
  const justCaptured = !state.labs[String(id)].exploited && response.lab.exploited;
  state.labs[String(id)] = response.lab;
  state.summary = response.summary;
  if (state.current === id) {
    drawLab();
    if (justCaptured && !document.querySelector('dialog[open]')) $('flag-dialog').showModal();
  }
  else { drawNavigation(); updateControls(); }
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.pending.size) return;
  const id = state.current;
  const message = $('prompt').value.trim();
  if (!message) return;
  state.pending.set(id, { message, started: Date.now() });
  state.promptDrafts.set(id, '');
  $('prompt').value = '';
  $('error').hidden = true;
  drawConversation(); updateControls();
  try {
    const response = await api(`/api/labs/${id}/chat`, { message, use_kb: state.labs[String(id)].use_kb });
    state.pending.delete(id);
    acceptResponse(id, response);
  } catch (error) {
    state.pending.delete(id);
    state.promptDrafts.set(id, message);
    if (state.current === id) drawLab();
    showError(`Lab ${id}: ${error.message}`);
  } finally {
    updateControls();
    if (state.current === id) $('prompt').focus({ preventScroll: true });
  }
}

async function updateKnowledge({ documents, sync = false, useKB, notice, clearEditor = false }) {
  const id = state.current;
  if (state.kbBusy.has(id)) return;
  state.kbBusy.add(id); updateControls();
  try {
    const record = state.labs[String(id)];
    const response = await api(`/api/labs/${id}/kb`, { documents: documents || record.documents, sync, use_kb: useKB === undefined ? record.use_kb : useKB });
    if (clearEditor) state.editors.delete(id);
    if (notice) state.kbNotices.set(id, notice);
    acceptResponse(id, response);
  } catch (error) {
    if (state.current === id) $('use-kb').checked = currentLab().use_kb;
    showError(`Lab ${id}: ${error.message}`);
  } finally { state.kbBusy.delete(id); updateControls(); }
}

async function submitFlag(event) {
  event.preventDefault();
  const id = state.current;
  if (state.flagBusy.has(id)) return;
  const flag = $('flag-input').value.trim();
  state.flagDrafts.set(id, flag);
  state.flagBusy.add(id); updateControls();
  try {
    const response = await api(`/api/labs/${id}/flag`, { flag });
    state.flagFeedback.set(id, { message: `+${metadata(id).points} points. Well played.` });
    acceptResponse(id, response);
  } catch (error) {
    state.flagFeedback.set(id, { message: error.message, error: true });
    if (state.current === id) drawFlag();
  } finally { state.flagBusy.delete(id); updateControls(); }
}

async function restartLab() {
  const id = state.current;
  if (state.pending.has(id) || state.resetBusy.has(id)) return;
  state.resetBusy.add(id); updateControls();
  try {
    const response = await api(`/api/labs/${id}/reset`, {});
    boilerPanel?.resetSimulation(id);
    for (const map of [state.promptDrafts, state.flagDrafts, state.editors, state.hintCounts, state.kbNotices, state.flagFeedback]) map.delete(id);
    acceptResponse(id, response);
  } catch (error) { showError(`Lab ${id}: ${error.message}`); }
  finally { state.resetBusy.delete(id); updateControls(); }
}

async function changeDefense() {
  const id = state.current;
  const level = Number($('defense-select').value);
  const record = currentLab();
  if (level === (record.defense_level || 0)) return;
  if ((record.messages.length || record.completed || record.exploited) && !window.confirm('Changing difficulty starts a new attempt and clears this lab’s chat, captured flag, and points. Your knowledge base stays saved. Continue?')) {
    $('defense-select').value = String(record.defense_level || 0);
    return;
  }
  state.defenseBusy.add(id); updateControls();
  try {
    const response = await api(`/api/labs/${id}/defense`, { level });
    for (const map of [state.promptDrafts, state.flagDrafts, state.flagFeedback]) map.delete(id);
    acceptResponse(id, response);
  } catch (error) {
    if (state.current === id) $('defense-select').value = String(currentLab().defense_level || 0);
    showError(error.message);
  } finally { state.defenseBusy.delete(id); updateControls(); }
}

async function checkHealth() {
  try {
    const health = await api('/api/health');
    $('ai-status').className = `ai-status ${health.ai_ready ? 'online' : 'offline'}`;
    $('ai-status-text').textContent = health.ai_ready ? 'Local AI ready' : 'Local AI unavailable';
    $('ai-status').title = health.ai_ready ? `Running on this machine with ${health.model}` : 'The app is running, but its local model is unavailable.';
  } catch { $('ai-status').className = 'ai-status offline'; $('ai-status-text').textContent = 'Connection lost'; }
}

async function start() {
  try {
    const data = await api('/api/state');
    state.catalog = data.catalog; state.labs = data.labs; state.summary = data.summary;
    state.current = routeId();
    if (location.pathname === '/') history.replaceState(null, '', '/lab/1');
    drawLab();
    $('lab-select').addEventListener('change', (event) => event.target.value === 'killchain' ? location.assign('/killchain') : navigate(Number(event.target.value)));
    for (const [id, direction] of [['previous-lab', -1], ['next-lab', 1]]) $(id).addEventListener('click', (event) => {
      if (isOrdinaryClick(event)) { event.preventDefault(); if (state.current === 9 && direction === 1) location.assign('/killchain'); else navigate(state.current + direction); }
    });
    window.addEventListener('popstate', () => navigate(routeId(), false));
    $('chat-form').addEventListener('submit', sendMessage);
    $('prompt').addEventListener('input', () => state.promptDrafts.set(state.current, $('prompt').value));
    $('prompt').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); } });
    $('flag-form').addEventListener('submit', submitFlag);
    $('flag-input').addEventListener('input', () => state.flagDrafts.set(state.current, $('flag-input').value));
    $('use-captured-flag').addEventListener('click', () => { $('flag-input').value = currentLab().flag || ''; state.flagDrafts.set(state.current, $('flag-input').value); $('flag-input').focus(); });
    $('hint-button').addEventListener('click', () => { state.hintCounts.set(state.current, ((state.hintCounts.get(state.current) || 0) + 1) % (metadata().hints.length + 1)); drawHints(); });
    $('restart-lab').addEventListener('click', restartLab);
    $('defense-select').addEventListener('change', changeDefense);
    $('flag-button').addEventListener('click', () => $('flag-dialog').showModal());
    $('knowledge-button').addEventListener('click', () => $('knowledge-dialog').showModal());
    for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $(button.dataset.close).close());
    for (const dialog of document.querySelectorAll('.tool-dialog')) dialog.addEventListener('click', (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
    $('new-document').addEventListener('click', () => { state.editors.set(state.current, { title: '', content: '' }); drawKnowledge(); updateControls(); $('document-title').focus(); });
    $('cancel-document').addEventListener('click', () => { state.editors.delete(state.current); drawKnowledge(); updateControls(); });
    $('document-title').addEventListener('input', () => { state.editors.get(state.current).title = $('document-title').value; });
    $('document-content').addEventListener('input', () => { state.editors.get(state.current).content = $('document-content').value; });
    $('document-editor').addEventListener('submit', (event) => {
      event.preventDefault();
      const draft = state.editors.get(state.current);
      if (!draft) return;
      const docs = currentLab().documents.filter((item) => !draft.id || item.id !== draft.id);
      docs.push({ ...draft });
      updateKnowledge({ documents: docs, notice: 'Document saved. Sync knowledge base to use your edits.', clearEditor: true });
    });
    $('sync-kb').addEventListener('click', () => updateKnowledge({ sync: true, useKB: true, notice: 'Synced and enabled. Ask about your document’s topic in the chat.' }));
    $('use-kb').addEventListener('change', () => updateKnowledge({ useKB: $('use-kb').checked, notice: $('use-kb').checked ? 'Knowledge base enabled for the next message.' : 'Knowledge base disabled for the next message.' }));
    checkHealth(); setInterval(checkHealth, 45000);
    setInterval(() => {
      const pending = state.pending.get(state.current);
      const time = document.querySelector('.thinking-time');
      if (pending && time) { const seconds = Math.floor((Date.now() - pending.started) / 1000); time.textContent = `${seconds}s${seconds > 25 ? ' · local AI can take a moment' : ''}`; }
    }, 1000);
  } catch (error) { showError('The labs could not load. ' + error.message); $('send').disabled = true; }
}

start();
