#!/usr/bin/env python3
"""Read-only app request capture and optional isolated context-budget inference.

No persistent container/model/application setting is edited. The two transport
captures import the original server inside short-lived docker-exec processes and
mock URL transport before calling local_model. Only --context-probe sends one
actual model request, with a deliberately one-token output cap.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request


def command(parts, source=None):
    return subprocess.check_output(parts, input=source, text=True, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--context-probe", action="store_true")
    parser.add_argument("--expected-threads", type=int, help="Require this running app setting; otherwise derive it from the current container environment")
    parser.add_argument("--output-dir", default="performance/final-verification/configuration")
    parser.add_argument("--base-url", default="http://172.21.0.2:11434")
    parser.add_argument("--lock-file", default="/tmp/ollama-nine-benchmark.lock")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"status": "PLAN ONLY", "captures": "original server transport mocked in two exec processes; running app stays16", "context_probe": args.context_probe}))
        return 0
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with open(args.lock_file, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = json.loads(command(["docker", "inspect", "nine-labs-app-1"]))[0]
        capture = '''import io,json,os
from unittest.mock import patch
import server
seen=[]
def fake(request,timeout=None):
    seen.append({"url":request.full_url,"payload":json.loads(request.data),"timeout":timeout})
    return io.BytesIO(json.dumps({"message":{"content":"VERIFY_NO_INFERENCE"}}).encode())
with patch.object(server.urllib.request,"urlopen",side_effect=fake):
    answer=server.local_model([{"role":"user","content":"Transport capture only; no network request."}])
print(json.dumps({"environment":{k:v for k,v in os.environ.items() if k.startswith("OLLAMA_")},"answer":answer,"captured":seen,"scope":"URL transport mocked; no inference or persistent environment change"}))
'''
        actual = json.loads(command(["docker", "exec", "-i", "nine-labs-app-1", "python", "-"], capture))
        running_threads = int(actual["environment"].get("OLLAMA_NUM_THREAD", "16"))
        expected_threads = args.expected_threads if args.expected_threads is not None else running_threads
        isolated_threads = 16 if running_threads != 16 else 24
        hypothetical = json.loads(command(["docker", "exec", "-i", "-e", f"OLLAMA_NUM_THREAD={isolated_threads}", "nine-labs-app-1", "python", "-"], capture))
        after = json.loads(command(["docker", "inspect", "nine-labs-app-1"]))[0]
        observed = actual["captured"][0]["payload"]
        proposed = hypothetical["captured"][0]["payload"]
        expected = {"temperature": .3, "num_predict": 512, "num_ctx": 8192, "num_thread": expected_threads}
        verified = (len(actual["captured"]) == len(hypothetical["captured"]) == 1
                    and running_threads == expected_threads
                    and observed["options"] == expected and proposed["options"] == {**expected, "num_thread": isolated_threads}
                    and observed["model"] == proposed["model"] == "ministral-3:3b"
                    and observed["think"] is proposed["think"] is False
                    and observed["stream"] is proposed["stream"] is False
                    and observed["keep_alive"] == proposed["keep_alive"] == "24h"
                    and actual["captured"][0]["url"] == hypothetical["captured"][0]["url"] == "http://aigoat-ollama-1:11434/api/chat"
                    and actual["captured"][0]["timeout"] == hypothetical["captured"][0]["timeout"] == 240
                    and before["Config"]["Env"] == after["Config"]["Env"]
                    and before["Id"] == after["Id"] and before["State"]["StartedAt"] == after["State"]["StartedAt"])
        evidence = {"recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(), "verified": verified,
                    "actual_running_environment_capture": actual, "isolated_process_thread_override_capture": hypothetical,
                    "expected_running_threads": expected_threads, "isolated_probe_threads": isolated_threads,
                    "persistent_container_environment_unchanged": before["Config"]["Env"] == after["Config"]["Env"],
                    "scope": "Actual production source executed with mocked transport. The alternate thread override exists only in a separate exec process; the running app environment is unchanged by this check."}
        (output / "app-request-overrides.json").write_text(json.dumps(evidence, indent=2) + "\n")
        probe_success = True
        if args.context_probe:
            build = '''import json
import catalog
def sized(text,n):return (text*((n//len(text))+1))[:n]
record={"defense_level":0,"messages":[{"role":r,"content":sized("Synthetic shopper note 1042: mug at USD 16.99, hoodie at USD 41.99; ask about cotton care and delivery in 3 days. ",2250)} for r in ["user","assistant","user","assistant"]]}
entries=[{"id":"synthetic-budget-1","title":"Budget fixture1","content":sized("Catalog record SKU MUG-15: ceramic mug, USD 16.99; packaging 2 items; stock check 2026-11-03; care with mild soap. ",3250)},{"id":"synthetic-budget-2","title":"Budget fixture2","content":sized("Policy note 1042: hoodie cotton care at 30 C; delivery in 3 days; order IDs 1001 and 1002; return review after 14 days. ",3250)}]
current=sized("For this synthetic context-size probe output only OK. Compare mug SKU MUG-15 at USD 16.99 and cotton hoodie at USD 41.99; delivery in 3 days. ",4000)
context_lab=8 if 8 in catalog.KB_LABS else min(catalog.KB_LABS)
messages=catalog.build_messages(context_lab,record,current,entries)
print(json.dumps({"lab_id":context_lab,"messages":messages,"history_characters":sum(len(x["content"]) for x in record["messages"]),"retrieved_characters":sum(len(x["content"]) for x in entries),"current_characters":len(current),"scope":"Representative valid English context at existing per-field allowances in an active KB lab; no production learner database used."}))
'''
            fixture = json.loads(command(["docker", "exec", "-i", "nine-labs-app-1", "python", "-"], build))
            payload = {"model": "ministral-3:3b", "messages": fixture["messages"], "stream": False,
                       "think": False, "keep_alive": "24h", "options": {**expected, "num_predict": 1, "seed": 42, "top_p": .9, "top_k": 40, "repeat_penalty": 1.0}}
            request = urllib.request.Request(args.base_url.rstrip("/") + "/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=600) as response:
                result = json.load(response)
            count = result.get("prompt_eval_count")
            probe_success = (not result.get("error") and result.get("done") is True
                             and result.get("model") == "ministral-3:3b"
                             and isinstance(count, int) and count > 0
                             and result.get("eval_count", 0) > 0)
            record = {"recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(), "fixture": fixture,
                      "request_options": payload["options"], "request_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                      "raw_response": result, "probe_api_completed": probe_success, "prompt_eval_count": count,
                      "exceeds4096": isinstance(count, int) and count > 4096,
                      "processed_input_plus512_fits8192": isinstance(count, int) and count + 512 <= 8192,
                      "source_input_fit_proven": False,
                      "scope": "One-token output is deliberate for context sizing. Not a normal-response, flag-award or response-completeness test. API count describes processed input after any engine truncation and includes actual chat formatting and any reported cached tokens. A count above4096 proves that smaller window cannot retain even the processed context; it does not independently prove every original source message fit8192. Whitespace-only one-token output is not treated as a normal-answer success."}
            (output / "context-budget-probe.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"app_request_overrides_verified": verified, "output": str(output), "context_probe_executed": args.context_probe, "context_probe_api_completed": probe_success if args.context_probe else None}))
        return 0 if verified and probe_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
