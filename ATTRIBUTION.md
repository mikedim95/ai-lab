# Nine Labs — source and attribution

The nine CTF challenges are copied from AIGoat by AISecurityConsortium:
https://github.com/AISecurityConsortium/AIGoat

Copyright 2025 AISecurityConsortium.

Source: the installation at /home/kali/AIGoat, commit
bb3cd1bbd1e051d16b8cceaa9467676d5e47d7ac, copied on 2026-11-03.

The original nine challenge prompts, metadata, and success evaluators are
preserved in upstream/. The original product and fictional shipping fixtures
are copied from scripts/seed.py into fixtures.json. Prices are converted from
cents to dollars and sample order IDs are deterministic.

Training content and adaptations are licensed under Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 International:
https://creativecommons.org/licenses/by-nc-sa/4.0/

Original notices and terms are included in upstream/NOTICE,
upstream/TRAINING_LICENSE.md, and upstream/app/challenges/LICENSE.
Upstream platform files retain Apache 2.0 terms (upstream/LICENSE).

Changes in this version: a new standalone interface, one URL per challenge,
anonymous browser progress, per-lab chat and knowledge-base isolation,
per-attempt dynamic flags, and a local document editor. Hint navigation text is
adapted to the editor embedded in each relevant lab. Knowledge-base syncing
builds an isolated BM25 retrieval snapshot instead of a Chroma vector index.
The original challenge evaluators and prompts are unmodified.

The full original Agentic Kill Chain is also included. Its backend is preserved
under upstream/app/labs/killchain and uses the copied upstream agent loop,
models, tools, policy, PDF parser, and guardrails. A new private adapter provides
a dedicated browser-scoped learner and database. The interface is rewritten
around the original API, including all attack sources, memory stores, trace,
approval modes, simulated mail, checkout impact, and cleanup operations.
The installation's pre-existing Ollama client and configuration fixes are
included in the source snapshot. The original capstone tests and fake-model
helper are retained for reference; new focused checks run in isolation.

This is an independent local adaptation. AIGoat is the source of the training
content; AISecurityConsortium is not the publisher of this application.

The interface now prioritizes chat with on-demand tools and per-lab scenario briefings. A per-lab difficulty selector adds prompt hardening and reuses the original deterministic input validator, intent classifier, and output moderator, with stricter retrieval and context checks at the highest level. The original source prompt files and evaluator classes remain unchanged.
