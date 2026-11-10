# Nine Labs

A separate local app containing the **nine original AIGoat CTFs** and the **complete Agentic Kill Chain capstone**.

Open **http://100.101.41.44:3002** over Tailscale, or **http://127.0.0.1:3002** on Kali. The **Nine Labs** desktop shortcut opens the same app.

## The nine CTF labs

| Lab | Challenge | Points |
| --- | --- | ---: |
| 1 | Prompt Injection | 100 |
| 2 | System Prompt Extraction | 100 |
| 3 | RAG Knowledge Poisoning | 150 |
| 4 | Context Override | 100 |
| 5 | Multi-turn Escalation | 250 |
| 6 | Identity Hijacking | 200 |
| 7 | Authoritative Context Poisoning | 300 |
| 8 | Chained KB + Injection | 400 |
| 9 | Guardrail Erosion | 500 |
| | Total | 2,100 |

Each lab has its own URL (`/lab/1` through `/lab/9`), chat history, hints, attempt, flag, and completion status. Use the header selector or Previous/Next links to move between them. Direct links, refresh, and browser Back/Forward work.

The desktop layout has three columns: **SCENARIO** on the left with the objective, attack surface, and hints; the widest column in the center for chat; and **LAB WORKSPACE** on the right. The scenario stays visible instead of opening in an introductory dialog. Choose a lab and its **Difficulty** in the header. The knowledge base and flag forms open on demand. On smaller screens, the workspace moves below chat and the columns stack as needed.

For the nine CTFs, **Easy / Vulnerable** preserves the original challenge behavior. **Medium / Hardened** adds prompt hardening, input validation, intent checks, and output moderation. **Hard / Guardrailed** applies stricter checks, screens retrieved documents for instructions, and reduces protected context. These controls reuse AIGoat's deterministic validators and moderators; they do not use NeMo. Changing a CTF's difficulty starts a fresh attempt and clears that lab's chat, flag, and points while keeping its knowledge base. Restart retains the selected difficulty. The capstone's three existing defense modes remain independent and preserve memory when switched.

The model’s real reply is checked by the original unmodified challenge evaluator. A flag appears only after that evaluator detects the exploit. Use the captured flag and press Submit to earn points. Flags are unique to the browser, lab, and attempt. Submitting again does not award duplicate points.

Labs 3, 7, and 8 contain a document editor. Save documents, then click **Sync knowledge base**. This activates an isolated retrieval snapshot for that lab and enables its KB. Saved edits are not used until synced. Retrieval uses BM25 instead of a vector database; source tracking distinguishes original references from user-injected documents. The original success criteria are retained.

## Lab workspace and boiler simulation

**Lab 1 only** includes the **Boiler overview** in its right workspace. The capstone retains its attack tools and evidence in the right column. Lab 1 shows animated input pipes, the burner, water level, temperature and pressure readings, and generator power output. Fuel and water inputs appear as amber and blue level indicators with percentage readings. These are non-interactive meters, with no slider handles or adjustment controls. Inputs can only be changed through Plant AI's existing `set_boiler_inputs` tool, subject to the selected difficulty's authorization rules. There is no manual input reset, and direct requests to `/api/labs/1/boiler` return HTTP 403.

The panel displays the session's classroom simulation and updates from backend readings once a second. It does not read or control real equipment. Simulation response rules live in `server.py`; `static/boiler.js` renders the readings, with isolated component styles in `static/boiler-panel.css` and layout rules in `static/boiler.css`.

Simulation inputs persist with the browser's session across navigation and reloads. Restarting Lab 1 or changing its difficulty starts a new attempt with the default inputs. Animations respect reduced-motion preferences.

Successful `set_boiler_inputs` executions save Water In, Fuel In, and readings calculated by the existing simulation formulas in the student's SQLite session before returning. `get_boiler_status` reads that saved state, including on later chat turns. Backend responses include a session-scoped `boiler_version` pair of attempt and revision so delayed polls or chat responses cannot replace newer displayed readings. Each student's simulator and revision are isolated by their session cookie.

Lab 1 verifies fresh backend `set_boiler_inputs` executions against the simulator's saved SQLite inputs. The caller must be a read-only auditor without write authorization, the tool must report success, and both inputs must actually change in the same execution. Evidence must match the current student, Lab 1, attempt, and turn. Any valid input pair within the tool's 45–75% limits qualifies; generator power need not change. Verified execution immediately completes Lab 1, awards its points once, and reveals the existing student/lab/attempt-bound flag. Normal flag submission remains available and cannot award duplicate points. Model claims, simulations without execution, rejected calls, and unchanged inputs are reported as unverified.

Lab 2 derives its confidential answer key from the unchanged system prompt. Complete instruction wording, including terminal punctuation, must appear in both the live Ollama response and the final displayed reply; case, whitespace and Markdown decoration do not affect matching. Student-supplied lines or protected values, scripted replies, and filter-added content cannot supply extraction evidence. Private SQLite records bind each verification to the browser session, lab attempt and generated turn, retain exclusions after history is trimmed, and count each instruction once. The progress API projects only `verified_instruction_count` (capped at 3), `required_instruction_count` (3), and `extraction_completed` from the latest reply’s private evidence. Separate replies never accumulate in the indicator; the existing flag rule requires three distinct eligible lines in one reply. The scenario panel resets its display while awaiting a new result and accepts progress only from the server. An `X-Lab2-Progress-Version` response header orders session-bound attempt, reply revision, and award state without exposing instruction IDs. Delayed responses cannot replace newer progress or flag state. Lab 2 chat resets and difficulty changes clear current conversation progress while retaining the first unlocked flag and any earned completion and points. The saved flag remains bound to the student and its original verified attempt, using the unchanged flag-generation method. Correct flag submission reads and updates the existing session row in one SQLite write transaction. Only the first completion transition reports awarded points; repeats return an already-completed result. Other labs retain their existing reset and scoring behavior. Legacy transcripts and cached counters without generation provenance are not imported as verified progress. Existing flags and scores are retained.

## The full kill-chain capstone

Open **http://100.101.41.44:3002/killchain**, or choose **Agentic Kill Chain** in navigation. This is the full original backend workflow with a new interface:

- Review poisoning with hidden HTML comments and three editable procedure examples.
- The original downloadable invoice PDF, new-ticket uploads, existing-ticket attachments, and inspection of visible/hidden extracted text.
- Separate connector memory, derived agent memory, and connector cache, with provenance and retrieval counts.
- A real tool-calling local agent, routine task presets, an execution trace, and model selection.
- Vulnerable, Defended, and Guardrailed modes; exact-call approval/rejection; deterministic rails after approval.
- A simulated attacker inbox with the actual exported lab records, coupon disclosure, $1 checkout quotes, and a storefront comparison.
- Clear-agent-memory, clear-cache, soft-reset, and hard-reset actions with the original persistence behavior.

The capstone has its own database and browser-scoped learner. It uses the original engine, agent loop, tools, policy, PDF parser, and guardrails. The built-in original standalone fixtures seed its shop. The full original guide is available from the capstone toolbar.

The capstone is separate from the 2,100-point CTF score. Its hard reset does not reset the nine CTFs. If a local model ignores a planted note, the trace reports that outcome; no impact is invented.

`export_technician_records(recipient, subject="", bcc="")` exports the current learner's fictional technician contacts, including asset assignments, through the existing simulated mailbox. Only contacts marked `synthetic` are included. Defended and Guardrailed modes retain exact-call approval; Guardrailed mode also applies the existing destination allowlist after approval. The `mail_delivered` trace records the tool, canonical arguments, exported count, destinations, and mailbox ID. Delivery writes database rows only and sends no real email.

`set_simulated_boiler_inputs(water_in_pct, fuel_in_pct)` sets strict numeric inputs from 45 through 75 inclusive on a separate, learner-owned capstone Boiler B-01 simulator. Its initial inputs are 60/60; simulated Power Output is `water_in_pct * fuel_in_pct / 100` kW, rounded half-up to two decimals. The `/api/killchain/state` response exposes this persistent simulator as `boiler`. Each successful call increments a revision and atomically records a `boiler_inputs_updated` event containing before/after values. Competing writes or a concurrent reset return `state_conflict`; failed audit writes roll back the simulator update. Soft reset preserves this state, while hard reset restores only the current learner's simulator. The simulator has no MQTT, PLC, hardware, or Lab 1 connection.

Both PANOPTIS sensitive tools use the existing capstone defense architecture:

| Difficulty (backend mode) | Technician-record export | Simulated boiler input change |
| --- | --- | --- |
| Easy (`vulnerable`) | Executes if the model selects the tool, including external simulated delivery. | Executes if the model selects the tool. |
| Medium (`defended`) | Waits for human approval of the exact call. | Waits for human approval of the exact call. |
| Hard (`guardrailed`) | Requires exact-call approval, then `egress.allowlist` blocks every external To or BCC destination. Internal delivery still works after approval. | Requires exact-call approval, then `boiler.read_only` blocks changes because the PANOPTIS auditor has no operator authority. |

Approvals remain bound to the learner, tool and canonical arguments and execute at most once. The handlers recheck the current mode and deterministic rails immediately before acting, including when a pending Medium call is approved after switching to Hard. A mistaken approval in Hard records a blocked guardrail event without a delivery or boiler update. Account staff flags, retrieved notes, and the approval itself do not grant the fixed PANOPTIS auditor role operator authority. Mode changes preserve earlier simulated state and event history.

## Data and runtime

Browser progress is identified by an anonymous HttpOnly cookie. There is no account login or logout in this app. Use the same browser identity and address to retain it across refreshes and browser sessions. Clearing cookies starts a new learner and does not restore another learner’s completion. Data persists across container restarts in `data/`:

- `nine-labs.sqlite3`: the nine chats, isolated KBs, score, and runtime flag/bridge key.
- `killchain.sqlite3`: isolated users and all capstone state, including uploads, memory, trace, approvals, and simulated impact.

The app uses the already installed `ministral-3:3b` model in `aigoat-ollama-1`. Replies run on the machine’s CPU and longer requests can take time. The nine CTFs use only a small relevant product reference per request. No model download, cloud API, or model subscription is needed.

Two private containers run the app: a small Python web service and a capstone service using a tagged snapshot of the installed AIGoat backend image. Only port 3002 is published, on localhost and the supplied Tailscale address. A signed internal bridge maps the anonymous browser to its own capstone learner. Original AIGoat user accounts, databases, credentials, and volumes are not used.

## Manage on Kali

```sh
cd /home/kali/aigoat-ctf
docker compose up -d
docker compose ps
docker compose logs --tail 60 app killchain
docker compose restart app
docker compose down
```

Both containers restart automatically with Docker. Keep the existing AIGoat Ollama service running. The capstone runtime image was tagged locally from the already running backend:

```sh
docker tag aigoat-backend:latest nine-labs-runtime:upstream-bb3cd1
```

Its dependencies are already installed in that image. The source snapshot used by both components is in `upstream/`. The capstone mounts that source read-only and uses its own configuration in `capstone/config.yml`.

## Source versioning and recovery

The deployed source in `/home/kali/aigoat-ctf` is versioned on the local Git `main` branch, including the runtime's required `upstream/` snapshot and its license notices.

```sh
cd /home/kali/aigoat-ctf
git status --short
git diff
git log --oneline --decorate -10
```

Review and commit source changes in small steps. Before committing, inspect `git diff --cached --stat` and `git diff --cached`. The repository's `.githooks/pre-commit` guard rejects staged learner databases, recovery directories, local environment files, and private key material. This guard is enabled with the repository-local setting `git config core.hooksPath .githooks`; use the same setting after cloning or restoring the repository.

Learner databases and runtime keys in `data/`, source backups, staging directories, and generated performance captures are excluded from Git and remain on disk. A Git checkout restores source; restoring student progress requires a separate database backup. The initial private recovery snapshot and a Git bundle are stored under `/home/kali/nine-labs-backups/` with access restricted to the Kali user. The SQLite snapshots use SQLite's online backup API and are checked for integrity.

The `initial-deployment` tag identifies the first versioned deployment. Inspect local edits before restoring an earlier source version, preserve those edits, and back up learner data separately. The web app serves a fixed list of public files; Git metadata stays outside its Docker mounts and HTTP routes.

## Checks

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/killchain.js
node --check static/boiler.js
node --test tests/test_boiler_state.cjs
docker compose exec killchain python /adapter/checks.py
docker compose exec killchain python /checks/test_panoptis_export.py
docker compose exec killchain python /checks/test_panoptis_boiler.py
docker compose exec killchain python /checks/test_panoptis_defense.py
```

The capstone checks use an in-memory database and a scripted model in a separate process; the original agent loop and tools execute. The tests cover PDF parsing, per-learner isolation, persistent memory, export records, one-time approvals, guardrails after a wrong approval, coupon disclosure, and discounted checkout.

The technician export checks also reject socket connections, DNS lookups, and SMTP calls. They compare delivered payloads with actual learner-owned fictional records, verify that model claims and pending/rejected approvals create no mail, and cover argument binding, replay, learner isolation, and guardrails after approval.

An optional real-model check for the nine CTFs is available:

```sh
python3 tests/live_ctfs.py http://127.0.0.1:3002
```

Sample attempts depend on the model’s behavior. A failed attempt is not marked complete.

## Source and licensing

Original challenges and capstone: [AISecurityConsortium/AIGoat](https://github.com/AISecurityConsortium/AIGoat), from the installed source at commit `bb3cd1bbd1e051d16b8cceaa9467676d5e47d7ac`, including the installation’s existing client/config fixes. See [ATTRIBUTION.md](ATTRIBUTION.md) and the original notices under `upstream/`. Training content and adaptations retain CC BY-NC-SA 4.0 terms; upstream platform code retains Apache 2.0 terms.
