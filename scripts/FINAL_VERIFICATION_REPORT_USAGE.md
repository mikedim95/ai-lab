# Final verification report builder

`build_final_verification_report.py` reads existing measurements and writes the
concise `OLLAMA_FINAL_VERIFICATION.md`. It never makes model requests, changes
Docker configuration, or modifies raw results/proofs.

Copy `final-verification-settings.example.json` to
`performance/final-verification/report-settings.json` in the project root and
fill the evidence fields after execution. Artifact paths are relative to `--root`.
The default filenames match the fresh benchmark, reliability wrapper and new
Nine Labs verifier. If root chooses another output directory, adjust the mapping.

```sh
python3 scripts/build_final_verification_report.py --root . \
  --settings performance/final-verification/report-settings.json \
  --output OLLAMA_FINAL_VERIFICATION.md \
  --summary performance/final-verification/report-summary.json
```

The six required category rows use PASS/FAIL. Missing evidence is explicitly
labelled NOT RUN in the result/evidence and cannot pass. The nine lab rows require
a real model call, issued flag, accepted flag endpoint and positive points; a
successful HTTP response alone is insufficient. The incorrect-attempt rows must
also involve real inference and produce no accepted flag. Independent refusal
observations are separate from application defense rejection.

The uncached gate requires five measured short and five measured long paired
prompts at each of 16 and 24 threads, with matching prompt/options hashes, actual
cached count zero, streamed TTFT, and a clean phase audit. The performance row
uses the generation-rate median and five paired rate comparisons; it separately
reports API/wall latency and TTFT because output lengths can differ. Optional
20/22/32 groups remain visible without pretending they have five samples.

Reliability reads only the latest recorded 24-thread reliability session. It
requires 30 measured successes, monitoring completeness, unchanged fixed options,
clean audit, and no timeout/restart/OOM/load-reload flag/native PID change/model
absence. Explicit fresh-phase resets do not count as reliability reloads. Missing
or disabled scheduler metrics are never interpreted as zero contention.

Set `production_unchanged`, `app_request_overrides_verified`, and their evidence
only after actual capture/review. A mocked transport can verify the production
request constructor; name that scope rather than describing it as wire inference.
The current preservation snapshot must contain the unchanged model/quantization/
volume/source/configuration checks. `control16_verified` references the earlier
proof suite plus current source/model preservation; it does not invent a new
16-thread lab replay. Nine matching proof records must still exist.

Default deployment compatibility is strict: a separately named formatting
alternate can prove the intended vulnerability and flag remain obtainable, but
does not erase a failed established payload. The report retains both references.
Keep `require_established_payloads_for_deployment: true` unless root explicitly
reviews and documents another compatibility policy.

The report chooses exactly one conclusion. It uses `DEPLOY 24 THREADS` only when
all six gates and the configured established-payload policy pass. With complete
measurements, a failed candidate gate and a verified preserved 16-thread control
produce `KEEP 16 THREADS`. Missing evidence or no verified safe control produces
`FURTHER TESTING REQUIRED`.

The current campaign also needs the separately executed offline normal-response
review. `review_normal_responses.py` must finish after both budget profiles and
restoration are complete. The builder validates its exact canonical raw-row hash,
full input-file hash, run/lab/budget/payload/source/case/options/request identities,
fixture hash, and reviewer/rule module hashes. All 18 named criteria are required.
Only normal-request copies are overlaid; raw rows, errors, attack verdicts and
flag facts stay immutable. Every changed raw/reviewed verdict is displayed, and a
reviewed FAIL can invalidate a raw PASS. An incomplete required review produces
`FURTHER TESTING REQUIRED`.

Numeric cap equality alone is not confirmed truncation. The budget table counts
explicit `length/max_tokens/limit` stops separately from natural `stop` at the
numeric boundary. The offline review can correct the old normal-validator
boundary heuristic when all normal facts and completion checks pass. Attack
compatibility is not reinterpreted by that normal-only review.

Preparation validation was limited to Python AST and JSON parsing. No application
tests or inference were performed by the report author.
