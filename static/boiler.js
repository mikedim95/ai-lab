'use strict';

/* A shared visual classroom simulation. It does not interpret model replies or
   change CTF results. All readings below are illustrative, not plant telemetry. */
(() => {
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const defaults = () => ({ fuel: 55, water: 60, temperature: 148, pressure: 3.1, power: 36, level: 62, running: true });
  const approach = (value, target, seconds, dt) => value + (target - value) * (1 - Math.exp(-dt / seconds));

  class BoilerPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._contexts = new Map();
      this._context = { labId: '1', defense: '0' };
      this._state = defaults();
      this._contexts.set('1:0', this._state);
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
          <div class="actions"><button type="button" data-action="toggle" aria-pressed="true">Stop boiler</button><button type="button" data-action="reset">Reset simulation ↺</button></div>
          <p class="caption">Fuel heats the water. Steam turns the generator.<br>Adjust the inputs to watch the simulated readings respond.</p>
        </section>`;
      this._refs = {};
      for (const name of ['context', 'temperature', 'pressure', 'power', 'level', 'fuel', 'water', 'status', 'status-title', 'status-detail', 'vessel-water', 'needle', 'flame', 'rotor', 'bubbles', 'water-flow', 'fuel-flow', 'steam-flow', 'power-flow', 'diagram-temperature', 'diagram-power']) {
        this._refs[name] = this.shadowRoot.querySelector(`[data-${name}]`);
      }
      for (const control of ['fuel', 'water']) {
        this.shadowRoot.getElementById(control).addEventListener('input', (event) => {
          this._state[control] = clamp(Number(event.target.value), 0, 100);
          this._render();
        });
      }
      this.shadowRoot.querySelector('[data-action=toggle]').addEventListener('click', () => { this._state.running = !this._state.running; this._render(); });
      this.shadowRoot.querySelector('[data-action=reset]').addEventListener('click', () => this.resetSimulation());
    }

    connectedCallback() {
      if (this._timer) return;
      this._last = performance.now();
      this._render();
      this._timer = setInterval(() => {
        const now = performance.now();
        const dt = clamp((now - this._last) / 1000, 0, .5);
        this._last = now;
        if (!document.hidden) { this._advance(dt); this._render(); }
      }, 200);
    }

    disconnectedCallback() { clearInterval(this._timer); this._timer = null; }

    setContext({ labId, defense = '0' }) {
      const context = { labId: String(labId), defense: String(defense) };
      const key = `${context.labId}:${context.defense}`;
      if (!this._contexts.has(key)) this._contexts.set(key, defaults());
      this._context = context;
      this._state = this._contexts.get(key);
      const label = context.labId === 'killchain' ? 'Capstone' : `Lab ${context.labId.padStart(2, '0')}`;
      const difficulty = ({ '0': 'Easy', '1': 'Medium', '2': 'Hard', vulnerable: 'Easy', defended: 'Medium', guardrailed: 'Hard' })[context.defense] || 'Simulation';
      this._refs.context.textContent = `${label} · ${difficulty}`;
      this._render();
    }

    resetSimulation(labId = this._context.labId) {
      const id = String(labId);
      for (const key of this._contexts.keys()) if (key.startsWith(`${id}:`)) this._contexts.delete(key);
      const key = `${this._context.labId}:${this._context.defense}`;
      if (this._context.labId === id) { this._state = defaults(); this._contexts.set(key, this._state); this._render(); }
    }

    get simulationState() { return { ...this._state, ...this._context }; }

    _advance(dt) {
      const s = this._state;
      const fuel = s.running ? s.fuel : 0;
      s.level = approach(s.level, clamp(10 + s.water * .95 - fuel * .1, 0, 100), 6, dt);
      // Deliberately simple classroom dynamics, not a thermodynamic model.
      const temperature = 20 + fuel * 2.8 / (.8 + s.water / 150);
      s.temperature = approach(s.temperature, temperature, 5, dt);
      s.pressure = approach(s.pressure, Math.max(0, (s.temperature - 100) * .065), 2, dt);
      const power = clamp(s.pressure * 12 * Math.min(1, s.level / 28), 0, 120);
      s.power = approach(s.power, power, 2, dt);
    }

    _render() {
      const s = this._state;
      const r = this._refs;
      r.temperature.textContent = Math.round(s.temperature);
      r.pressure.textContent = s.pressure.toFixed(1);
      r.power.textContent = s.power.toFixed(1);
      r.level.textContent = `${Math.round(s.level)}%`;
      this.shadowRoot.getElementById('water-level').value = s.level;
      for (const control of ['fuel', 'water']) {
        const input = this.shadowRoot.getElementById(control);
        if (input.value !== String(s[control])) input.value = s[control];
        input.setAttribute('aria-valuetext', `${s[control]} percent`);
        r[control].textContent = `${s[control]}%`;
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
      const toggle = this.shadowRoot.querySelector('[data-action=toggle]');
      toggle.textContent = s.running ? 'Stop boiler' : 'Start boiler';
      toggle.setAttribute('aria-pressed', String(s.running));
    }
  }

  if (!customElements.get('boiler-panel')) customElements.define('boiler-panel', BoilerPanel);
})();
