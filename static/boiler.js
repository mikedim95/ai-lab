'use strict';

/* A view of the session's classroom simulation. It does not interpret model
   replies or change CTF results. Readings and inputs are saved by the backend. */
(() => {
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  class BoilerPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._context = { labId: '1', defense: '0' };
      this._state = null;
      this._editing = false;
      this._alertDrafts = new Map();
      this._alertForms = new Map();
      this._alertSaving = false;
      this.shadowRoot.innerHTML = `
        <link rel="stylesheet" href="/boiler-panel.css">
        <section class="panel" aria-label="Boiler classroom simulation">
          <header class="heading"><div><span class="context" data-context>Lab 01 · Easy</span><h2>Boiler overview</h2></div><span class="badge">Classroom simulation</span></header>
          <div class="diagram">
            <svg viewBox="0 0 520 310" role="img" aria-label="Water and fuel feed a boiler. A temperature sensor and pressure gauge monitor the vessel. Steam drives a generator to produce power.">
              <defs>
                <pattern id="grid" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="#dfeae5"/></pattern>
                <clipPath id="vessel"><rect x="190" y="101" width="130" height="143" rx="26"/></clipPath>
              </defs>
              <path fill="url(#grid)" d="M0 0h520v310H0z"/>
              <text x="24" y="88" class="strong">WATER IN</text><text x="24" y="105" class="small">Feedwater</text>
              <path class="pipe-base" d="M35 126H134V160H190"/><path class="pipe water-pipe" data-water-flow d="M35 126H134V160H190"/>
              <circle cx="80" cy="126" r="10" fill="#fff" stroke="#b1d5df" stroke-width="2"/><path d="m76 121 7 5-7 5" fill="none" stroke="#64a6bc" stroke-width="2"/>
              <text x="24" y="214" class="strong">FUEL IN</text><text x="24" y="231" class="small">Burner demand</text>
              <path class="pipe-base" d="M35 251H180V271H227"/><path class="pipe fuel-pipe" data-fuel-flow d="M35 251H180V271H227"/>
              <circle cx="80" cy="251" r="10" fill="#fff" stroke="#e3c6a7" stroke-width="2"/><path d="m76 246 7 5-7 5" fill="none" stroke="#c78d4e" stroke-width="2"/>
              <path class="pipe-base" d="M255 101V59H414V106"/><path class="pipe steam-pipe" data-steam-flow d="M255 101V59H414V106"/>
              <text x="262" y="36" class="small">STEAM OUT</text>
              <path d="M330 59V45" stroke="#9cb8ab" stroke-width="3"/>
              <circle cx="330" cy="59" r="21" fill="#fff" stroke="#b4cbbf" stroke-width="2"/><path d="M316 62a14 14 0 0 1 28 0" stroke="#d4e1d8" fill="none" stroke-width="4"/>
              <path class="needle" data-needle d="M330 59l-10-7" stroke="#608570" stroke-width="2" stroke-linecap="round"/><circle cx="330" cy="59" r="2.5" fill="#608570"/>
              <rect x="190" y="101" width="130" height="143" rx="26" fill="#fff"/>
              <g clip-path="url(#vessel)"><rect class="vessel-water" data-vessel-water x="190" y="160" width="130" height="84" fill="#aed6e2"/><g class="bubbles" data-bubbles><circle cx="221" cy="211" r="4"/><circle cx="275" cy="199" r="3"/><circle cx="251" cy="223" r="5"/></g></g>
              <rect x="190" y="101" width="130" height="143" rx="26" stroke="#91b4a7" stroke-width="2" fill="none"/>
              <text x="255" y="126" text-anchor="middle" class="strong">BOILER</text>
              <rect x="211" y="145" width="88" height="27" rx="5" fill="#fff" fill-opacity=".88"/>
              <text data-diagram-temperature x="255" y="163" text-anchor="middle" class="temperature-label">148 °C</text>
              <path d="M308 188h22" stroke="#91b4a7" stroke-width="2"/><circle cx="337" cy="188" r="9" fill="#fff" stroke="#91b4a7" stroke-width="2"/><text x="337" y="192" text-anchor="middle" class="small">T</text>
              <rect x="212" y="245" width="12" height="10" rx="2" fill="#a2bab0"/><rect x="286" y="245" width="12" height="10" rx="2" fill="#a2bab0"/>
              <rect x="227" y="256" width="56" height="22" rx="6" fill="#f3e4d0" stroke="#d9c2a3"/>
              <g class="flame" data-flame><path d="M239 267c-5-9 9-16 7-26 10 6 7 12 12 14 4-6 5-9 4-14 14 13 12 26 0 29h-16Z" fill="#dfa152"/><path d="M247 267c-3-6 6-10 6-16 7 6 11 14 3 17Z" fill="#f3c97e"/></g>
              <text x="255" y="296" text-anchor="middle" class="small">BURNER</text>
              <circle cx="414" cy="142" r="36" fill="#fff" stroke="#a5bfad" stroke-width="2"/>
              <g class="rotor" data-rotor fill="#b0c7b3"><path d="M414 137c-16-24 12-25 12-25l-8 26Z"/><path d="M419 144c28-2 15 23 15 23l-19-20Z"/><path d="M410 148c-12 25-27 2-27 2l27-8Z"/></g><circle cx="414" cy="142" r="6" fill="#65896e"/>
              <text x="414" y="196" text-anchor="middle" class="strong">GENERATOR</text>
              <path class="pipe-base" d="M450 142H482V231H456"/><path class="pipe electric" data-power-flow d="M450 142H482V231H456"/>
              <rect x="356" y="220" width="106" height="61" rx="8" fill="#edf4e8" stroke="#d5e1cd"/>
              <text x="370" y="239" class="small">POWER OUT</text><text x="370" y="264" class="power-reading" data-diagram-power>36.0<tspan> kW</tspan></text>
            </svg>
          </div>
          <div class="status" data-status data-tone="running"><i class="status-dot" aria-hidden="true"></i><strong data-status-title>Running</strong><span data-status-detail>Steady output</span></div>
          <div class="readings" aria-label="Simulated sensor readings">
            <div class="reading"><span>Temperature</span><strong data-temperature>148</strong><small>°C</small></div>
            <div class="reading"><span>Pressure</span><strong data-pressure>3.1</strong><small>bar</small></div>
            <div class="reading power"><span>Power output</span><strong data-power>36.0</strong><small>kW</small></div>
          </div>
          <div class="level"><label for="water-level">Water level</label><meter id="water-level" min="0" max="100" low="20" high="90" optimum="60" value="62">62%</meter><span data-level>62%</span></div>
          <div class="controls">
            <div class="control"><label for="fuel">Fuel intake <output for="fuel" aria-live="off" data-fuel>55%</output></label><input id="fuel" type="range" min="0" max="100" step="1" value="55" aria-valuetext="55 percent"><small>Less heat ↔ more heat</small></div>
            <div class="control"><label for="water">Water intake <output for="water" aria-live="off" data-water>60%</output></label><input id="water" type="range" min="0" max="100" step="1" value="60" aria-valuetext="60 percent"><small>Less flow ↔ more flow</small></div>
          </div>
          <div class="actions"><button type="button" data-action="toggle" aria-pressed="true" disabled>Boiler running</button><button type="button" data-action="reset">Reset inputs ↺</button></div>
          <p class="caption">Fuel heats the water. Steam turns the generator.<br>Adjust the inputs to watch the simulated readings respond.</p>
        </section>
        <section class="panel" aria-labelledby="operational-alerts-heading" data-operational-alerts>
          <header class="heading"><h2 id="operational-alerts-heading">Operational Alerts</h2></header>
          <div data-alert-list></div>
        </section>`;
      this.shadowRoot.querySelector('[data-operational-alerts]').style.borderTop = '1px solid #e8ede8';
      this._refs = {};
      for (const name of ['context', 'temperature', 'pressure', 'power', 'level', 'fuel', 'water', 'status', 'status-title', 'status-detail', 'vessel-water', 'needle', 'flame', 'rotor', 'bubbles', 'water-flow', 'fuel-flow', 'steam-flow', 'power-flow', 'diagram-temperature', 'diagram-power']) {
        this._refs[name] = this.shadowRoot.querySelector(`[data-${name}]`);
      }
      for (const control of ['fuel', 'water']) {
        const input = this.shadowRoot.getElementById(control);
        input.disabled = true;
        input.addEventListener('input', (event) => {
          this._editing = true;
          this._refs[control].textContent = `${event.target.value}%`;
        });
        input.addEventListener('change', () => {
          this._requestInputs({ fuel: Number(this.shadowRoot.getElementById('fuel').value), water: Number(this.shadowRoot.getElementById('water').value) });
        });
      }
      this.shadowRoot.querySelector('[data-action=reset]').addEventListener('click', () => this._requestInputs({ fuel: 55, water: 60 }));
    }

    connectedCallback() { this._render(); }

    setContext({ labId, defense = '0' }) {
      const context = { labId: String(labId), defense: String(defense) };
      this._context = context;
      const label = context.labId === 'killchain' ? 'Capstone' : `Lab ${context.labId.padStart(2, '0')}`;
      const difficulty = ({ '0': 'Easy', '1': 'Medium', '2': 'Hard', vulnerable: 'Easy', defended: 'Medium', guardrailed: 'Hard' })[context.defense] || 'Simulation';
      this._refs.context.textContent = `${label} · ${difficulty}`;
      this._render();
    }

    setState(boiler) {
      this._state = boiler ? { ...boiler } : null;
      this._render();
    }

    setOperationalAlerts(alerts = []) {
      const signature = JSON.stringify(alerts);
      if (signature === this._alertsSignature) return;
      this._alertsSignature = signature;
      const list = this.shadowRoot.querySelector('[data-alert-list]');
      list.replaceChildren();
      this._alertForms.clear();
      const element = (tag, text, className) => {
        const result = document.createElement(tag);
        if (text !== undefined) result.textContent = text;
        if (className) result.className = className;
        return result;
      };
      for (const alert of alerts) {
        const form = element('form', undefined, 'caption');
        const severity = element('span', alert.severity, 'badge');
        const message = element('span', alert.message);
        const timestamp = element('time');
        const date = new Date(alert.timestamp * 1000);
        timestamp.dateTime = date.toISOString();
        timestamp.textContent = date.toLocaleString();
        for (const [label, value] of [['Severity', severity], ['Message', message], ['Timestamp', timestamp]]) {
          const line = element('p');
          line.append(element('strong', `${label}: `), value);
          form.append(line);
        }
        const label = element('label', 'Comments');
        const input = element('textarea');
        input.id = `alert-comments-${alert.id}`;
        input.name = 'comments';
        input.rows = 3;
        input.maxLength = 2000;
        label.htmlFor = input.id;
        Object.assign(input.style, { display: 'block', width: '100%', resize: 'vertical', padding: '8px',
          font: '12px/1.5 system-ui, sans-serif', color: 'inherit', border: '1px solid #dce4de', borderRadius: '6px' });
        const draft = this._alertDrafts.get(alert.id);
        if (draft && draft.timestamp !== alert.timestamp) this._alertDrafts.delete(alert.id);
        input.value = this._alertDrafts.get(alert.id)?.comments ?? alert.comments;
        const actions = element('div', undefined, 'actions');
        actions.style.padding = '10px 0 0';
        const save = element('button', 'Save');
        save.type = 'submit';
        const feedback = element('span', '', 'context');
        feedback.setAttribute('role', 'status');
        input.addEventListener('input', () => {
          this._alertDrafts.set(alert.id, { timestamp: alert.timestamp, comments: input.value });
          feedback.textContent = 'Unsaved changes';
        });
        form.addEventListener('submit', (event) => {
          event.preventDefault();
          if (this._alertSaving) return;
          this.dispatchEvent(new CustomEvent('alert-comment-save', {
            detail: { alertId: alert.id, comments: input.value }, bubbles: true, composed: true,
          }));
        });
        actions.append(save, feedback);
        form.append(label, input, actions);
        this._alertForms.set(alert.id, { input, save, feedback });
        list.append(form);
      }
      this.setAlertSaving(this._alertSaving);
    }

    setAlertSaving(saving) {
      this._alertSaving = saving;
      for (const { input, save, feedback } of this._alertForms.values()) {
        input.disabled = saving;
        save.disabled = saving;
        if (saving) feedback.textContent = 'Saving…';
      }
    }

    finishAlertSave(alertId, alerts, error) {
      if (alerts) {
        this._alertDrafts.delete(alertId);
        this._alertsSignature = null;
        this.setOperationalAlerts(alerts);
      }
      const form = this._alertForms.get(alertId);
      if (form) form.feedback.textContent = error ? `Not saved: ${error}` : 'Saved';
    }

    finishInput() { this._editing = false; this._render(); }

    get simulationState() { return this._state ? { ...this._state, ...this._context } : null; }

    _requestInputs(inputs) {
      this._editing = true;
      for (const control of ['fuel', 'water']) {
        this.shadowRoot.getElementById(control).value = inputs[control];
        this._refs[control].textContent = `${inputs[control]}%`;
      }
      this.dispatchEvent(new CustomEvent('boiler-inputs', { detail: inputs, bubbles: true, composed: true }));
    }

    _render() {
      const s = this._state;
      const r = this._refs;
      for (const control of ['fuel', 'water']) this.shadowRoot.getElementById(control).disabled = !s;
      this.shadowRoot.querySelector('[data-action=reset]').disabled = !s;
      if (!s) {
        r['status-title'].textContent = 'Connecting';
        r['status-detail'].textContent = 'Waiting for readings';
        return;
      }
      r.temperature.textContent = Math.round(s.temperature);
      r.pressure.textContent = s.pressure.toFixed(1);
      r.power.textContent = s.power.toFixed(1);
      r.level.textContent = `${Math.round(s.level)}%`;
      this.shadowRoot.getElementById('water-level').value = s.level;
      for (const control of ['fuel', 'water']) {
        const input = this.shadowRoot.getElementById(control);
        if (!this._editing) {
          if (input.value !== String(s[control])) input.value = s[control];
          r[control].textContent = `${s[control]}%`;
        }
        input.setAttribute('aria-valuetext', `${input.value} percent`);
      }
      r['diagram-temperature'].textContent = `${Math.round(s.temperature)} °C`;
      // Reuse nodes; no model text is interpreted as markup.
      r['diagram-power'].firstChild.nodeValue = s.power.toFixed(1);
      const waterHeight = 118 * s.level / 100;
      r['vessel-water'].setAttribute('y', 244 - waterHeight);
      r['vessel-water'].setAttribute('height', waterHeight);
      r.needle.style.transform = `rotate(${clamp(s.pressure / 15, 0, 1) * 130}deg)`;
      r.flame.style.transform = `scale(${s.running ? .45 + s.fuel / 160 : 0})`;
      r.flame.style.opacity = s.running && s.fuel > 0 ? 1 : 0;
      r.rotor.style.animationDuration = `${Math.max(.7, 7 - s.power / 20)}s`;
      for (const [name, flowing] of [['water-flow', s.water > 0], ['fuel-flow', s.running && s.fuel > 0], ['steam-flow', s.pressure > .15], ['power-flow', s.power > .5], ['rotor', s.power > .5], ['bubbles', s.temperature > 95 && s.level > 45]]) {
        r[name].classList.toggle('flow-off', !flowing);
      }
      let title = 'Running', detail = 'Steady output', tone = 'running';
      if (!s.running) { title = 'Stopped'; detail = 'Cooling down'; tone = 'stopped'; }
      else if (!s.fuel) { title = 'Idle'; detail = 'No fuel flow'; tone = 'stopped'; }
      else if (s.level < 20) { title = 'Low water'; detail = 'Increase water intake'; tone = 'warning'; }
      else if (s.pressure > 9 || s.temperature > 240) { title = 'High heat'; detail = 'Reduce fuel or add water'; tone = 'warning'; }
      else if (s.level > 90) { title = 'High water'; detail = 'Reduce water intake'; tone = 'warning'; }
      else if (s.temperature < 100) { title = 'Warming up'; detail = 'Building steam'; }
      r.status.dataset.tone = tone;
      r['status-title'].textContent = title;
      r['status-detail'].textContent = detail;
    }
  }

  if (!customElements.get('boiler-panel')) customElements.define('boiler-panel', BoilerPanel);
})();
