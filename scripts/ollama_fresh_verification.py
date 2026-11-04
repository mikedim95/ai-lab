#!/usr/bin/env python3
"""Paired fresh-prompt CPU verification; never edits production configuration.

Default invocation only prints a plan. --execute is required for inference.
The existing model is explicitly unloaded and empty-preloaded before EVERY
timed request. These expected in-memory runner reloads happen outside the
measured request; they are not part of the separate 30-request reliability run.
An API cached-token counter of exactly zero is mandatory for a usable fresh run.

Example on the Docker host:
  python3 scripts/ollama_fresh_verification.py --execute \
    --base-url http://172.21.0.2:11434 \
    --workload-file performance/benchmark_workloads.json \
    --output-dir performance/final-verification/fresh

Extra candidate counts require explicit selection: --threads 20,22.
Optional bounded 32-thread diagnostics: --diagnostic32-runs 3 (three short,
one long, no diagnostic warm-ups, separate 128-token diagnostic cap by default).
All calls are sequential under the same lock as the original harnesses. The
original stored model parameters are preloaded in finally, without modifying
the model tag, Compose files, containers, volumes, or learner databases.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
import uuid


EXTRA_FIELDS = [
    "pair_id", "fresh_workload", "prompt_variant", "uncached_verified",
    "expected_kv_reset", "reset_duration_s", "unload_duration_s",
    "preload_duration_s", "runner_pid_before_reset", "runner_pid_after_preload",
    "reset_info_json", "request_started_at", "request_finished_at",
    "host_steal_percent", "host_busy_percent", "host_loadavg1_peak",
    "host_runnable_peak", "host_cpu_samples_json",
    "runner_cpu_seconds", "runner_sched_run_seconds_sum",
    "runner_sched_wait_seconds_sum", "runner_voluntary_switches",
    "runner_involuntary_switches", "runner_thread_count_peak",
    "runner_schedstats_enabled", "runner_evidence_json", "runner_monitor_error",
]


def load_library(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise SystemExit("Existing benchmark library not found: " + str(path) + "; use --library.")
    spec = importlib.util.spec_from_file_location("ollama_existing_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.FIELDS = list(module.FIELDS) + EXTRA_FIELDS
    return module


def host_sample():
    stat = Path("/proc/stat").read_text().splitlines()
    aggregate = next(line for line in stat if line.startswith("cpu ")).split()[1:]
    ticks = [int(value) for value in aggregate]
    load = Path("/proc/loadavg").read_text().split()
    result = {"monotonic": time.monotonic(), "cpu_ticks": ticks,
              "loadavg1": float(load[0]), "runnable": int(load[3].split("/")[0])}
    pressure = Path("/proc/pressure/cpu")
    if pressure.is_file():
        result["cpu_pressure"] = pressure.read_text().strip()
    return result


def runner_pids(docker):
    return sorted(str(row[0]) for row in docker.runner_args())


def process_snapshot(pid):
    """Read procfs only; context-switch sums include the individual worker TIDs."""
    path = Path("/proc") / str(pid)
    stat = path.joinpath("stat").read_text().rsplit(") ", 1)[1].split()
    value = {"pid": str(pid), "user_ticks": int(stat[11]), "system_ticks": int(stat[12]),
             "threads": int(stat[17]), "start_ticks": int(stat[19]), "thread_stats": {}}
    for task in path.joinpath("task").iterdir():
        try:
            status = {}
            for line in task.joinpath("status").read_text().splitlines():
                if line.startswith(("voluntary_ctxt_switches:", "nonvoluntary_ctxt_switches:")):
                    key, number = line.split(":", 1)
                    status[key] = int(number.strip())
            sched = [int(n) for n in task.joinpath("schedstat").read_text().split()]
            value["thread_stats"][task.name] = {"voluntary": status.get("voluntary_ctxt_switches"),
                                               "involuntary": status.get("nonvoluntary_ctxt_switches"),
                                               "run_ns": sched[0], "wait_ns": sched[1], "slices": sched[2]}
        except FileNotFoundError:
            # A worker TID can end while procfs is read. This remains visible as
            # a boundary difference rather than inventing a zero observation.
            continue
    return value


def build_classes(base):
    class HostSampler(base.Sampler):
        def __init__(self, docker, interval):
            self.host_samples = []
            self.runner_before, self.runner_after, self.runner_errors = {}, {}, []
            super().__init__(docker, interval)

        def start(self):
            for pid in getattr(self.docker, "fresh_runner_pids", []):
                try:
                    self.runner_before[pid] = process_snapshot(pid)
                except Exception as error:
                    self.runner_errors.append("before " + str(pid) + ": " + str(error))
            super().start()

        def sample(self):
            try:
                self.host_samples.append(host_sample())
            except Exception as error:
                self.errors.append("host monitor: " + type(error).__name__ + ": " + str(error))
            super().sample()

        def finish(self):
            result = super().finish()
            samples = self.host_samples
            result.update(host_steal_percent=None, host_busy_percent=None,
                          host_loadavg1_peak=max((s["loadavg1"] for s in samples), default=None),
                          host_runnable_peak=max((s["runnable"] for s in samples), default=None),
                          host_cpu_samples_json=json.dumps(samples))
            if len(samples) >= 2:
                start, end = samples[0]["cpu_ticks"], samples[-1]["cpu_ticks"]
                # Linux guest fields are already included in user/nice; use only
                # the first eight fields to avoid double-counting guest time.
                delta = [b - a for a, b in zip(start[:8], end[:8])]
                total = sum(delta)
                if total > 0 and len(delta) >= 8:
                    result["host_steal_percent"] = 100 * delta[7] / total
                    result["host_busy_percent"] = 100 * (total - delta[3] - delta[4] - delta[7]) / total
            for pid in getattr(self.docker, "fresh_runner_pids", []):
                try:
                    self.runner_after[pid] = process_snapshot(pid)
                except Exception as error:
                    self.runner_errors.append("after " + str(pid) + ": " + str(error))
            cpu_ticks, run_ns, wait_ns, voluntary, involuntary, missing_tids = 0, 0, 0, 0, 0, []
            pairs = set(self.runner_before) & set(self.runner_after)
            for pid in pairs:
                first, last = self.runner_before[pid], self.runner_after[pid]
                if first["start_ticks"] != last["start_ticks"]:
                    self.runner_errors.append("PID reused: " + str(pid))
                    continue
                cpu_ticks += last["user_ticks"] + last["system_ticks"] - first["user_ticks"] - first["system_ticks"]
                common = set(first["thread_stats"]) & set(last["thread_stats"])
                missing_tids += sorted(set(first["thread_stats"]) ^ set(last["thread_stats"]))
                for tid in common:
                    before, after = first["thread_stats"][tid], last["thread_stats"][tid]
                    run_ns += after["run_ns"] - before["run_ns"]
                    wait_ns += after["wait_ns"] - before["wait_ns"]
                    if before["voluntary"] is not None and after["voluntary"] is not None:
                        voluntary += after["voluntary"] - before["voluntary"]
                    if before["involuntary"] is not None and after["involuntary"] is not None:
                        involuntary += after["involuntary"] - before["involuntary"]
            if missing_tids:
                self.runner_errors.append("Thread boundary differences; sums use common TIDs only: " + ",".join(missing_tids))
            observed = bool(pairs)
            sched_setting = Path("/proc/sys/kernel/sched_schedstats")
            sched_enabled = sched_setting.read_text().strip() == "1" if sched_setting.is_file() else None
            result.update(runner_cpu_seconds=cpu_ticks / os.sysconf("SC_CLK_TCK") if observed else None,
                          runner_sched_run_seconds_sum=run_ns / 1e9 if observed and sched_enabled is True else None,
                          runner_sched_wait_seconds_sum=wait_ns / 1e9 if observed and sched_enabled is True else None,
                          runner_voluntary_switches=voluntary if observed else None,
                          runner_involuntary_switches=involuntary if observed else None,
                          runner_thread_count_peak=max((value["threads"] for value in list(self.runner_before.values()) + list(self.runner_after.values())), default=None),
                          runner_schedstats_enabled=sched_enabled,
                          runner_evidence_json=json.dumps({"before": self.runner_before, "after": self.runner_after}),
                          runner_monitor_error="; ".join(self.runner_errors))
            return result

    class TimedAPI(base.API):
        def infer(self, body):
            self.last_infer_started_at = base.now()
            try:
                return super().infer(body)
            finally:
                self.last_infer_finished_at = base.now()

    class FreshBenchmark(base.Benchmark):
        def __init__(self, args, workloads, schedule):
            super().__init__(args, workloads)
            self.api = TimedAPI(args.base_url, args.timeout)
            self.csv_path = self.output / "ollama_fresh_verification_results.csv"
            self.raw_path = self.output / "ollama_fresh_verification_runs.jsonl"
            self.meta_path = self.output / "ollama_fresh_verification_metadata.json"
            self.schedule = schedule
            self.pending = {}

        def snapshot(self):
            super().snapshot()
            self.metadata.update({
                "fresh_method": "Each timed streamed request follows an explicit model unload then empty preload at the requested profile. API cached count must equal0. Resets/preloads are outside timed inference but inside inference-log audits.",
                "expected_reload_policy": "Runner reloads between fresh requests are scheduled test preparation, not failures or reliability observations. No container restart or persistent configuration edit is requested.",
                "ttft_method": "Client wall time from POST start to first nonempty streamed content frame; excludes preceding explicit unload/preload.",
                "load_state_limitation": "Empty preload loads the model before timing, but worker/kernel graph allocation and CPU/file-cache state are not guaranteed identical to a reused production runner. This is a controlled fresh-KV comparison, not physically cold CPU caches.",
                "host_steal_method": "Delta steal ticks / delta sum of first8 aggregate /proc/stat CPU fields *100; guest fields excluded to avoid double counting.",
                "host_busy_method": "100*(total-idle-iowait-steal)/total over the monitored request; CPU pressure/load are observations, not causal proof.",
                "runner_scheduler_method": "Read-only start/end /proc/PID/stat and per-TID status/schedstat. Process CPU time uses aggregate user+system ticks. Scheduler run/wait and context-switch sums use common TIDs; boundary differences/errors are explicit. Summed worker wait can exceed wall time and is not host steal time.",
                "scheduler_statistics_setting": Path("/proc/sys/kernel/sched_schedstats").read_text().strip() if Path("/proc/sys/kernel/sched_schedstats").is_file() else "unavailable; no setting changed",
                "request_schedule": self.schedule,
                "diagnostic_policy": "Optional32-thread diagnostic rows use a separate output cap and are not an equal-cap end-to-end comparison unless configured to match.",
            })
            self.save_metadata()

        def expected_post(self, path, body):
            self.issued_requests += 1
            return self.api.request(path, body)

        def reset(self, case):
            self.idle()
            started = time.monotonic()
            event = {"started_at": base.now(), "expected": True,
                     "runner_pid_before_reset": runner_pids(self.docker)}
            unload_start = time.monotonic()
            event["unload_response"] = self.expected_post("/api/generate", {"model": self.args.model, "keep_alive": 0})
            event["unload_duration_s"] = time.monotonic() - unload_start
            deadline = time.monotonic() + self.args.reset_timeout
            while True:
                loaded = self.api.request("/api/ps").get("models", [])
                if not any(m.get("name") == self.args.model for m in loaded):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Explicit model unload did not reach an absent /api/ps state")
                time.sleep(0.1)
            options = {"num_thread": case["threads"], "num_ctx": case["context"],
                       "num_predict": case["output_limit"], "temperature": 0.3,
                       "top_p": 0.9, "top_k": 40, "repeat_penalty": 1.0, "seed": 42}
            preload_start = time.monotonic()
            event["preload_response"] = self.expected_post("/api/generate", {
                "model": self.args.model, "keep_alive": "24h", "options": options})
            event["preload_duration_s"] = time.monotonic() - preload_start
            loaded = self.api.request("/api/ps").get("models", [])
            active = [m for m in loaded if m.get("name") == self.args.model]
            if len(active) != 1 or active[0].get("context_length") != case["context"]:
                raise RuntimeError("Empty preload did not establish the expected active model/context")
            deadline = time.monotonic() + self.args.reset_timeout
            while True:
                processes = self.docker.runner_args()
                if len(processes) == 1:
                    command = " ".join(processes[0][1:])
                    if (re.search(r"(?:^|\s)-t\s+" + str(case["threads"]) + r"(?:\s|$)", command)
                            and re.search(r"(?:^|\s)-c\s+" + str(case["context"]) + r"(?:\s|$)", command)):
                        break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Empty preload did not leave exactly one runner at the requested thread/context flags")
                time.sleep(0.1)
            event["runner_pid_after_preload"] = [str(processes[0][0])]
            event["runner_pid_changed"] = event["runner_pid_before_reset"] != event["runner_pid_after_preload"]
            self.docker.fresh_runner_pids = event["runner_pid_after_preload"]
            event["reset_duration_s"] = time.monotonic() - started
            event["finished_at"] = base.now()
            return event

        def append(self, row, raw):
            row.update(self.pending)
            row["cache_policy"] = "explicit_unload_empty_preload"
            row["request_started_at"] = getattr(self.api, "last_infer_started_at", None)
            row["request_finished_at"] = getattr(self.api, "last_infer_finished_at", None)
            row["uncached_verified"] = (row.get("success") is True and
                                        row.get("prompt_eval_cached_count") == 0 and
                                        row.get("ttft_s") is not None)
            super().append(row, raw)

        def run_fresh(self, item):
            case = {key: item[key] for key in ("phase", "test_id", "threads", "context", "output_limit", "workload_id")}
            source = self.workloads["P1" if item["fresh_workload"] == "SHORT" else "P3"]
            self.workloads[item["workload_id"]] = {**source, "prompt": item["prompt"]}
            self.pending = {"pair_id": item["pair_id"], "fresh_workload": item["fresh_workload"],
                            "prompt_variant": item["run_index"], "expected_kv_reset": True}
            self.api.last_infer_started_at = self.api.last_infer_finished_at = None
            try:
                event = self.reset(case)
                self.pending.update({key: event[key] for key in ("reset_duration_s", "unload_duration_s", "preload_duration_s")})
                self.pending["runner_pid_before_reset"] = json.dumps(event["runner_pid_before_reset"])
                self.pending["runner_pid_after_preload"] = json.dumps(event["runner_pid_after_preload"])
                self.pending["reset_info_json"] = json.dumps(event)
            except Exception as error:
                row = {key: None for key in base.FIELDS}
                row.update(case, session_id=self.session_id, run_id=uuid.uuid4().hex,
                           run_index=item["run_index"], is_warmup=item["is_warmup"],
                           model=self.args.model, prompt_sha256=base.digest(item["prompt"]),
                           success=False, error="Preparation failed: " + str(error),
                           error_type=type(error).__name__, timed_out=isinstance(error, TimeoutError),
                           phase_audit_status="UNVERIFIED", phase_contaminated=False,
                           cache_policy="explicit_unload_empty_preload")
                self.append(row, None)
                raise
            row = super().run_one(case, item["run_index"], warmup=item["is_warmup"])
            print(json.dumps({"event": "fresh_result", "run_id": row["run_id"],
                              "pair_id": row["pair_id"], "threads": row["threads"],
                              "success": row["success"], "uncached_verified": row["uncached_verified"],
                              "cached_tokens": row["prompt_eval_cached_count"],
                              "ttft_s": row["ttft_s"], "host_steal_percent": row["host_steal_percent"]}), flush=True)
            return row

        def summary(self):
            groups = {}
            for row in self.rows:
                groups.setdefault((row["phase"], row["threads"], row.get("fresh_workload")), []).append(row)
            summaries = []
            for (phase, threads, workload), rows in groups.items():
                measured = [r for r in rows if not r["is_warmup"]]
                good = [r for r in measured if r.get("success") is True and r.get("uncached_verified") is True and r.get("phase_audit_status") == "VALID"]
                item = {"phase": phase, "threads": threads, "fresh_workload": workload,
                        "warmups": sum(bool(r["is_warmup"]) for r in rows),
                        "measured": len(measured), "successes": sum(r.get("success") is True for r in measured),
                        "usable_uncached": len(good),
                        "cache_verification_failures": sum(r.get("uncached_verified") is not True for r in measured),
                        "timeouts": sum(bool(r.get("timed_out")) for r in measured),
                        "runner_monitor_errors": sum(bool(r.get("runner_monitor_error")) for r in measured),
                        "expected_resets": len(rows), "percentile_method": base.P95_METHOD}
                for field in ("tokens_per_second", "total_duration_s", "wall_latency_s", "ttft_s",
                              "prompt_tokens_per_second", "eval_count", "prompt_eval_count",
                              "host_steal_percent", "host_busy_percent", "reset_duration_s",
                              "runner_cpu_seconds", "runner_sched_wait_seconds_sum",
                              "runner_voluntary_switches", "runner_involuntary_switches"):
                    item[field] = base.describe([r[field] for r in good if r.get(field) is not None])
                item["peak_ram_bytes"] = max((r["peak_ram_bytes"] for r in rows if r.get("peak_ram_bytes") is not None), default=None)
                item["peak_cpu_percent"] = max((r["peak_cpu_percent"] for r in rows if r.get("peak_cpu_percent") is not None), default=None)
                summaries.append(item)
            path = self.output / "ollama_fresh_verification_summary.json"
            old = json.loads(path.read_text()) if path.exists() else {"sessions": []}
            old["sessions"] = [s for s in old["sessions"] if s.get("session_id") != self.session_id]
            old["sessions"].append({"session_id": self.session_id, "groups": summaries})
            path.write_text(json.dumps(old, indent=2) + "\n")

    base.Sampler = HostSampler
    return FreshBenchmark


def schedule_cases(args, workloads, base):
    result = []
    def case(threads, workload, index, warmup=False, diagnostic=False):
        key = "P1" if workload == "SHORT" else "P3"
        pair = f"{args.campaign_id}:{key}:{index:02d}" + (":diagnostic" if diagnostic else "")
        prompt = "Fresh verification request ID: " + pair + ". This ID is inert metadata; do not repeat it.\n\n" + workloads[key]["prompt"]
        return {"phase": "diagnostic32" if diagnostic else "fresh_uncached", "test_id": f"{'D32' if diagnostic else 'FRESH'}_{workload}_T{threads}",
                "threads": threads, "context": args.context, "output_limit": args.diagnostic_output_limit if diagnostic else args.output_limit,
                "workload_id": key + "_FRESH", "fresh_workload": workload,
                "pair_id": pair, "run_index": index, "is_warmup": warmup,
                "prompt": prompt, "prompt_sha256": base.digest(prompt)}
    for workload in args.workload_names:
        for threads in args.thread_counts:
            result.append(case(threads, workload, 0, warmup=True))
        for index in range(1, args.runs + 1):
            offset = (index - 1) % len(args.thread_counts)
            order = args.thread_counts[offset:] + args.thread_counts[:offset]
            for threads in order:
                result.append(case(threads, workload, index))
    if args.diagnostic32_runs:
        for index in range(1, args.diagnostic32_runs + 1):
            result.append(case(32, "SHORT", index, diagnostic=True))
        for index in range(1, args.diagnostic32_long_runs + 1):
            result.append(case(32, "LONG", index, diagnostic=True))
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true", help="Explicitly permit temporary inference/reset calls; no persistent deployment")
    p.add_argument("--library", default=str(Path(__file__).with_name("ollama_benchmark.py")))
    p.add_argument("--base-url", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="ministral-3:3b")
    p.add_argument("--container", default="aigoat-ollama-1")
    p.add_argument("--docker-socket", default="/var/run/docker.sock")
    p.add_argument("--workload-file", required=True)
    p.add_argument("--output-dir", default="performance/final-verification/fresh")
    p.add_argument("--threads", default="16,24", help="Comma-separated fresh-comparison counts; add20/22 only explicitly")
    p.add_argument("--workloads", default="SHORT,LONG", help="Explicit workload subset for supplementary pilots; required16/24 comparison uses both")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--context", type=int, default=8192)
    p.add_argument("--output-limit", type=int, default=256)
    p.add_argument("--diagnostic32-runs", type=int, default=0)
    p.add_argument("--diagnostic32-long-runs", type=int, default=1)
    p.add_argument("--diagnostic-output-limit", type=int, default=128)
    p.add_argument("--campaign-id")
    p.add_argument("--session-id")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--reset-timeout", type=float, default=120)
    p.add_argument("--sample-interval", type=float, default=0.5)
    p.add_argument("--idle-timeout", type=float, default=120)
    p.add_argument("--idle-cpu-percent", type=float, default=10)
    p.add_argument("--lock-file", default="/tmp/ollama-nine-benchmark.lock")
    p.add_argument("--stop-on-error", action="store_true")
    p.set_defaults(system="", stream=True, cache_policy="explicit_unload_empty_preload",
                   cache_clear_command=None, restore_threads=16, restore_context=8192,
                   skip_idle_check=False, reload_threshold=0.5, no_restore=False)
    return p


def main():
    args = parser().parse_args()
    try:
        args.thread_counts = [int(x) for x in args.threads.split(",")]
    except ValueError:
        raise SystemExit("--threads must be comma-separated positive integers")
    if not args.thread_counts or any(x < 1 for x in args.thread_counts) or len(set(args.thread_counts)) != len(args.thread_counts):
        raise SystemExit("Thread counts must be unique positive integers")
    args.workload_names = args.workloads.split(",")
    if not args.workload_names or len(set(args.workload_names)) != len(args.workload_names) or any(x not in ("SHORT", "LONG") for x in args.workload_names):
        raise SystemExit("--workloads must select SHORT and/or LONG")
    if args.runs < 1 or args.context < 1 or args.output_limit < 1 or args.sample_interval <= 0 or args.diagnostic32_runs < 0 or args.diagnostic32_long_runs < 0:
        raise SystemExit("Counts/context/output/sample interval must be valid positive values")
    args.campaign_id = args.campaign_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    base = load_library(args.library)
    workloads = base.load_workloads(args.workload_file)
    if "P3" not in workloads:
        raise SystemExit("An exactly tokenized P3 document is required")
    schedule = schedule_cases(args, workloads, base)
    if not args.execute:
        print(json.dumps({"status": "PLAN ONLY; no inference or persistent writes", "campaign_id": args.campaign_id,
                          "temporary_expected_resets": len(schedule), "expected_POSTs_excluding_final_restore": 3 * len(schedule),
                          "schedule": [{key: value for key, value in item.items() if key != "prompt"} for item in schedule]}, indent=2))
        return 0
    lock = open(args.lock_file, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another sequential inference/test campaign holds " + args.lock_file)
    benchmark = build_classes(base)(args, workloads, schedule)
    outcome = 1
    try:
        benchmark.snapshot()
        for phase in ("fresh_uncached", "diagnostic32"):
            selected = [item for item in schedule if item["phase"] == phase]
            if not selected:
                continue
            start, issued_before, row_start = dt.datetime.now(dt.timezone.utc), benchmark.issued_requests, len(benchmark.rows)
            try:
                for item in selected:
                    benchmark.run_fresh(item)
            finally:
                benchmark.audit_phase(phase, start, issued_before, row_start)
                benchmark.summary()
        valid = (len(benchmark.rows) == len(schedule) and
                 all(r.get("success") is True and r.get("uncached_verified") is True
                     and r.get("phase_audit_status") == "VALID" and not r.get("monitor_error")
                     for r in benchmark.rows))
        benchmark.metadata["status"] = "finished" if valid else "finished_with_validation_failures"
        benchmark.metadata["all_fresh_runs_verified"] = valid
        outcome = 0 if valid else 1
    except BaseException as error:
        benchmark.metadata.update(status="interrupted_or_failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        benchmark.summary()
        benchmark.restore()
        benchmark.metadata["finished_at"] = base.now()
        benchmark.save_metadata()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
