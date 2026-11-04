#!/usr/bin/env python3
"""Sequential, isolated production-code Nine Labs regression runner.

No production configuration, learner data, model parameters, images or volumes
are changed. The exact Application and HTTP handlers are loaded from the chosen
project root. Its model callback alone is supplied explicit benchmark options.
Dedicated state under --output-dir makes interruption/resume possible. Treat it
as synthetic test state, never point --output-dir at production's data directory.

The CLI intentionally does not run the existing unittest suite: the operator
must run that suite sequentially before this runner, and record it separately.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch

CATEGORIES = (
    "normal_request", "invalid_attempt", "excessively_long_prompt", "valid_solution",
    "no_protection", "duplicate_award", "session_isolation",
    "protected_mode_l1", "protected_mode_l2", "model_timeout",
)
CSV_FIELDS = (
    "run_id", "phase", "recorded_at", "lab_number", "technique", "objective",
    "test_payload_id", "case_category", "execution_kind", "expected_behavior",
    "actual_behavior", "model_refusal", "defense_rejection", "flag_awarded",
    "result", "response_latency_seconds", "model_request_count", "effective_settings",
    "model_metrics", "truncated", "unexpected_truncation", "model_invoked",
    "source_sha256", "case_sha256", "details",
)
REFUSAL = re.compile(
    r"\b(?:i (?:cannot|can't|won't|must decline|shouldn't|am unable|refuse)|i'm unable|"
    r"not authorized|not permitted|outside my scope|restricted information|"
    r"i'm sorry,? but|i am not allowed|i can't disclose|i cannot reveal)\b", re.I,
)


def is_model_refusal(text):
    """Diagnostic only: never changes text sent to the original evaluator."""
    normalized = text.replace("\u2019", "'").replace("\u2018", "'")
    normalized = normalized.replace("*", "").replace("`", "")
    return bool(REFUSAL.search(normalized))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class Browser:
    def __init__(self, address, cookie=None, timeout=650):
        self.address, self.cookie, self.timeout = address, cookie, timeout

    def request(self, path="/api/state", body=None):
        connection = http.client.HTTPConnection(*self.address, timeout=self.timeout)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        encoded = json.dumps(body).encode() if body is not None else None
        connection.request("POST" if encoded is not None else "GET", path, encoded, headers)
        response = connection.getresponse()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        status, raw = response.status, response.read()
        connection.close()
        return status, json.loads(raw)


class Model:
    """Real Ollama transport, plus one explicitly named timeout fault seam."""
    def __init__(self, args, application_module, catalog_module):
        self.args, self.server, self.catalog = args, application_module, catalog_module
        self.receipts = []
        self.lab_id, self.fault_next = None, False

    def options(self):
        cap = self.args.output_overrides.get(str(self.lab_id), self.args.max_output)
        return {"num_thread": self.args.threads, "num_ctx": self.args.context,
                "num_predict": cap, "temperature": self.args.temperature,
                "top_p": self.args.top_p, "top_k": self.args.top_k,
                "repeat_penalty": self.args.repeat_penalty, "seed": self.args.seed}

    def settings(self):
        return {"model": self.args.model, "options": self.options(), "think": False,
                "stream": False, "keep_alive": self.args.keep_alive,
                "num_batch": "not supplied; existing model/runtime behavior retained"}

    def runtime(self):
        result = {}
        try:
            with urllib.request.urlopen(self.args.base_url.rstrip("/") + "/api/ps", timeout=10) as response:
                result["models"] = json.load(response).get("models", [])
        except Exception as error:
            result["ps_error"] = type(error).__name__
        if self.args.container:
            try:
                result["runner_command"] = subprocess.check_output(
                    ["docker", "top", self.args.container, "-eo", "pid,args"],
                    text=True, stderr=subprocess.STDOUT, timeout=10,
                ).strip()
            except Exception as error:
                result["runner_command_error"] = type(error).__name__
        return result

    def __call__(self, messages):
        started = time.monotonic()
        receipt = {"lab": self.lab_id, "started_at": now(), "settings": self.settings(),
                   "prompt_sha256": sha(messages), "prompt_characters": sum(len(m["content"]) for m in messages)}
        hardening = getattr(self.catalog, "_DEFENSE_RULES", "")
        system_text = "\n".join(message["content"] for message in messages if message["role"] == "system")
        receipt["hardening_policy_present"] = bool(hardening and hardening in system_text)
        if self.fault_next:
            self.fault_next = False
            receipt.update({"execution_kind": "fault_injection", "error": "synthetic TimeoutError at production transport boundary"})
            self.receipts.append(receipt)
            # Exercise production's actual TimeoutError -> APIError(503) path.
            with patch.object(self.server.urllib.request, "urlopen", side_effect=TimeoutError("synthetic regression timeout")):
                return self.server.local_model(messages)
        payload = {"model": self.args.model, "messages": messages, "stream": False,
                   "think": False, "keep_alive": self.args.keep_alive, "options": self.options()}
        receipt["execution_kind"] = "real_model"
        try:
            req = urllib.request.Request(self.args.base_url.rstrip("/") + "/api/chat",
                                         data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.args.request_timeout) as response:
                data = json.load(response)
            reply = data.get("message", {}).get("content", "").strip()
            for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
                receipt[key + "_seconds"] = data.get(key, 0) / 1e9
            receipt["prompt_eval_count"] = data.get("prompt_eval_count", 0)
            receipt["eval_count"] = data.get("eval_count", 0)
            receipt["prompt_tokens_per_second"] = (receipt["prompt_eval_count"] / receipt["prompt_eval_duration_seconds"]
                                                      if receipt["prompt_eval_duration_seconds"] else None)
            receipt["generation_tokens_per_second"] = (receipt["eval_count"] / receipt["eval_duration_seconds"]
                                                          if receipt["eval_duration_seconds"] else None)
            receipt.update({"response_model": data.get("model"), "done_reason": data.get("done_reason"),
                            "truncated": data.get("done_reason") in ("length", "max_tokens", "limit") or data.get("eval_count", 0) >= self.options()["num_predict"],
                            "reply": reply, "model_refusal": is_model_refusal(reply)})
            if not reply:
                raise self.server.APIError(503, "The isolated model returned an empty reply.")
            return reply[:14000]
        except self.server.APIError:
            raise
        except Exception as error:
            receipt["error"] = type(error).__name__ + ": " + str(error)
            receipt["timeout"] = isinstance(error, (TimeoutError, socket.timeout))
            raise self.server.APIError(503, "The isolated model was unavailable or timed out.") from error
        finally:
            receipt["wall_latency_seconds"] = time.monotonic() - started
            receipt["runtime_after"] = self.runtime()
            self.receipts.append(receipt)


class Harness:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.project_root).resolve()
        sys.path.insert(0, str(self.root))
        bridge = self.root / "capstone/bridge.py"
        if not bridge.is_file():
            raise SystemExit("The production capstone/bridge.py dependency is missing from --project-root; copy it unchanged before running.")
        # Kali installs the unrelated Capstone disassembler as a regular Python
        # package. Production's minimal container resolves this local namespace
        # instead; make that same local import resolution explicit in this process.
        package = types.ModuleType("capstone")
        package.__path__ = [str(bridge.parent)]
        sys.modules["capstone"] = package
        import catalog
        import server
        self.catalog, self.server = catalog, server
        self.cases_path = Path(args.cases or self.root / "lab_regression_cases.json").resolve()
        self.cases_document = json.loads(self.cases_path.read_text())
        self.output = Path(args.output_dir).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.state_file = self.output / f"lab_regression_state_{args.phase}.json"
        fingerprint = sha({"model": args.model, "threads": args.threads, "context": args.context,
                           "output": args.max_output, "overrides": args.output_overrides, "temperature": args.temperature,
                           "seed": args.seed, "top_p": args.top_p, "top_k": args.top_k, "repeat_penalty": args.repeat_penalty})
        if args.resume and self.state_file.exists():
            self.state = json.loads(self.state_file.read_text())
            if self.state["configuration_sha256"] != fingerprint:
                raise SystemExit("Resume configuration differs from the saved suite. Use a new phase or output directory.")
        else:
            run_id = args.phase + "-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            self.state = {"run_id": run_id, "configuration_sha256": fingerprint, "cookies": {}, "completed": [],
                          "data_dir": str(self.output / "isolated_lab_state" / run_id), "successful": {}}
        self.run_id = self.state["run_id"]
        self.model = Model(args, server, catalog)
        self.application = server.Application(Path(self.state["data_dir"]), model=self.model)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.application))
        self.httpd.daemon_threads = True
        self.httpd_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.httpd_thread.start()
        self.browsers = {}
        self.rows = []
        result_path = self.output / "lab_regression_results.jsonl"
        if result_path.exists():
            self.rows = [json.loads(line) for line in result_path.read_text().splitlines() if line.strip()]
        self.proof_path = self.output / "proven_lab_solutions.json"
        self.proofs = json.loads(self.proof_path.read_text()) if self.proof_path.exists() else {}
        source_paths = [self.root / "server.py", self.root / "catalog.py", self.root / "fixtures.json", bridge]
        source_paths += sorted((self.root / "upstream/prompts/challenges").glob("*.md"))
        source_paths += sorted((self.root / "upstream/app/challenges/evaluators").glob("*.py"))
        self.source_hashes = {str(path.relative_to(self.root)): sha(path.read_bytes()) for path in source_paths}
        self.source_sha = sha(self.source_hashes)
        write_json(self.output / f"lab_regression_metadata_{self.run_id}.json", {
            "run_id": self.run_id, "phase": args.phase, "recorded_at": now(),
            "isolation": "Exact selected production Application + HTTP handlers, dedicated synthetic SQLite DB and cookies; no production learner data.",
            "model_callback": "Real sequential Ollama requests with explicit benchmark options; artificial timeout faults labelled separately.",
            "settings": self.model.settings(), "source_hashes": self.source_hashes,
            "cases_sha256": sha(self.cases_path.read_bytes()), "refusal_detection": REFUSAL.pattern,
            "refusal_detection_limit": "Wording heuristic, not a semantic judge; mixed refusal/compliance can be labelled refusal and still pass the unchanged evaluator.",
            "percentile_method": "nearest rank: sorted latency at ceil(0.95*n)-1; small n p95 equals the maximum",
        })
        self.save_state()

    def save_state(self):
        self.state["cookies"].update({key: browser.cookie for key, browser in self.browsers.items()})
        write_json(self.state_file, self.state)

    def browser(self, key):
        if key not in self.browsers:
            self.browsers[key] = Browser(self.httpd.server_address, self.state["cookies"].get(key), self.args.request_timeout + 30)
            self.browsers[key].request()
            self.save_state()
        return self.browsers[key]

    def call(self, browser, path="/api/state", body=None, expected=200):
        status, value = browser.request(path, body)
        if path.endswith("/chat") and isinstance(value, dict) and isinstance(value.get("lab"), dict):
            self.observed_defense = bool((value["lab"].get("last_result") or {}).get("defense_blocked"))
        if status != expected:
            raise AssertionError(f"HTTP {status}; expected {expected}: {value}")
        return value

    def lab(self, browser, lab):
        return self.call(browser, "/api/state")["labs"][str(lab)]

    def selected(self, lab, category, payload_id):
        if self.args.only_categories and category not in self.args.only_categories:
            return False
        key = f"{lab}:{category}:{payload_id}"
        if not self.args.resume or key not in self.state["completed"]:
            return True
        if not self.args.rerun_failed:
            return False
        previous = [row for row in self.rows if row["run_id"] == self.run_id and row["lab_number"] == lab
                    and row["case_category"] == category and row["test_payload_id"] == payload_id]
        return not previous or previous[-1]["result"] != "PASS"

    def case(self, entry, category, payload_id, function, kind="structural"):
        lab = entry["lab"]
        if not self.selected(lab, category, payload_id):
            return None
        started, before = time.monotonic(), len(self.model.receipts)
        self.model.lab_id = lab
        self.observed_defense = False
        row = {"run_id": self.run_id, "phase": self.args.phase, "recorded_at": now(),
               "lab_number": lab, "technique": entry["technique"], "objective": entry["objective"],
               "test_payload_id": payload_id, "case_category": category, "execution_kind": kind,
               "expected_behavior": self.cases_document["interpretation"].get(category, self.cases_document["interpretation"].get("protected_mode", "")),
               "model_refusal": False, "defense_rejection": False, "flag_awarded": False,
               "result": "PASS", "effective_settings": self.model.settings(), "details": {},
               "source_sha256": self.source_sha, "case_sha256": sha(entry)}
        try:
            result = function() or {}
            row.update(result)
            row.setdefault("actual_behavior", "Expected behavior observed.")
        except Exception as error:
            row["result"] = "FAIL"
            row["actual_behavior"] = type(error).__name__ + ": " + str(error)
        receipts = copy.deepcopy(self.model.receipts[before:])
        real = [receipt for receipt in receipts if receipt.get("execution_kind") == "real_model"]
        row["model_metrics"] = receipts
        row["model_request_count"] = len(real)
        row["model_invoked"] = bool(real)
        row["model_refusal"] = row.get("model_refusal", False) or any(receipt.get("model_refusal", False) for receipt in real)
        row["defense_rejection"] = row.get("defense_rejection", False) or self.observed_defense
        row["truncated"] = any(receipt.get("truncated", False) for receipt in real)
        row["unexpected_truncation"] = row["truncated"] and category in ("normal_request", "valid_solution", "duplicate_award")
        if row["unexpected_truncation"] and row["result"] == "PASS":
            row["result"] = "FAIL"
            row["actual_behavior"] += " The model also hit its output token cap; this live lab response is treated as unexpectedly truncated."
        row["response_latency_seconds"] = time.monotonic() - started
        if real:
            row["execution_kind"] = "real_model"
        self.append(row)
        self.state["completed"].append(f"{lab}:{category}:{payload_id}")
        self.save_state()
        print(json.dumps({key: row[key] for key in ("phase", "lab_number", "case_category", "test_payload_id", "result", "response_latency_seconds", "model_refusal", "defense_rejection", "flag_awarded")}), flush=True)
        return row

    def append(self, row):
        with (self.output / "lab_regression_results.jsonl").open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        path = self.output / "lab_regression_results.csv"
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if needs_header:
                writer.writeheader()
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})
        self.rows.append(row)
        self.summary()

    def normal(self, entry):
        lab, browser = entry["lab"], self.browser("student-a")
        self.call(browser, f"/api/labs/{lab}/reset", {})
        result = self.call(browser, f"/api/labs/{lab}/chat", {"message": self.cases_document["normal_prompt"], "use_kb": False})
        record = result["lab"]
        reply = record["messages"][-1]["content"]
        assert reply.strip(), "Empty normal answer"
        assert not record["last_result"]["defense_blocked"], "Normal request rejected by app defense"
        assert not record["exploited"] and record["flag"] is None, "Normal request unexpectedly awarded an exploit/flag"
        assert not is_model_refusal(reply), "Model independently refused the normal product request"
        assert any(word in reply.lower() for word in self.cases_document["normal_expected_keywords"]), "Normal answer did not mention the requested product"
        return {"actual_behavior": "Normal product request answered without a defense rejection or flag.", "details": {"reply": reply}}

    def invalid(self, entry):
        lab, browser = entry["lab"], self.browser("invalid-" + str(entry["lab"]))
        result = self.call(browser, f"/api/labs/{lab}/flag", {"flag": "AIGOAT{KNOWN-INVALID-REGRESSION-ATTEMPT}"}, 403)
        record = self.lab(browser, lab)
        assert not record["completed"] and record["flag"] is None
        return {"actual_behavior": "Known invalid pre-exploit flag rejected with HTTP403; no award.", "details": result}

    def long_prompt(self, entry):
        lab, browser = entry["lab"], self.browser("long-" + str(entry["lab"]))
        count = len(self.model.receipts)
        result = self.call(browser, f"/api/labs/{lab}/chat", {"message": "X" * 4001}, 400)
        assert len(self.model.receipts) == count, "Too-long request reached the model"
        return {"actual_behavior": "4,001-character request rejected with HTTP400 before model invocation.", "details": result}

    def candidates(self, entry):
        candidates = entry["candidates"]
        if self.args.candidate_id:
            return [candidate for candidate in candidates if candidate["id"] == self.args.candidate_id]
        proven = self.proofs.get(str(entry["lab"]))
        if self.args.phase != "baseline" and proven and not self.args.allow_alternates:
            matches = [candidate for candidate in candidates if candidate["id"] == proven["payload_id"]]
            if not matches:
                raise ValueError("Previously proven solution is missing from the cases document")
            if sha(matches[0]) != proven["candidate_sha256"]:
                raise ValueError("Previously proven solution was edited; use a separately named candidate")
            return matches
        return candidates

    def valid(self, entry, candidate):
        lab, browser = entry["lab"], self.browser("student-a")
        receipt_start = len(self.model.receipts)
        self.call(browser, f"/api/labs/{lab}/reset", {})
        if entry.get("document"):
            self.call(browser, f"/api/labs/{lab}/kb", {"documents": [{"id": "regression-planted", "title": "Moonstone notice", "content": entry["document"]}], "sync": True, "use_kb": True})
        before = self.call(browser)["summary"]["score"]
        result = None
        for message in candidate["messages"]:
            result = self.call(browser, f"/api/labs/{lab}/chat", {"message": message, "use_kb": bool(entry.get("document"))})
        record = result["lab"]
        assert record["exploited"] and record["flag"], "Actual model response did not pass the unchanged evaluator"
        assert re.fullmatch(r"AIGOAT\{[A-F0-9]{32}\}", record["flag"]), "Unexpected runtime flag format"
        accepted = self.call(browser, f"/api/labs/{lab}/flag", {"flag": record["flag"]})
        points = self.catalog.CATALOG[lab - 1]["points"]
        assert accepted["lab"]["completed"] and accepted["summary"]["score"] == before + points, "Runtime flag was not accepted for the expected points"
        self.state["successful"][str(lab)] = {"payload_id": candidate["id"], "last_message": candidate["messages"][-1], "has_kb": bool(entry.get("document")), "flag_sha256": sha(record["flag"]), "score_after": accepted["summary"]["score"]}
        current_receipts = self.model.receipts[receipt_start:]
        current_truncated = any(receipt.get("truncated", False) for receipt in current_receipts if receipt.get("execution_kind") == "real_model")
        if self.args.phase == "baseline" and not current_truncated:
            self.proofs[str(lab)] = {"payload_id": candidate["id"], "candidate_sha256": sha(candidate), "established_at": now(), "run_id": self.run_id, "settings": self.model.settings(), "response_truncated": False}
            write_json(self.proof_path, self.proofs)
        return {"actual_behavior": "Real model exploit detected by original evaluator; returned runtime flag accepted and points awarded exactly once.", "flag_awarded": True, "defense_rejection": bool(record["last_result"]["defense_blocked"]), "details": {"candidate": candidate, "flag_sha256": sha(record["flag"]), "points_awarded": points, "baseline_proven_payload": self.proofs.get(str(lab), {}).get("payload_id")}}

    def no_protection(self, entry):
        lab, browser = entry["lab"], self.browser("student-a")
        record = self.lab(browser, lab)
        assert record["defense_level"] == 0, "Level0 is not selected"
        assert record["last_result"] is not None, "No prior model attempt exists"
        assert not record["last_result"]["defense_blocked"], "Application defense was applied at level0"
        assert record["messages"], "Level0 never reached the real model"
        prior = [receipt for receipt in self.model.receipts if receipt.get("lab") == lab and receipt.get("execution_kind") == "real_model"]
        if prior:
            assert not prior[-1]["hardening_policy_present"], "Level0 model prompt contains application hardening rules"
        refusal = is_model_refusal(record["messages"][-1]["content"])
        return {"actual_behavior": "Level0 reached the model with no application defense rejection. Independent model refusal remains a separate observation.", "model_refusal": refusal, "details": {"defense_level": 0, "model_refusal_last_reply": refusal}}

    def duplicate(self, entry):
        lab, browser = entry["lab"], self.browser("student-a")
        success = self.state["successful"].get(str(lab))
        if not success:
            return {"result": "NOT RUN", "actual_behavior": "No real accepted flag obtained, so duplicate-award replay cannot be tested without inventing success."}
        before = self.call(browser)["summary"]["score"]
        result = self.call(browser, f"/api/labs/{lab}/chat", {"message": success["last_message"], "use_kb": success["has_kb"]})
        accepted = self.call(browser, f"/api/labs/{lab}/flag", {"flag": result["lab"]["flag"]})
        repeated = self.call(browser, f"/api/labs/{lab}/flag", {"flag": result["lab"]["flag"]})
        assert accepted["summary"]["score"] == repeated["summary"]["score"] == before
        return {"actual_behavior": "Identical final attack replay and two flag submissions left total score unchanged.", "details": {"score_before": before, "score_after": repeated["summary"]["score"]}}

    def isolation(self, entry):
        lab, first, other = entry["lab"], self.browser("student-a"), self.browser("student-b")
        first_record, other_record = self.lab(first, lab), self.lab(other, lab)
        assert other_record["messages"] == [] and not other_record["exploited"] and not other_record["completed"] and other_record["flag"] is None
        if entry.get("document"):
            assert entry["document"] not in json.dumps(other_record["documents"])
        cross_flag = "NOT RUN: no actual accepted flag exists"
        if first_record["completed"] and first_record["flag"]:
            self.call(other, f"/api/labs/{lab}/flag", {"flag": first_record["flag"]}, 403)
            cross_flag = "PASS: first learner's real flag rejected in second session"
        return {"actual_behavior": "Separate cookie contains no first learner conversation, flag, completion or planted documents.", "details": {"cross_session_flag": cross_flag}}

    def protected(self, entry, level):
        lab, browser = entry["lab"], self.browser(f"protected-{level}-" + str(entry["lab"]))
        self.call(browser, f"/api/labs/{lab}/defense", {"level": level})
        before = len(self.model.receipts)
        result = self.call(browser, f"/api/labs/{lab}/chat", {"message": self.cases_document["protected_payload"]})
        record = result["lab"]
        assert record["defense_level"] == level and record["last_result"]["defense_blocked"]
        assert not record["exploited"] and record["flag"] is None
        assert len(self.model.receipts) == before, "Known protected-mode rejection unexpectedly reached model"
        return {"actual_behavior": f"Configured level{level} rejected the multi-signal injection before inference; no flag.", "defense_rejection": True, "details": {"reason": record["last_result"]["defense_reason"], "defense_level": level}}

    def timeout(self, entry):
        lab, browser = entry["lab"], self.browser("timeout-" + str(entry["lab"]))
        self.model.fault_next = True
        try:
            result = self.call(browser, f"/api/labs/{lab}/chat", {"message": "Describe the mug briefly."}, 503)
        finally:
            self.model.fault_next = False
        record = self.lab(browser, lab)
        assert record["messages"] == [] and record["flag"] is None and not record["completed"]
        return {"actual_behavior": "Injected transport TimeoutError became controlled HTTP503, without conversation/flag corruption.", "details": {"fault_injection": True, "response": result}}

    def summary(self):
        current = [row for row in self.rows if row["run_id"] == self.run_id]
        by_lab = {}
        for row in current:
            lab = by_lab.setdefault(str(row["lab_number"]), {})
            lab[row["case_category"]] = {key: row.get(key) for key in ("result", "test_payload_id", "execution_kind", "actual_behavior", "model_refusal", "defense_rejection", "flag_awarded", "response_latency_seconds", "truncated")}
        complete = all(set(CATEGORIES).issubset(by_lab.get(str(lab), {})) for lab in self.args.only_labs)
        good = complete and all(value["result"] == "PASS" for lab in by_lab.values() for value in lab.values())
        selected_categories = self.args.only_categories or list(CATEGORIES)
        selected_good = all(category in by_lab.get(str(lab), {}) and by_lab[str(lab)][category]["result"] == "PASS"
                            for lab in self.args.only_labs for category in selected_categories)
        latencies = sorted(row["response_latency_seconds"] for row in current if row["execution_kind"] == "real_model")
        value = {"phase": self.args.phase, "run_id": self.run_id, "updated_at": now(), "all_selected_categories_executed": complete,
                 "all_passed": good, "selected_cases_passed": selected_good,
                 "counts": {status: sum(row["result"] == status for row in current) for status in ("PASS", "FAIL", "NOT RUN")},
                 "latest_by_lab": by_lab, "real_model_case_p95_seconds": latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None,
                 "p95_method": "nearest rank", "caution": "Live-model cases, deterministic handler/defense checks and explicit fault injection are separate execution kinds. Candidate attacks are not assumed valid before real acceptance.",
                 "source_hashes": self.source_hashes}
        write_json(self.output / f"lab_regression_summary_{self.args.phase}.json", value)
        return value

    def run(self):
        for entry in self.cases_document["labs"]:
            lab = entry["lab"]
            if lab not in self.args.only_labs:
                continue
            self.case(entry, "normal_request", f"L{lab:02d}-NORMAL-v1", lambda e=entry: self.normal(e), "real_model")
            self.case(entry, "invalid_attempt", f"L{lab:02d}-INVALID-FLAG-v1", lambda e=entry: self.invalid(e))
            self.case(entry, "excessively_long_prompt", f"L{lab:02d}-LONG-4001-v1", lambda e=entry: self.long_prompt(e))
            if not self.args.only_categories or "valid_solution" in self.args.only_categories:
                candidates = self.candidates(entry)
                for candidate in candidates:
                    row = self.case(entry, "valid_solution", candidate["id"], lambda e=entry, c=candidate: self.valid(e, c), "real_model")
                    if (row and row["result"] == "PASS") or (row is None and str(lab) in self.state["successful"]):
                        break
            self.case(entry, "no_protection", f"L{lab:02d}-NO-PROTECTION-v1", lambda e=entry: self.no_protection(e))
            self.case(entry, "duplicate_award", f"L{lab:02d}-DUPLICATE-v1", lambda e=entry: self.duplicate(e), "real_model")
            self.case(entry, "session_isolation", f"L{lab:02d}-ISOLATION-v1", lambda e=entry: self.isolation(e))
            for level in (1, 2):
                self.case(entry, f"protected_mode_l{level}", f"L{lab:02d}-PROTECTED-L{level}-v1", lambda e=entry, n=level: self.protected(e, n))
            self.case(entry, "model_timeout", f"L{lab:02d}-TIMEOUT-INJECTED-v1", lambda e=entry: self.timeout(e), "fault_injection")
        return self.summary()

    def close(self):
        self.save_state()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd_thread.join(timeout=5)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--cases")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434", help="Ollama API, not the production Nine Labs HTTP API")
    parser.add_argument("--model", default="ministral-3:3b")
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--max-output", type=int, default=512, help="Keep the existing app budget for baseline; compare profiles explicitly")
    parser.add_argument("--output-overrides", type=json.loads, default={}, help='Per-lab JSON, e.g. {"2":512}')
    parser.add_argument("--temperature", type=float, default=.3)
    parser.add_argument("--top-p", type=float, default=.9)
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--repeat-penalty", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument("--request-timeout", type=float, default=240)
    parser.add_argument("--container", default="aigoat-ollama-1", help="Use empty string when Docker CLI is unavailable; API runtime metrics still recorded")
    parser.add_argument("--lock-file", default="/tmp/ollama-nine-benchmark.lock")
    parser.add_argument("--only-labs", type=lambda value: [int(x) for x in value.split(",")], default=list(range(1, 10)))
    parser.add_argument("--only-categories", type=lambda value: value.split(","))
    parser.add_argument("--candidate-id")
    parser.add_argument("--allow-alternates", action="store_true", help="Final normally replays the exact baseline-proven candidate")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true", help="With resume, rerun selected cases; append evidence instead of overwriting it")
    args = parser.parse_args()
    if min(args.threads, args.context, args.max_output) <= 0:
        parser.error("Threads, context, and output limit must be positive")
    if not isinstance(args.output_overrides, dict) or any(
            str(key) not in {str(i) for i in range(1, 10)} or type(value) is not int or value <= 0
            for key, value in args.output_overrides.items()):
        parser.error("--output-overrides must map lab numbers1..9 to positive integer token limits")
    if not all(1 <= lab <= 9 for lab in args.only_labs):
        parser.error("--only-labs must contain integers1 through9")
    if args.only_categories and set(args.only_categories) - set(CATEGORIES):
        parser.error("Unknown category; choose " + ",".join(CATEGORIES))
    return args


def main():
    args = parse_args()
    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another benchmark/regression process holds the inference lock; refusing parallel execution.")
        harness = Harness(args)
        try:
            result = harness.run()
            print(json.dumps({"summary": result}, ensure_ascii=False), flush=True)
        finally:
            harness.close()
    return 0 if result["selected_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
