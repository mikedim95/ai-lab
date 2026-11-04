#!/usr/bin/env python3
"""Sequential, read-only-configuration Ollama benchmark (Python stdlib only).

Run on the Docker host, pointing --base-url at a private forwarding endpoint for
the existing Ollama service. This script never creates/deletes models, changes
Compose files, recreates containers, or writes application databases. Benchmark
options temporarily select an in-memory runner; the original model parameters
are preloaded again in finally unless --no-restore is explicitly requested.

Examples:
  python3 scripts/ollama_benchmark.py --phase plan
  python3 scripts/ollama_benchmark.py --base-url http://127.0.0.1:19034 \
      --output-dir . --phase baseline --phase threads
  python3 scripts/ollama_benchmark.py --base-url http://127.0.0.1:19034 \
      --output-dir . --threads 16 --workload-file performance/workloads.json \
      --phase context --phase output --phase workloads --phase reliability

The workload file accepts {"P3": {"document": "...", "question": "...",
"document_tokens": 2000, "document_sha256": "...", "tokenizer_evidence":
{...}}}. P1/P2 are fixed by the task and cannot be overridden. A P3 document is
not claimed to be 2,000 tokens by estimating its words: its exact native-model
tokenization evidence must be prepared separately and supplied in this file.

There is one inference call at a time. Docker stats monitoring is read-only and
is not another inference/test. The flock is shared with the regression harness.
Production clients still need to be quiesced externally: an idle CPU check can
detect existing computation, but cannot stop another client arriving later.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


P1 = "Explain what prompt injection is in two sentences."
P2 = ("Explain the difference between direct and indirect prompt injection, "
      "including three technical examples and the trust boundaries involved.")
FIXED_OUTPUT = ("Output the integers from 1 through 1000 in ascending order, "
                "separated by single spaces. Do not add a heading, explanation, "
                "ellipsis, or closing sentence. Continue the sequence.")
P95_METHOD = "nearest rank: sorted[ceil(0.95*n)-1]; for n=5 this is the maximum"
FIELDS = [
    "session_id", "run_id", "phase", "test_id", "workload_id", "run_index",
    "is_warmup", "model", "threads", "context", "output_limit", "temperature",
    "top_p", "top_k", "repeat_penalty", "seed", "think", "keep_alive",
    "options_json", "prompt_sha256", "document_sha256", "document_token_count",
    "success", "error", "error_type", "timed_out", "total_duration_s",
    "load_duration_s", "prompt_eval_count", "prompt_eval_cached_count", "prompt_eval_duration_s", "eval_count",
    "eval_duration_s", "prompt_tokens_per_second", "tokens_per_second",
    "wall_latency_s", "ttft_s", "done_reason", "token_cap_reached",
    "truncation_reported", "peak_ram_bytes", "peak_working_set_bytes",
    "mean_cpu_percent", "peak_cpu_percent", "monitor_error",
    "container_restarts", "oom_killed", "unexpected_reload",
    "model_present_before", "model_present_after", "runner_args_json", "cache_policy",
    "cache_clear_result_json", "phase_audit_status", "phase_contaminated",
    "extra_inference_requests",
]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def quantile95(values):
    return sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)] if values else None


def describe(values):
    return {"median": statistics.median(values), "minimum": min(values),
            "maximum": max(values), "p95": quantile95(values)} if values else None


class API:
    def __init__(self, base_url, timeout):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return json.load(response)

    def infer(self, body):
        start = time.monotonic()
        req = urllib.request.Request(self.base_url + "/api/chat",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            if not body["stream"]:
                return json.load(response), None, time.monotonic() - start
            content, thinking, final, ttft = [], [], None, None
            for line in response:
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("error"):
                    raise RuntimeError(item["error"])
                message = item.get("message") or {}
                if message.get("content"):
                    if ttft is None:
                        ttft = time.monotonic() - start
                    content.append(message["content"])
                if message.get("thinking"):
                    thinking.append(message["thinking"])
                if item.get("done"):
                    final = item
                    break
            if final is None:
                raise RuntimeError("Stream ended without a final metrics frame")
            final["message"] = {"role": "assistant", "content": "".join(content)}
            if thinking:
                final["message"]["thinking"] = "".join(thinking)
            return final, ttft, time.monotonic() - start


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, container, socket_path):
        self.container = urllib.parse.quote(container, safe="")
        self.socket_path = socket_path

    def raw(self, suffix):
        connection = UnixConnection(self.socket_path)
        try:
            connection.request("GET", "/containers/" + self.container + suffix)
            response = connection.getresponse()
            data = response.read()
            if response.status != 200:
                raise RuntimeError(f"Docker HTTP {response.status}: {data[:150]!r}")
            return data
        finally:
            connection.close()

    def get(self, suffix):
        return json.loads(self.raw(suffix))

    def inference_logs(self, start, end):
        data = self.raw("/logs?stdout=true&stderr=true&timestamps=true&since=" + str(int(start.timestamp())))
        if len(data) >= 8 and data[0] in (0, 1, 2) and data[1:4] == b"\0\0\0":
            chunks, offset = [], 0
            while offset + 8 <= len(data):
                size = int.from_bytes(data[offset + 4:offset + 8], "big")
                chunks.append(data[offset + 8:offset + 8 + size])
                offset += 8 + size
            data = b"".join(chunks)
        found = []
        pattern = re.compile(r'\bPOST\s+"?(/api/(?:chat|generate|embed|embeddings)|/v1/(?:chat/completions|completions|embeddings|responses))(?:[?"\s]|$)')
        for line in data.decode("utf-8", errors="replace").splitlines():
            match = pattern.search(line)
            if not match:
                continue
            stamp = line.split(" ", 1)[0]
            try:
                timestamp = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RuntimeError("An inference log line had no parseable Docker timestamp") from exc
            if start <= timestamp <= end:
                found.append({"timestamp": stamp, "path": match.group(1), "log": line})
        return found

    def stats(self):
        return self.get("/stats?stream=false&one-shot=true")

    def state(self):
        data = self.get("/json")
        state, host = data.get("State", {}), data.get("HostConfig", {})
        return {"status": state.get("Status"), "pid": state.get("Pid"),
                "oom_killed": state.get("OOMKilled", False),
                "restart_count": data.get("RestartCount", 0),
                "memory_limit": host.get("Memory"), "nano_cpus": host.get("NanoCpus"),
                "cpu_quota": host.get("CpuQuota"), "cpuset_cpus": host.get("CpusetCpus"),
                "image_id": data.get("Image"),
                "environment": [v for v in data.get("Config", {}).get("Env", [])
                                if v.startswith(("OLLAMA_NUM_PARALLEL=", "OLLAMA_MAX_LOADED_MODELS=",
                                                 "OLLAMA_CONTEXT_LENGTH=", "OLLAMA_KEEP_ALIVE="))]}

    def runner_args(self):
        data = self.get("/top?ps_args=" + urllib.parse.quote("-eo pid,args"))
        return [row for row in data.get("Processes", [])
                if any("llama-server" in value or "ollama runner" in value for value in row)]


def cpu_percent(current, previous=None):
    cpu = current.get("cpu_stats", {})
    old = (previous or {}).get("cpu_stats", {}) if previous else current.get("precpu_stats", {})
    delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - old.get("cpu_usage", {}).get("total_usage", 0)
    system = cpu.get("system_cpu_usage", 0) - old.get("system_cpu_usage", 0)
    cores = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", [])) or os.cpu_count() or 1
    return 100 * delta / system * cores if system > 0 and delta >= 0 else None


class Sampler:
    def __init__(self, docker, interval):
        self.docker, self.interval = docker, interval
        self.samples, self.errors, self.stop_event = [], [], threading.Event()
        self.thread = None

    def sample(self):
        try:
            data = self.docker.stats()
            memory = data.get("memory_stats", {})
            raw = memory.get("usage", 0)
            cache = memory.get("stats", {}).get("inactive_file", memory.get("stats", {}).get("total_inactive_file", 0))
            previous = self.samples[-1]["raw"] if self.samples else None
            self.samples.append({"raw": data, "memory": raw,
                                 "working": max(0, raw - cache),
                                 "cpu": cpu_percent(data, previous)})
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def start(self):
        self.sample()
        def loop():
            while not self.stop_event.wait(self.interval):
                self.sample()
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.sample()
        cpus = [s["cpu"] for s in self.samples if s["cpu"] is not None]
        return {"peak_ram_bytes": max((s["memory"] for s in self.samples), default=None),
                "peak_working_set_bytes": max((s["working"] for s in self.samples), default=None),
                "mean_cpu_percent": statistics.mean(cpus) if cpus else None,
                "peak_cpu_percent": max(cpus) if cpus else None,
                "monitor_error": "; ".join(dict.fromkeys(self.errors))}


def load_workloads(path):
    workloads = {"P1": {"prompt": P1}, "P2": {"prompt": P2},
                 "F1": {"prompt": FIXED_OUTPUT}}
    if not path:
        return workloads
    data = json.loads(Path(path).read_text())
    entries = data.get("workloads", data)
    for key, text in (("P1", P1), ("P2", P2)):
        value = entries.get(key)
        provided = value.get("prompt") if isinstance(value, dict) else value
        if provided is not None and provided != text:
            raise ValueError(f"{key} must exactly match the task's fixed prompt")
    p3 = entries.get("P3")
    if p3:
        document = p3.get("document")
        count = p3.get("document_token_count", p3.get("document_tokens", p3.get("token_count")))
        evidence = p3.get("tokenizer_evidence", p3.get("tokenization"))
        if not isinstance(document, str) or count != 2000:
            raise ValueError("P3 requires a document and its operator-verified native token count of exactly 2000")
        sha = digest(document)
        if p3.get("document_sha256", p3.get("sha256", sha)) != sha:
            raise ValueError("P3 document SHA256 does not match the supplied document")
        question = p3.get("question") or p3.get("task")
        prompt = p3.get("prompt")
        if prompt is None:
            if not question:
                raise ValueError("P3 needs a question/task or a complete prompt")
            prompt = "SECURITY OPERATIONS DOCUMENT:\n" + document + "\n\nTASK:\n" + question
        if document not in prompt:
            raise ValueError("P3's complete prompt must contain the exact supplied document")
        workloads["P3"] = {**p3, "prompt": prompt, "document_sha256": sha,
                           "document_token_count": count,
                           "tokenizer_evidence": evidence or "Exact count supplied by operator; retain native-tokenization evidence with the workload fixture"}
    return workloads


def cases_for(phase, threads, args):
    def case(test_id, context, budget, workload, nthreads=threads):
        return {"phase": phase, "test_id": test_id, "threads": nthreads,
                "context": context, "output_limit": budget, "workload_id": workload}
    if phase == "baseline":
        label = "B0" if args.baseline_predict == 256 else f"B_CURRENT{args.baseline_predict}"
        return [case(label, args.context, args.baseline_predict, "P1", args.baseline_threads)]
    if phase == "threads":
        return [case(f"T{i}", 8192, 256, "P1", value) for i, value in enumerate((8, 16, 24, 32), 1)]
    if phase == "context":
        return [case("C1", 4096, 256, "P3"), case("C2", 8192, 256, "P3")]
    if phase == "output":
        return [case(f"O{i}", 8192, budget, args.output_workload) for i, budget in enumerate((128, 256, 512), 1)]
    if phase == "workloads":
        return [case("D_" + name, 8192, 256, name) for name in ("P1", "P2", "P3")]
    if phase == "reliability":
        return [case("E1", 8192, 256, args.reliability_workloads.split(",")[0])]
    if phase == "streaming":
        return [case("S1", 8192, 256, "P2"), case("S2", 8192, 256, "P2")]
    raise ValueError(phase)


def parse_parameters(text):
    values = {}
    for line in text.splitlines():
        pieces = line.split(None, 1)
        if len(pieces) != 2:
            continue
        try:
            values[pieces[0]] = json.loads(pieces[1])
        except json.JSONDecodeError:
            values[pieces[0]] = pieces[1]
    return values


class Benchmark:
    def __init__(self, args, workloads):
        self.args, self.workloads = args, workloads
        self.api = API(args.base_url, args.timeout)
        self.docker = Docker(args.container, args.docker_socket)
        self.output = Path(args.output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.session_id = args.session_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.rows, self.not_run = [], []
        self.csv_path = self.output / "ollama_benchmark_results.csv"
        self.raw_path = self.output / "ollama_benchmark_runs.jsonl"
        self.meta_path = self.output / "ollama_benchmark_metadata.json"
        self.metadata, self.original = {}, None
        self.issued_requests = 0

    def snapshot(self):
        shown = self.api.request("/api/show", {"model": self.args.model})
        loaded = self.api.request("/api/ps")
        tags = self.api.request("/api/tags")
        selected = next((m for m in tags.get("models", []) if m.get("name") == self.args.model), None)
        if selected is None:
            raise RuntimeError("Requested model is not installed; no model will be pulled automatically")
        self.original = parse_parameters(shown.get("parameters", ""))
        self.original.setdefault("num_thread", self.args.restore_threads)
        active = next((m for m in loaded.get("models", []) if m.get("name") == self.args.model), {})
        self.original.setdefault("num_ctx", active.get("context_length", self.args.restore_context))
        state, monitor_error = None, None
        try:
            state = self.docker.state()
        except Exception as exc:
            monitor_error = str(exc)
        self.metadata = {"session_id": self.session_id, "started_at": now(),
                         "version": self.api.request("/api/version"), "model": selected,
                         "original_model_parameters": self.original,
                         "loaded_before": loaded, "container_before": state,
                         "monitor_error": monitor_error, "arguments": vars(self.args),
                         "percentile_method": P95_METHOD,
                         "peak_ram_method": "Docker memory_stats.usage sampled every interval; working set additionally subtracts inactive_file",
                         "cpu_method": "Docker CPU delta/system delta * online_cpus *100; 100% is one logical CPU",
                         "num_batch_policy": "No num_batch option sent; active runner arguments recorded after every run",
                         "concurrency_policy": "Sequential inference and global flock; root must quiesce other clients externally",
                         "phase_audits": [],
                         "prompt_processing_rate_note": "User-requested prompt_eval_count/duration formula is retained; prompt_eval_cached_count and cache policy are recorded because cache reuse affects apparent prefill rate.",
                         "hardware": {"logical_cpus": os.cpu_count(), "platform": platform.platform()},
                         "workload_hashes": {k: digest(v["prompt"]) for k, v in self.workloads.items()}}
        self.save_metadata()

    def save_metadata(self):
        data = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {"sessions": []}
        existing = [x for x in data["sessions"] if x.get("session_id") != self.session_id]
        data["sessions"] = existing + [{**self.metadata, "not_run": self.not_run}]
        self.meta_path.write_text(json.dumps(data, indent=2))

    def idle(self):
        if self.args.skip_idle_check:
            return
        deadline, warned = time.monotonic() + self.args.idle_timeout, False
        while True:
            try:
                first = self.docker.stats()
                time.sleep(0.25)
                value = cpu_percent(self.docker.stats(), first)
            except Exception as exc:
                raise RuntimeError("Cannot verify quiet Ollama; use --skip-idle-check only after external isolation: " + str(exc)) from exc
            if value is not None and value <= self.args.idle_cpu_percent:
                return
            if not warned:
                print(json.dumps({"event": "waiting_for_idle", "cpu_percent": value}), flush=True)
                warned = True
            if time.monotonic() >= deadline:
                raise RuntimeError("Existing Ollama computation did not become idle; no request was issued")
            time.sleep(1)

    def append(self, row, raw):
        new = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        with self.csv_path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in FIELDS})
            handle.flush()
            os.fsync(handle.fileno())
        with self.raw_path.open("a") as handle:
            handle.write(json.dumps({**row, "raw_response": raw}, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.rows.append(row)

    def audit_phase(self, phase, start, issued_before, row_start):
        end = dt.datetime.now(dt.timezone.utc)
        expected = self.issued_requests - issued_before
        audit = {"phase": phase, "started_at": start.isoformat(), "finished_at": end.isoformat(),
                 "expected_inference_requests": expected,
                 "run_ids": [r["run_id"] for r in self.rows[row_start:]],
                 "status": "UNVERIFIED", "extra_inference_requests": None}
        try:
            observed = self.docker.inference_logs(start, end)
            actual = len(observed)
            audit.update(observed_inference_requests=actual, observed_requests=observed,
                         extra_inference_requests=max(0, actual - expected),
                         missing_completed_request_logs=max(0, expected - actual),
                         status="VALID" if actual == expected else "INVALID")
        except Exception as exc:
            audit["error"] = f"{type(exc).__name__}: {exc}"
        for row in self.rows[row_start:]:
            row["phase_audit_status"] = audit["status"]
            row["phase_contaminated"] = audit["status"] == "INVALID"
            row["extra_inference_requests"] = audit["extra_inference_requests"]
        self.metadata.setdefault("phase_audits", []).append(audit)
        self.rewrite_audit_fields()
        self.save_metadata()
        print(json.dumps({"event": "phase_audit", **audit}), flush=True)

    def rewrite_audit_fields(self):
        updated = {r["run_id"]: r for r in self.rows}
        if self.csv_path.exists():
            with self.csv_path.open(newline="") as handle:
                old = list(csv.DictReader(handle))
            temp = self.csv_path.with_suffix(".csv.tmp")
            with temp.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
                writer.writeheader()
                for row in old:
                    writer.writerow(updated.get(row.get("run_id"), row))
            temp.replace(self.csv_path)
        if self.raw_path.exists():
            temp = self.raw_path.with_suffix(".jsonl.tmp")
            with self.raw_path.open() as source, temp.open("w") as dest:
                for line in source:
                    item = json.loads(line)
                    if item.get("run_id") in updated:
                        item.update(updated[item["run_id"]])
                    dest.write(json.dumps(item, ensure_ascii=False) + "\n")
            temp.replace(self.raw_path)

    def clear_cache(self):
        result = __import__("subprocess").run(shlex.split(self.args.cache_clear_command),
                                             capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Native cache clear helper failed: " + result.stderr[:300])
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Cache clear helper must return JSON with cleared:true") from exc
        if data.get("cleared") is not True:
            raise RuntimeError("Cache helper did not verify cleared:true")
        return data

    def run_one(self, case, index, warmup=False, workload_id=None):
        workload_id = workload_id or case["workload_id"]
        workload = self.workloads[workload_id]
        self.idle()
        options = {"num_thread": case["threads"], "num_ctx": case["context"],
                   "num_predict": case["output_limit"], "temperature": 0.3,
                   "top_p": 0.9, "top_k": 40, "repeat_penalty": 1.0, "seed": 42}
        messages = ([{"role": "system", "content": self.args.system}] if self.args.system else [])
        messages.append({"role": "user", "content": workload["prompt"]})
        stream = self.args.stream or case.get("test_id") == "S2"
        body = {"model": self.args.model, "messages": messages, "stream": stream,
                "think": False, "keep_alive": "24h", "options": options}
        row = {key: None for key in FIELDS}
        row.update({**case, "session_id": self.session_id, "run_id": uuid.uuid4().hex,
                    "workload_id": workload_id, "run_index": index, "is_warmup": warmup,
                    "model": self.args.model, "temperature": 0.3, "top_p": 0.9,
                    "top_k": 40, "repeat_penalty": 1.0, "seed": 42, "think": False,
                    "keep_alive": "24h", "options_json": json.dumps({**options, "stream": stream}, sort_keys=True),
                    "prompt_sha256": digest(workload["prompt"]),
                    "document_sha256": workload.get("document_sha256"),
                    "document_token_count": workload.get("document_token_count"),
                    "success": False, "timed_out": False,
                    "cache_policy": "preserve" if warmup else self.args.cache_policy,
                    "phase_audit_status": "UNVERIFIED", "phase_contaminated": False})
        if row["cache_policy"] == "clear-slot":
            row["cache_clear_result_json"] = json.dumps(self.clear_cache())
        raw, before_state = None, None
        try:
            before_state = self.docker.state()
        except Exception:
            pass
        try:
            before_ps = self.api.request("/api/ps")
            row["model_present_before"] = any(m.get("name") == self.args.model for m in before_ps.get("models", []))
        except Exception:
            pass
        sampler, started = Sampler(self.docker, self.args.sample_interval), time.monotonic()
        sampler.start()
        print(json.dumps({"event": "start", "phase": case["phase"], "test_id": case["test_id"],
                          "workload": workload_id, "run": index, "warmup": warmup,
                          "threads": case["threads"], "context": case["context"],
                          "output_limit": case["output_limit"]}), flush=True)
        try:
            self.issued_requests += 1
            raw, ttft, wall = self.api.infer(body)
            row["wall_latency_s"], row["ttft_s"] = wall, ttft
            if raw.get("error"):
                raise RuntimeError(raw["error"])
            if not raw.get("done"):
                raise RuntimeError("No completed response")
            for field in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
                row[field + "_s"] = raw.get(field, 0) / 1e9
            for field in ("prompt_eval_count", "eval_count"):
                row[field] = raw.get(field, 0)
            row["prompt_eval_cached_count"] = raw.get("prompt_eval_cached_count")
            if row["eval_count"] <= 0:
                raise RuntimeError("Completed request produced no output tokens")
            row["prompt_tokens_per_second"] = row["prompt_eval_count"] / row["prompt_eval_duration_s"] if row["prompt_eval_duration_s"] else None
            row["tokens_per_second"] = row["eval_count"] / row["eval_duration_s"] if row["eval_duration_s"] else None
            row["done_reason"] = raw.get("done_reason")
            row["token_cap_reached"] = row["eval_count"] >= case["output_limit"]
            row["truncation_reported"] = raw.get("done_reason") in ("length", "max_tokens", "limit")
            row["unexpected_reload"] = (not warmup and row["load_duration_s"] > self.args.reload_threshold)
            row["success"] = True
        except Exception as exc:
            row.update(error=str(exc), error_type=type(exc).__name__,
                       timed_out=isinstance(exc, (TimeoutError, socket.timeout)) or
                       isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)))
            row["wall_latency_s"] = time.monotonic() - started
        finally:
            row.update(sampler.finish())
            try:
                after = self.docker.state()
                row["container_restarts"] = max(0, after["restart_count"] - (before_state or after)["restart_count"])
                row["oom_killed"] = after["oom_killed"]
                row["runner_args_json"] = json.dumps(self.docker.runner_args())
            except Exception as exc:
                row["monitor_error"] = (row.get("monitor_error") or "") + "; state: " + str(exc)
            try:
                after_ps = self.api.request("/api/ps")
                row["model_present_after"] = any(m.get("name") == self.args.model for m in after_ps.get("models", []))
            except Exception:
                pass
            self.append(row, raw)
        print(json.dumps({"event": "result", **{k: row.get(k) for k in
              ("test_id", "workload_id", "run_index", "is_warmup", "success", "error",
               "total_duration_s", "tokens_per_second", "prompt_eval_count", "eval_count",
               "done_reason", "peak_working_set_bytes", "unexpected_reload")}}), flush=True)
        if not row["success"] and self.args.stop_on_error:
            raise RuntimeError("Stopped after failed benchmark request; completed rows were saved")
        return row

    def run_case(self, case):
        if self.args.only_test and case["test_id"] not in self.args.only_test.split(","):
            return
        required = self.args.reliability_workloads.split(",") if case["phase"] == "reliability" else [case["workload_id"]]
        missing = [name for name in required if name not in self.workloads]
        if missing:
            item = {**case, "status": "NOT RUN", "reason": "Missing prepared workload(s): " + ", ".join(missing)}
            self.not_run.append(item)
            self.save_metadata()
            print(json.dumps(item), flush=True)
            return
        self.run_one(case, 0, warmup=True)
        count = self.args.reliability_count if case["phase"] == "reliability" else self.args.runs
        for index in range(1, count + 1):
            workload = required[(index - 1) % len(required)]
            self.run_one(case, index, workload_id=workload)

    def winner(self):
        candidates = []
        for threads in (8, 16, 24, 32):
            rows = [r for r in self.rows if r["phase"] == "threads" and
                    r["threads"] == threads and not r["is_warmup"] and
                    r.get("phase_audit_status") == "VALID"]
            if len(rows) != self.args.runs or not all(r["success"] for r in rows):
                continue
            speeds = [r["tokens_per_second"] for r in rows]
            latencies = [r["total_duration_s"] for r in rows]
            candidates.append((statistics.median(speeds), -statistics.median(latencies), threads))
        return max(candidates)[2] if candidates else self.args.threads

    def summary(self):
        groups = {}
        for row in self.rows:
            key = (row["phase"], row["test_id"], row["workload_id"], row["threads"], row["context"], row["output_limit"])
            groups.setdefault(key, []).append(row)
        output = []
        for key, rows in groups.items():
            measured = [r for r in rows if not r["is_warmup"]]
            good = [r for r in measured if r["success"] and r.get("phase_audit_status") == "VALID"]
            entry = dict(zip(("phase", "test_id", "workload_id", "threads", "context", "output_limit"), key))
            entry.update(session_id=self.session_id, warmups=sum(bool(r["is_warmup"]) for r in rows),
                         measured_runs=len(measured), successes=sum(bool(r["success"]) for r in measured),
                         errors=sum(not r["success"] for r in measured), usable_for_comparison=len(good),
                         timeouts=sum(bool(r["timed_out"]) for r in measured),
                         reported_truncations=sum(bool(r["truncation_reported"]) for r in good),
                         token_cap_reached=sum(bool(r["token_cap_reached"]) for r in good),
                         unexpected_reloads=sum(bool(r["unexpected_reload"]) for r in good),
                         phase_audit_status=next((r.get("phase_audit_status") for r in rows), "UNVERIFIED"),
                         phase_contaminated=any(r.get("phase_contaminated") for r in rows),
                         percentile_method=P95_METHOD)
            for field in ("total_duration_s", "wall_latency_s", "tokens_per_second", "prompt_tokens_per_second", "eval_count", "prompt_eval_count", "ttft_s"):
                entry[field] = describe([r[field] for r in good if r.get(field) is not None])
            entry["peak_ram_bytes"] = max((r["peak_ram_bytes"] for r in rows if r.get("peak_ram_bytes") is not None), default=None)
            entry["peak_working_set_bytes"] = max((r["peak_working_set_bytes"] for r in rows if r.get("peak_working_set_bytes") is not None), default=None)
            entry["mean_cpu_percent"] = statistics.mean([r["mean_cpu_percent"] for r in good if r.get("mean_cpu_percent") is not None]) if any(r.get("mean_cpu_percent") is not None for r in good) else None
            entry["peak_cpu_percent"] = max((r["peak_cpu_percent"] for r in rows if r.get("peak_cpu_percent") is not None), default=None)
            entry["container_restarts"] = sum(r.get("container_restarts") or 0 for r in rows)
            entry["oom_killed"] = any(r.get("oom_killed") for r in rows)
            output.append(entry)
        summary_path = self.output / "ollama_benchmark_summary.json"
        previous = json.loads(summary_path.read_text()) if summary_path.exists() else {"sessions": []}
        previous["sessions"] = [s for s in previous["sessions"] if s.get("session_id") != self.session_id]
        previous["sessions"].append({"session_id": self.session_id, "groups": output, "not_run": self.not_run,
                                     "provisional_thread_winner": self.winner(), "percentile_method": P95_METHOD})
        summary_path.write_text(json.dumps(previous, indent=2))

    def restore(self):
        if self.args.no_restore or self.original is None:
            self.metadata["runner_restore"] = "Not requested (--no-restore) or no initial snapshot"
            return
        try:
            self.api.request("/api/generate", {"model": self.args.model, "keep_alive": "24h",
                                              "options": self.original})
            self.metadata["runner_restore"] = {"status": "restored", "options": self.original,
                                                "note": "In-memory preload only; saved model/application configuration never changed"}
        except Exception as exc:
            self.metadata["runner_restore"] = {"status": "FAILED", "error": str(exc), "options": self.original}
            print(json.dumps({"event": "restore_failed", "error": str(exc)}), file=sys.stderr, flush=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="ministral-3:3b")
    p.add_argument("--container", default="aigoat-ollama-1")
    p.add_argument("--docker-socket", default="/var/run/docker.sock")
    p.add_argument("--output-dir", default=".")
    p.add_argument("--workload-file")
    p.add_argument("--phase", action="append", choices=("plan", "baseline", "threads", "context", "output", "workloads", "reliability", "streaming", "all"))
    p.add_argument("--threads", type=int, default=16)
    p.add_argument("--context", type=int, default=8192)
    p.add_argument("--baseline-threads", type=int, default=16)
    p.add_argument("--baseline-predict", type=int, default=256)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--reliability-count", type=int, default=30)
    p.add_argument("--reliability-workloads", default="P1,P2,P3")
    p.add_argument("--output-workload", choices=("P2", "F1"), default="P2")
    p.add_argument("--system", default="")
    p.add_argument("--stream", action="store_true", help="Explicit separate streaming experiment; recorded in options_json")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--sample-interval", type=float, default=0.5)
    p.add_argument("--cache-policy", choices=("preserve", "clear-slot"), default="preserve")
    p.add_argument("--cache-clear-command", help="Validated native helper command returning JSON {cleared:true}; used only with clear-slot")
    p.add_argument("--idle-timeout", type=float, default=120)
    p.add_argument("--idle-cpu-percent", type=float, default=10)
    p.add_argument("--skip-idle-check", action="store_true", help="Requires root to ensure external client quiescence")
    p.add_argument("--reload-threshold", type=float, default=0.5)
    p.add_argument("--restore-threads", type=int, default=16)
    p.add_argument("--restore-context", type=int, default=8192)
    p.add_argument("--no-restore", action="store_true")
    p.add_argument("--session-id")
    p.add_argument("--only-test", help="Comma-separated test IDs for an explicitly scoped run")
    p.add_argument("--lock-file", default="/tmp/ollama-nine-benchmark.lock")
    p.add_argument("--stop-on-error", action="store_true")
    return p


def main():
    args = parser().parse_args()
    phases = args.phase or ["plan"]
    if "all" in phases:
        phases = ["baseline", "threads", "context", "output", "workloads", "reliability"]
    workloads = load_workloads(args.workload_file)
    if phases == ["plan"]:
        print(json.dumps({"cases": [c for phase in ("baseline", "threads", "context", "output", "workloads", "reliability")
                                    for c in cases_for(phase, args.threads, args)],
                          "available_workloads": list(workloads), "percentile_method": P95_METHOD,
                          "production_writes": False}, indent=2))
        return 0
    if "plan" in phases:
        raise SystemExit("--phase plan cannot be combined with executed phases")
    if args.runs < 1 or args.reliability_count < 1 or args.sample_interval <= 0:
        raise SystemExit("Run counts and sampling interval must be positive")
    if args.cache_policy == "clear-slot" and not args.cache_clear_command:
        raise SystemExit("clear-slot requires an operator-validated --cache-clear-command")
    lock = open(args.lock_file, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another benchmark/regression process holds " + args.lock_file)
    benchmark = Benchmark(args, workloads)
    try:
        benchmark.snapshot()
        benchmark.idle()
        chosen = args.threads
        for phase in phases:
            start = dt.datetime.now(dt.timezone.utc)
            issued_before, row_start = benchmark.issued_requests, len(benchmark.rows)
            try:
                for case in cases_for(phase, chosen, args):
                    benchmark.run_case(case)
            finally:
                benchmark.audit_phase(phase, start, issued_before, row_start)
                benchmark.summary()
            if phase == "threads":
                chosen = benchmark.winner()
                print(json.dumps({"event": "provisional_thread_winner", "threads": chosen,
                                  "selection": "Highest median generation throughput; median total latency breaks ties; all measured requests must succeed"}), flush=True)
        benchmark.metadata["status"] = "finished"
    except BaseException as exc:
        benchmark.metadata["status"] = "interrupted_or_failed"
        benchmark.metadata["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        benchmark.summary()
        benchmark.restore()
        benchmark.metadata["finished_at"] = now()
        benchmark.save_metadata()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
