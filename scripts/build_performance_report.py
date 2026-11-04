#!/usr/bin/env python3
"""Build the requested performance report from recorded evidence only.

This command reads artifacts and writes Markdown. It does not contact Ollama,
run a test, change a Docker service, or apply a configuration. Missing evidence
is reported as NOT RUN; incomplete and failed measurements are never invented.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any


LABS = {
    1: "Prompt Injection",
    2: "System Prompt Extraction",
    3: "RAG Knowledge Poisoning",
    4: "Context Override",
    5: "Multi-turn Escalation",
    6: "Identity Hijacking",
    7: "Authoritative Context Poisoning",
    8: "Chained KB + Injection",
    9: "Guardrail Erosion",
}
CATEGORIES = (
    "normal_request", "valid_solution", "invalid_attempt", "no_protection",
    "protected_mode_l1", "protected_mode_l2", "excessively_long_prompt",
    "model_timeout", "duplicate_award", "session_isolation",
)
PRESERVATION_KEYS = (
    "model_weights_unchanged", "quantization_unchanged", "volumes_unchanged",
    "lab_objectives_unchanged", "evaluators_unchanged", "backups_retained",
)


def scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if text.lower() in ("null", "none"):
        return None
    if not text:
        return ""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return value


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        with path.open(newline="") as stream:
            return [{key: scalar(value) for key, value in row.items()} for row in csv.DictReader(stream)]
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        rows = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and any(key in item for key in ("run_id", "lab_number", "test_payload_id")):
                rows.append(item)
        return rows
    value = load_json(path, [])
    if isinstance(value, list):
        return value
    return value.get("runs", value.get("results", [])) if isinstance(value, dict) else []


def first(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return default


def number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def duration(row: dict[str, Any], name: str) -> float | None:
    value = number(first(row, name + "_s", name + "_seconds"))
    if value is not None:
        return value
    # Only the unsuffixed Ollama API duration is nanoseconds.
    value = number(row.get(name))
    return value / 1e9 if value is not None else None


def latency(row: dict[str, Any]) -> float | None:
    return duration(row, "total_duration")


def generation_speed(row: dict[str, Any]) -> float | None:
    value = number(first(row, "tokens_per_second", "generation_tokens_per_second", "generation_tps"))
    if value is not None:
        return value
    count, seconds = number(row.get("eval_count")), duration(row, "eval_duration")
    return count / seconds if count is not None and seconds and seconds > 0 else None


def prompt_speed(row: dict[str, Any]) -> float | None:
    value = number(first(row, "prompt_tokens_per_second", "prompt_tps"))
    if value is not None:
        return value
    count, seconds = number(row.get("prompt_eval_count")), duration(row, "prompt_eval_duration")
    return count / seconds if count is not None and seconds and seconds > 0 else None


def uncached_prompt_speed(row: dict[str, Any]) -> float | None:
    """Supplement the requested raw API rate only when cache counts exist."""
    count = number(row.get("prompt_eval_count"))
    cached = number(row.get("prompt_eval_cached_count"))
    seconds = duration(row, "prompt_eval_duration")
    if count is None or cached is None or seconds is None or seconds <= 0:
        return None
    return max(0, count - cached) / seconds


def truth(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def success(row: dict[str, Any]) -> bool:
    if "success" in row:
        return truth(row["success"])
    return not first(row, "error", "error_type") and latency(row) is not None


def warmup(row: dict[str, Any]) -> bool:
    return truth(row.get("is_warmup")) or str(row.get("run_type", "")).lower() == "warmup"


def cap_reached(row: dict[str, Any]) -> bool:
    return (any(truth(row.get(key)) for key in ("token_cap_reached", "truncation_reported", "truncated"))
            or str(row.get("done_reason", "")).lower() in ("length", "max_tokens", "limit")
            or (number(row.get("output_limit")) is not None and number(row.get("eval_count")) is not None
                and number(row["eval_count"]) >= number(row["output_limit"])))


def audit_state(row: dict[str, Any]) -> str:
    if truth(row.get("phase_contaminated")):
        return "INVALID"
    return str(first(row, "phase_audit_status", default="UNVERIFIED")).upper()


def peak_ram(row: dict[str, Any]) -> float | None:
    return number(first(row, "peak_ram_bytes", "peak_container_ram_bytes", "peak_memory_bytes", "memory_peak_bytes"))


def working_ram(row: dict[str, Any]) -> float | None:
    return number(first(row, "peak_working_set_bytes", "working_set_peak_bytes"))


def runner_pids(value: Any) -> tuple[tuple[int, ...] | None, str | None]:
    """Parse Docker's recorded [[PID, command], ...] runner observations."""
    observed = scalar(value)
    if not isinstance(observed, list):
        return None, "runner_args_json is missing or is not a JSON list"
    pids = []
    for process in observed:
        if not isinstance(process, (list, tuple)) or len(process) < 2:
            return None, "runner entry is not [PID, command]"
        pid = str(process[0]).strip()
        if not pid.isdecimal() or int(pid) <= 0:
            return None, "runner PID is missing or invalid"
        if not all(isinstance(part, str) for part in process[1:]):
            return None, "runner command is not text"
        command = " ".join(process[1:])
        if "llama-server" not in command and "ollama runner" not in command:
            return None, "recorded process is not identified as an Ollama runner"
        pids.append(int(pid))
    if len(set(pids)) != len(pids):
        return None, "runner list repeats a PID"
    return tuple(sorted(pids)), None


def reliability_runner_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit observations in recorded request order, separately from API loads."""
    result = {
        "rows": len(rows), "pid_changes_after_warmup": 0,
        "missing_runner_observations": 0, "unknown_runner_observations": 0,
        "missing_model_observations": 0, "unknown_model_observations": 0,
        "sequence_errors": [], "changes": [], "ledger": [], "observed_pids": [],
        "ok": False,
    }
    if not rows:
        result["sequence_errors"].append("No reliability observations were recorded")
        return result
    if not warmup(rows[0]):
        result["sequence_errors"].append("The first recorded reliability row is not the warm-up")
    if sum(warmup(row) for row in rows) != 1:
        result["sequence_errors"].append("Exactly one initial reliability warm-up is required for the runner baseline")
    sessions = {row.get("session_id") for row in rows}
    if None in sessions or "" in sessions or len(sessions) != 1:
        result["sequence_errors"].append("The reliability sequence lacks one unambiguous session ID")
    previous_pids = None
    baseline_seen = False
    all_pids = set()
    for position, row in enumerate(rows):
        run_id = row.get("run_id", f"row-{position + 1}")
        pids, parse_error = runner_pids(row.get("runner_args_json"))
        if parse_error:
            result["unknown_runner_observations"] += 1
        elif not pids:
            result["missing_runner_observations"] += 1
        else:
            all_pids.update(pids)
            if baseline_seen and previous_pids is not None and pids != previous_pids:
                result["pid_changes_after_warmup"] += 1
                result["changes"].append({"run_id": run_id, "previous_pids": previous_pids, "current_pids": pids})
            previous_pids = pids
        if position == 0 and warmup(row):
            baseline_seen = bool(pids) and parse_error is None
        presence = {}
        for field in ("model_present_before", "model_present_after"):
            value = scalar(row.get(field))
            presence[field] = value if isinstance(value, bool) else "UNKNOWN"
            if value is False:
                result["missing_model_observations"] += 1
            elif value is not True:
                result["unknown_model_observations"] += 1
        result["ledger"].append({
            "run_id": run_id, "is_warmup": warmup(row),
            "runner_pids": pids if pids is not None else "UNKNOWN",
            "runner_observation": parse_error or ("PRESENT" if pids else "ABSENT"),
            **presence,
        })
    result["observed_pids"] = sorted(all_pids)
    result["ok"] = (baseline_seen and not result["sequence_errors"]
                    and not any(result[key] for key in (
                        "pid_changes_after_warmup", "missing_runner_observations", "unknown_runner_observations",
                        "missing_model_observations", "unknown_model_observations")))
    return result


def quantile(values: list[float], fraction: float = .95) -> float | None:
    """Nearest rank, without interpolation; n=5 p95 is the sample maximum."""
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)] if values else None


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def clean(values: list[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def format_number(value: float | None, unit: str = "", digits: int = 3) -> str:
    if value is None:
        return "NOT RUN"
    if digits == 0:
        return f"{value:.0f}{unit}"
    return f"{value:.{digits}f}{unit}"


def format_ram(value: float | None) -> str:
    return format_number(value / (1024 ** 3), " GiB") if value is not None else "NOT RUN"


def escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(escape(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def measured(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not warmup(row)]


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = measured(rows)
    api_good = [row for row in attempts if success(row)]
    good = [row for row in api_good if audit_state(row) not in ("INVALID", "FAIL", "CONTAMINATED")]
    latencies = clean([latency(row) for row in good])
    rates = clean([generation_speed(row) for row in good])
    prompts = clean([prompt_speed(row) for row in good])
    memory = clean([peak_ram(row) for row in attempts])
    working = clean([working_ram(row) for row in attempts])
    return {
        "attempts": len(attempts), "successes": len(api_good), "usable": len(good), "failures": len(attempts) - len(api_good),
        "contaminated": sum(audit_state(row) in ("INVALID", "FAIL", "CONTAMINATED") for row in attempts),
        "unverified": sum(audit_state(row) not in ("VALID", "PASS", "CLEAN", "VERIFIED", "INVALID", "FAIL", "CONTAMINATED") for row in attempts),
        "timeouts": sum(truth(row.get("timed_out")) or "timeout" in str(first(row, "error_type", "error", default="")).lower() for row in attempts),
        "warmups": sum(warmup(row) for row in rows),
        "median_tps": median(rates), "min_tps": min(rates) if rates else None, "max_tps": max(rates) if rates else None,
        "tps_cv": statistics.stdev(rates) / statistics.mean(rates) if len(rates) > 1 and statistics.mean(rates) else None,
        "median_prompt_tps": median(prompts),
        "median_cached_prompt_tokens": median(clean([number(row.get("prompt_eval_cached_count")) for row in good])),
        "median_uncached_prompt_tps": median(clean([uncached_prompt_speed(row) for row in good])),
        "median_latency": median(latencies), "mean_latency": statistics.mean(latencies) if latencies else None,
        "min_latency": min(latencies) if latencies else None, "max_latency": max(latencies) if latencies else None,
        "p95_latency": quantile(latencies),
        "peak_ram": max(memory) if memory else None, "peak_working_set": max(working) if working else None,
        "mean_cpu": median(clean([number(row.get("mean_cpu_percent")) for row in good])),
        "peak_cpu": max(clean([number(row.get("peak_cpu_percent")) for row in attempts]), default=None),
        "prompt_tokens": sorted({int(row["prompt_eval_count"]) for row in good if number(row.get("prompt_eval_count")) is not None}),
        "output_tokens": [int(row["eval_count"]) for row in good if number(row.get("eval_count")) is not None],
        "caps": sum(cap_reached(row) for row in good),
        "unexpected_reloads": sum(truth(row.get("unexpected_reload")) for row in attempts),
        "model_loads": sum((duration(row, "load_duration") or 0) >= 1 for row in attempts),
        "restarts": max([int(number(row.get("container_restarts")) or 0) for row in attempts], default=0),
        "oom": any(truth(row.get("oom_killed")) for row in attempts),
        "monitor_errors": sum(bool(row.get("monitor_error")) for row in attempts),
        "good": good,
    }


def complete(summary: dict[str, Any], expected: int = 5) -> bool:
    return summary["attempts"] == expected and summary["successes"] == expected and summary["usable"] == expected and summary["warmups"] >= 1 and not summary["contaminated"] and not summary["unverified"]


def controlled_comparison_mismatches(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    if not baseline["good"] or not candidate["good"]:
        return ["complete controlled baseline / candidate profile absent"]
    comparable_keys = ("model", "prompt_sha256", "context", "output_limit", "temperature", "top_p", "top_k", "repeat_penalty", "seed", "think", "keep_alive", "cache_policy")
    left, right = baseline["good"][0], candidate["good"][0]
    mismatches = [key for key in comparable_keys if left.get(key) != right.get(key) or left.get(key) is None or right.get(key) is None]
    left_options, right_options = scalar(left.get("options_json")), scalar(right.get("options_json"))
    if isinstance(left_options, dict) and isinstance(right_options, dict):
        left_options = {key: value for key, value in left_options.items() if key != "num_thread"}
        right_options = {key: value for key, value in right_options.items() if key != "num_thread"}
        if left_options != right_options:
            mismatches.append("request options other than num_thread (including streaming)")
    else:
        mismatches.append("complete options_json")
    return mismatches


def result_text(summary: dict[str, Any], expected: int = 5) -> str:
    if not summary["attempts"]:
        return "NOT RUN — no measured rows"
    if summary["contaminated"]:
        return f"INVALID — {summary['contaminated']} contaminated measured run(s); {summary['successes']}/{summary['attempts']} API successes"
    if summary["failures"]:
        return f"FAIL — {summary['successes']}/{summary['attempts']} API successes; {summary['timeouts']} timeout(s)"
    if summary["unverified"]:
        return f"UNVERIFIED — {summary['unverified']} measured run(s) await inference-contention audit"
    if not complete(summary, expected):
        return f"INCOMPLETE — {summary['successes']}/{expected} measured, {summary['warmups']} warm-up(s)"
    return f"COMPLETE — {summary['successes']}/{expected} measured; {summary['warmups']} warm-up(s)"


def latest_session(rows: list[dict[str, Any]], session_id: str | None = None) -> list[dict[str, Any]]:
    if not rows:
        return []
    if session_id:
        return [row for row in rows if str(row.get("session_id", "")) == session_id]
    # Repeated CLI phases can have separate sessions; keep the latest session for
    # each exact test/profile group, not one global session which hides phases.
    keys = ("phase", "test_id", "workload_id", "threads", "context", "output_limit")
    latest: dict[tuple[Any, ...], Any] = {}
    for row in rows:
        latest[tuple(row.get(key) for key in keys)] = row.get("session_id")
    return [row for row in rows if row.get("session_id") == latest[tuple(row.get(key) for key in keys)]]


def phase_rows(rows: list[dict[str, Any]], phase: str, test_id: str | None = None) -> list[dict[str, Any]]:
    selected = [row for row in rows if str(row.get("phase", "")).lower() == phase.lower()]
    return [row for row in selected if str(row.get("test_id", "")).upper() == test_id.upper()] if test_id else selected


def regression_cases(rows: list[dict[str, Any]], phase: str) -> dict[int, dict[str, dict[str, Any]]]:
    selected: dict[int, dict[str, dict[str, Any]]] = {lab: {} for lab in LABS}
    for row in rows:
        if str(row.get("phase", "")).lower() != phase.lower():
            continue
        lab = number(first(row, "lab_number", "lab"))
        if lab is not None and int(lab) in LABS:
            selected[int(lab)][str(first(row, "case_category", "category", default="unknown"))] = row
    return selected


def case_pass(row: dict[str, Any] | None, live_flag: bool = False) -> bool:
    if not row or str(row.get("result", "")).upper() != "PASS":
        return False
    if live_flag:
        return row.get("execution_kind") == "real_model" and truth(row.get("flag_awarded")) and not truth(row.get("unexpected_truncation")) and not truth(row.get("truncated"))
    return True


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def executed_prompt_hashes(row: dict[str, Any]) -> list[str] | None:
    receipts = scalar(row.get("model_metrics"))
    if row.get("execution_kind") != "real_model" or not isinstance(receipts, list) or not receipts:
        return None
    if number(row.get("model_request_count")) != len(receipts):
        return None
    hashes = []
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("execution_kind") != "real_model" or not is_sha256(receipt.get("prompt_sha256")):
            return None
        hashes.append(receipt["prompt_sha256"])
    return hashes


def proof_match_method(row: dict[str, Any] | None, proof: dict[str, Any] | None) -> str | None:
    if not row or not proof or row.get("test_payload_id") != proof.get("payload_id") or proof.get("response_truncated") is not False:
        return None
    details = scalar(row.get("details"))
    candidate = details.get("candidate") if isinstance(details, dict) else None
    if isinstance(candidate, dict):
        candidate_hash = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
        return "candidate SHA-256" if candidate_hash == proof.get("candidate_sha256") else None
    # The harness can lose details.candidate when an assertion interrupts a
    # failed replay. A missing candidate must never hide that newer failure.
    # Only FAIL rows may use the separately verified baseline request evidence;
    # missing details can never manufacture a successful flag proof.
    if row.get("case_category") != "valid_solution" or str(row.get("result", "")).upper() != "FAIL":
        return None
    request_hashes = executed_prompt_hashes(row)
    if not request_hashes or not is_sha256(row.get("case_sha256")) or not is_sha256(row.get("source_sha256")):
        return None
    for evidence in proof.get("_verified_baseline_request_evidence", []):
        baseline_hashes = evidence["prompt_sha256_sequence"]
        if (number(first(row, "lab_number", "lab")) == number(evidence["lab_number"])
                and row["case_sha256"] == evidence["case_sha256"]
                and row["source_sha256"] == evidence["source_sha256"]
                and len(request_hashes) <= len(baseline_hashes)
                and request_hashes == baseline_hashes[:len(request_hashes)]):
            return "verified baseline case/source/request hashes (executed prefix)"
    return None


def matches_proof(row: dict[str, Any] | None, proof: dict[str, Any] | None) -> bool:
    return proof_match_method(row, proof) is not None


def enrich_proofs_from_baseline(proofs: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach validated identity evidence in memory; never modify raw artifacts."""
    enriched = {}
    for lab, original in proofs.items():
        if not isinstance(original, dict):
            continue
        proof = {key: value for key, value in original.items() if key != "_verified_baseline_request_evidence"}
        evidence = []
        for row in rows:
            if (str(row.get("phase", "")).lower() != "baseline"
                    or number(first(row, "lab_number", "lab")) != number(lab)
                    or row.get("case_category") != "valid_solution"
                    or (proof.get("run_id") and row.get("run_id") != proof["run_id"])
                    or not case_pass(row, live_flag=True)
                    or proof_match_method(row, proof) != "candidate SHA-256"):
                continue
            request_hashes = executed_prompt_hashes(row)
            if request_hashes and is_sha256(row.get("case_sha256")) and is_sha256(row.get("source_sha256")):
                evidence.append({"lab_number": first(row, "lab_number", "lab"), "run_id": row.get("run_id"),
                                 "case_sha256": row["case_sha256"], "source_sha256": row["source_sha256"],
                                 "prompt_sha256_sequence": request_hashes})
        proof["_verified_baseline_request_evidence"] = evidence
        enriched[str(lab)] = proof
    return enriched


def select_established_cases(rows: list[dict[str, Any]], phase: str, proofs: dict[str, Any]) -> dict[int, dict[str, dict[str, Any]]]:
    selected = regression_cases(rows, phase)
    for lab in LABS:
        proof = proofs.get(str(lab))
        matches = [row for row in rows if str(row.get("phase", "")).lower() == phase.lower()
                   and number(first(row, "lab_number", "lab")) == lab
                   and row.get("case_category") == "valid_solution" and matches_proof(row, proof)]
        # Fix the selected candidate by immutable baseline proof, not by whichever
        # exploratory payload happened to run last. Preserve every attempt in the
        # raw ledger and do not hide later failures of that selected same payload.
        if matches:
            selected[lab]["valid_solution"] = matches[-1]
    return selected


def configuration_value(config: dict[str, Any], *names: str) -> Any:
    for item in (config, config.get("request_options", {}), config.get("ollama_request_options", {}), config.get("options", {}), config.get("ollama", {})):
        if isinstance(item, dict):
            found = first(item, *names)
            if found is not None:
                return found
    return None


def regression_profile_matches(row: dict[str, Any], model: Any, threads: Any, context: Any, default_budget: Any, per_lab_budgets: dict[str, Any]) -> bool:
    effective = scalar(row.get("effective_settings"))
    if not isinstance(effective, dict):
        return False
    options = scalar(effective.get("options"))
    if not isinstance(options, dict):
        return False
    lab = str(first(row, "lab_number", "lab"))
    budget = per_lab_budgets.get(lab, default_budget)
    return (model is not None and threads is not None and context is not None and budget is not None
            and effective.get("model") == model
            and number(options.get("num_thread")) == number(threads)
            and number(options.get("num_ctx")) == number(context)
            and number(options.get("num_predict")) == number(budget)
            and effective.get("think") is False)


def suite_kind(row: dict[str, Any]) -> str:
    explicit = str(first(row, "kind", "test_kind", default="")).lower()
    suite = str(row.get("suite", "")).lower()
    if explicit in ("original_live", "live_original") or "live_ctfs.py" in suite:
        return "original_live"
    if explicit in ("unit", "existing_unit") or "test_app.py" in suite:
        return "unit"
    return explicit or "unknown"


def latest_suite(rows: list[dict[str, Any]], phase: str, kind: str) -> dict[str, Any] | None:
    selected = [row for row in rows if str(row.get("phase", "")).lower() == phase and suite_kind(row) == kind]
    return selected[-1] if selected else None


def suite_passed(row: dict[str, Any] | None, expected: int) -> bool:
    if not row:
        return False
    passed = number(row.get("passed"))
    return (passed is not None and passed >= expected
            and number(row.get("failed")) == 0
            and (number(row.get("errors")) or 0) == 0
            and (number(row.get("skipped")) or 0) == 0
            and not row.get("error"))


def case_cell(row: dict[str, Any] | None, live_flag: bool = False) -> str:
    if not row:
        return "NOT RUN"
    status = str(row.get("result", "NOT RUN")).upper()
    if live_flag and status == "PASS" and not case_pass(row, live_flag=True):
        status = "NOT VERIFIED"
    kind = str(row.get("execution_kind", "unspecified"))
    details = []
    if truth(row.get("model_refusal")):
        details.append("model refusal")
    if truth(row.get("defense_rejection")):
        details.append("app defense rejection")
    seconds = number(row.get("response_latency_seconds"))
    text = f"{status} ({kind}"
    if seconds is not None:
        text += f", {seconds:.3f}s"
    if details:
        text += "; " + ", ".join(details)
    return text + ")"


def yaml_like(value: Any, indent: int = 0) -> list[str]:
    # JSON is valid YAML 1.2 and avoids an external serialization dependency.
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def write_report(root: Path, rows: list[dict[str, Any]], lab_rows: list[dict[str, Any]], settings: dict[str, Any], artifacts: dict[str, Path], output: Path) -> dict[str, Any]:
    rows = latest_session(rows, settings.get("benchmark_session_id"))
    baseline_rows = phase_rows(rows, "baseline", "B0")
    baseline_current = stats(phase_rows(rows, "baseline", "B_CURRENT512"))
    baseline = stats(baseline_rows)
    threads = {count: stats([row for row in phase_rows(rows, "threads") if number(row.get("threads")) == count]) for count in (8, 16, 24, 32)}
    candidates = [(count, item) for count, item in threads.items() if complete(item) and item["median_tps"] is not None]
    measured_winner = max(candidates, key=lambda pair: (pair[1]["median_tps"], -(pair[1]["median_latency"] or math.inf)))[0] if candidates else None
    selected = settings.get("selected_threads", measured_winner)
    selected_context = settings.get("selected_context", 8192)
    original_config = settings.get("original_configuration", {})
    final_config = settings.get("final_configuration", {})
    original_threads = configuration_value(original_config, "num_thread", "threads")
    final_threads = configuration_value(final_config, "num_thread", "threads")
    final_context = configuration_value(final_config, "num_ctx", "context")
    final_model = configuration_value(final_config, "model")
    final_budget = configuration_value(final_config, "nine_labs_num_predict", "num_predict", "max_tokens")
    final_per_lab_budgets = final_config.get("per_lab_output_overrides", {})
    if not isinstance(final_per_lab_budgets, dict):
        final_per_lab_budgets = {}
    final_verified = settings.get("final_configuration_verified")
    if final_verified is None:
        final_verified = settings.get("final_applied")
    final_profile_ok = (selected is not None and final_model is not None and number(final_threads) == number(selected)
                        and number(final_context) == number(selected_context)
                        and final_model == configuration_value(original_config, "model")
                        and final_budget is not None)
    optimized = threads.get(int(selected), stats([])) if selected is not None else stats([])
    def selected_rows(phase: str, test_id: str | None = None) -> list[dict[str, Any]]:
        found = phase_rows(rows, phase, test_id)
        found = [row for row in found if selected is None or number(row.get("threads")) == int(selected)]
        if phase == "reliability":
            found = [row for row in found if number(row.get("context")) == number(selected_context)]
        return found
    reliability_rows = selected_rows("reliability")
    reliability = stats(reliability_rows)
    runner_audit = reliability_runner_audit(reliability_rows)
    proofs = enrich_proofs_from_baseline(load_json(artifacts["Baseline-proven solution receipts"], {}), lab_rows)
    base_cases = select_established_cases(lab_rows, "baseline", proofs)
    final_profile_rows = [row for row in lab_rows if regression_profile_matches(row, final_model, final_threads, final_context, final_budget, final_per_lab_budgets)]
    final_cases = select_established_cases(final_profile_rows, "final", proofs)
    baseline_valid = sum(case_pass(base_cases[lab].get("valid_solution"), True) and matches_proof(base_cases[lab].get("valid_solution"), proofs.get(str(lab))) for lab in LABS)
    final_valid = sum(case_pass(final_cases[lab].get("valid_solution"), True) for lab in LABS)
    established_replays = sum(case_pass(final_cases[lab].get("valid_solution"), True) and matches_proof(final_cases[lab].get("valid_solution"), proofs.get(str(lab))) for lab in LABS)
    required_categories = settings.get("required_regression_categories", list(CATEGORIES))
    if not settings.get("protected_modes_implemented", True):
        required_categories = [category for category in required_categories if not category.startswith("protected_mode")]
    complete_labs = sum(all(case_pass(final_cases[lab].get(category), category == "valid_solution") for category in required_categories) and matches_proof(final_cases[lab].get("valid_solution"), proofs.get(str(lab))) for lab in LABS)
    preservation = settings.get("preservation", {})
    preserved = all(preservation.get(key) is True for key in PRESERVATION_KEYS)
    reliability_ok = complete(reliability, 30) and not reliability["timeouts"] and not reliability["oom"] and not reliability["restarts"] and not reliability["unexpected_reloads"] and not reliability["monitor_errors"] and runner_audit["ok"]
    existing_suites = settings.get("existing_tests", [])
    if not isinstance(existing_suites, list):
        existing_suites = []
    baseline_unit = latest_suite(existing_suites, "baseline", "unit")
    final_unit = latest_suite(existing_suites, "final", "unit")
    final_original_live = latest_suite(existing_suites, "final", "original_live")
    expected_units = int(number((baseline_unit or {}).get("passed")) or 19)
    existing_ok = (settings.get("existing_tests_passed") is True
                   and suite_passed(baseline_unit, expected_units)
                   and suite_passed(final_unit, expected_units)
                   and suite_passed(final_original_live, 9))
    lab_ok = baseline_valid == 9 and final_valid == 9 and established_replays == 9 and complete_labs == 9 and existing_ok and final_profile_ok
    truncation_status = settings.get("unexpected_application_truncation")
    truncation_ok = truncation_status is False
    comparison_mismatches = controlled_comparison_mismatches(baseline, optimized)
    performance_verified = complete(baseline) and complete(optimized) and not comparison_mismatches
    improvement = (optimized["median_tps"] / baseline["median_tps"] - 1) * 100 if performance_verified and baseline["median_tps"] else None
    reduction = (1 - optimized["median_latency"] / baseline["median_latency"]) * 100 if performance_verified and baseline["median_latency"] else None
    fastest = threads.get(measured_winner, stats([]))
    fastest_comparable = complete(baseline) and complete(fastest) and not controlled_comparison_mismatches(baseline, fastest)
    fastest_improvement = (fastest["median_tps"] / baseline["median_tps"] - 1) * 100 if fastest_comparable and baseline["median_tps"] else None
    consistency_note = settings.get("performance_consistency", "Five measured runs support descriptive comparison only; no statistical significance claim is made.")
    configuration_reason = settings.get("selection_reason", "Selection is pending review of complete measurements and lab regressions.")
    candidate_profile_cases = {}
    for count in threads:
        candidate_rows = [row for row in lab_rows if regression_profile_matches(row, final_model, count, final_context, final_budget, final_per_lab_budgets)]
        candidate_profile_cases[count] = select_established_cases(candidate_rows, "final", proofs)
    rejected_profile_labs = {
        count: [lab for lab, cases in by_lab.items()
                if matches_proof(cases.get("valid_solution"), proofs.get(str(lab)))
                and str(cases["valid_solution"].get("result", "")).upper() == "FAIL"]
        for count, by_lab in candidate_profile_cases.items()
    }
    # A faster profile with an actual failure of the established baseline
    # solution cannot displace a slower profile that preserves the labs.
    reliability_selection_ok = (settings.get("selection_reliability_justified") is True
                                and bool(settings.get("selection_reason"))
                                and bool(rejected_profile_labs.get(measured_winner)))
    # A repeated sample at the unchanged thread count is not a new threading
    # optimization, even when thermal/cache variation changes its median.
    retained_original_threads = selected is not None and number(selected) == number(original_threads)
    original_optimal_ok = (retained_original_threads and settings.get("original_demonstrated_optimal") is True
                           and all(complete(item) for item in threads.values())
                           and (number(measured_winner) == number(selected)
                                or reliability_selection_ok
                                or (settings.get("selection_stability_justified") is True and bool(settings.get("selection_reason")))))
    performance_ok = performance_verified and (original_optimal_ok or (not retained_original_threads and improvement is not None and improvement > 0))
    required_profile_stats = [stats(selected_rows(phase, test)) for phase, test in (("context", "C1"), ("context", "C2"), ("output", "O1"), ("output", "O2"), ("output", "O3"))]
    workload_stats = {name: stats([row for row in selected_rows("workloads") if row.get("workload_id") == name]) for name in ("P1", "P2", "P3")}
    fixture = settings.get("long_context_fixture", {})
    p3_rows = [row for row in rows if row.get("workload_id") == "P3" and success(row)]
    p3_hashes = {row.get("document_sha256") for row in p3_rows if row.get("document_sha256")}
    p3_counts = {number(row.get("document_token_count")) for row in p3_rows}
    fixture_ok = bool(p3_rows) and len(p3_hashes) == 1 and p3_counts == {2000.0} and fixture.get("verified_with_native_tokenizer") is True
    suite_complete = all(complete(item) for item in threads.values()) and all(complete(item) for item in required_profile_stats) and all(complete(item) for item in workload_stats.values()) and fixture_ok
    accepted = lab_ok and reliability_ok and preserved and truncation_ok and performance_ok and suite_complete and final_verified is True
    report_status = "ACCEPTED" if accepted else "NOT ACCEPTED" if suite_complete and final_verified is True else "NOT ACCEPTED / INCOMPLETE"
    issues = list(settings.get("remaining_issues", []))
    if not lab_ok:
        issues.append(f"Regression acceptance gate is not satisfied: baseline proven flags {baseline_valid}/9; final valid flags {final_valid}/9; immutable established-solution replays {established_replays}/9; complete final lab suites {complete_labs}/9; required existing baseline/final suites verified={existing_ok}.")
    if not final_profile_ok:
        issues.append("Final configuration does not yet verify the selected model/thread/context and Nine Labs output budget; final regression rows from a different profile are not counted.")
    if not existing_ok:
        issues.append("Existing-test gate requires passing baseline unit tests, final unit tests, and final unmodified tests/live_ctfs.py (9/9). Seeded proven-solution replay does not substitute for the original live script.")
    if not reliability_ok:
        issues.append("The complete 30-request reliability gate, including RAM/CPU monitoring and reload/crash checks, is not yet satisfied.")
    if not runner_audit["ok"]:
        issues.append("The independent reliability runner audit is incomplete or failed: PID transitions, runner/model absence, unknown monitoring, or sequence errors remain. See section E.")
    if not preserved:
        issues.append("At least one required preservation invariant is missing or unverified.")
    if not truncation_ok:
        issues.append("Unexpected application-level truncation is unverified or observed; intentional output-cap benchmark stops are recorded separately.")
    if not performance_ok:
        issues.append("A complete comparable baseline versus selected-profile performance result is not yet verified.")
    if comparison_mismatches:
        issues.append("Controlled baseline and selected-profile comparison is not verified; mismatched/missing settings: " + ", ".join(comparison_mismatches) + ".")
    if not suite_complete:
        issues.append("At least one required thread/context/output/workload benchmark or the exact native-tokenized P3 fixture is missing, incomplete, contaminated, or unverified.")
    if final_verified is not True:
        issues.append("Final production configuration has not been confirmed applied or retained/restored and verified; no acceptance claim is made.")
    overall_memory = max(clean([peak_ram(row) for row in rows]), default=None)
    all_lab_selected = [row for cases in final_cases.values() for row in cases.values()]
    lab_passed = sum(case_pass(row, row.get("case_category") == "valid_solution") for row in all_lab_selected)
    lab_executed = sum(str(row.get("result", "")).upper() != "NOT RUN" for row in all_lab_selected)
    production_request = scalar(final_config.get("production_request"))
    production_temperature = configuration_value(final_config, "temperature")
    if production_temperature is None and isinstance(production_request, dict):
        production_temperature = configuration_value(production_request, "temperature")
    final_summary = (f"Model {final_model}; {final_threads} threads; context {final_context}; "
                     f"Nine Labs output cap {final_budget}; temperature {production_temperature if production_temperature is not None else 'not recorded'}. "
                     "Exact environment and request settings are in section F.") if final_config else "NOT RUN — final configuration not supplied"
    lines = ["# Ollama Performance Report", "", f"Generated from recorded artifacts. Report status: **{report_status}**.", ""]
    if settings.get("captured_at"):
        lines += [f"Evidence capture: {settings['captured_at']}.", ""]
    lines += ["## A. Executive Summary", ""]
    lines.append(table(["Item", "Recorded result"], [
        ["Original configuration", compact(original_config) if original_config else "NOT RUN — original snapshot not supplied"],
        ["Final configuration", final_summary],
        ["Best thread count measured", measured_winner if measured_winner is not None else "NOT RUN — no complete thread comparison"],
        ["Selected thread count", selected if selected is not None else "NOT RUN"],
        ["Baseline median generation throughput", format_number(baseline["median_tps"], " tok/s")],
        ["Fastest candidate median generation throughput", format_number(fastest["median_tps"], " tok/s")],
        ["Fastest candidate throughput change versus B0", format_number(fastest_improvement, "%", 2) + (" — rejected for established-solution failures; no deployed gain" if rejected_profile_labs.get(measured_winner) else " — selected candidate" if number(measured_winner) == number(selected) else " — candidate result only")],
        ["Existing 512-token-override baseline median throughput", format_number(baseline_current["median_tps"], " tok/s")],
        ["Selected-profile median generation throughput", format_number(optimized["median_tps"], " tok/s")],
        ["Observed generation-throughput change", format_number(improvement, "%", 2)],
        ["Deployed threading-configuration improvement", "0% — original thread setting retained" if retained_original_threads else format_number(improvement, "%", 2)],
        ["Observed median end-to-end latency reduction (negative means increase)", format_number(reduction, "%", 2)],
        ["Deployed latency configuration gain", "0% — original threading configuration retained" if retained_original_threads else "Not isolated from response-length differences"],
        ["Comparison interpretation", "Original thread count retained; observed B0 versus T-repeat delta is not a threading-configuration gain" if retained_original_threads else "Changed thread count; response-length differences can confound total latency"],
        ["Peak recorded container RAM", format_ram(overall_memory)],
        ["Final lab tests passed", f"{lab_passed}/{lab_executed} executed selected cases; {complete_labs}/9 complete lab suites; {final_valid}/9 live valid flags"],
        ["Existing regression tests", compact(settings.get("existing_tests", "NOT RUN — no result supplied"))],
        ["Acceptance", report_status],
    ]))
    lines += ["", f"Selection rationale: {configuration_reason}", "", "Remaining issues:", ""]
    lines += [f"- {issue}" for issue in issues] if issues else ["- No remaining acceptance issues were recorded."]
    if reliability_selection_ok:
        lines += ["", f"The throughput-winning {measured_winner}-thread candidate was rejected for established-solution failures in labs **{compact(rejected_profile_labs[measured_winner])}**. The selected {selected}-thread configuration prioritizes the verified lab behavior. The faster candidate's throughput is a benchmark observation, not a deployed improvement."]
    lines += ["", "### Measurement method", "", "All inference requests are sequential. Each benchmark configuration has one warm-up followed by five measured runs; the reliability phase has one warm-up followed by 30 measured requests. Warm-ups remain in raw artifacts and are excluded from reported measured statistics. A group is marked INCOMPLETE if its required sample or warm-up count is missing.", "", "Durations are converted from Ollama API nanoseconds to seconds. Prompt throughput = `prompt_eval_count / prompt_eval_duration_seconds`; generation throughput = `eval_count / eval_duration_seconds`; end-to-end latency = `total_duration_seconds`. Wall-clock request latency and load duration remain separate raw columns. Zero-duration rates are unavailable rather than fabricated.", "", "**p95 uses nearest rank without interpolation:** sort successful measured latencies and select rank `ceil(0.95 × n)`. For five samples p95 equals the maximum; for 30 samples it is the 29th sorted latency. The report makes no population-level significance claim from five samples. Errors and timeouts are counted separately and are never silently turned into successful measurements.", "", "RAM is the sampled Docker container memory metric (`peak_ram_bytes`); working-set RAM is shown separately when captured. Sampling interval, metric interpretation, CPU percentages, background-load checks, and run-specific effective options are retained in benchmark metadata/raw rows. Docker CPU percentages can exceed 100% on a multicore container.", ""]
    if settings.get("methodology_notes"):
        lines += [str(settings["methodology_notes"]), ""]
    lines += ["The speedup comparison uses controlled baseline `B0` (256-token cap) versus the selected `T` profile with identical prompt hash, context, cap, sampling, and seed. The original application's 512-token override is separately measured as `B_CURRENT512`; its output cap is not silently substituted into an apples-to-apples thread comparison.", "", "Phase-level inference-log audits identify competing requests. Contaminated rows remain in raw artifacts but are excluded from usable statistics and winner selection. UNVERIFIED rows may show provisional observed metrics; they cannot satisfy acceptance until their phase audit completes. Cache policy, cached-token counts, and clearing results remain explicit raw fields.", ""]
    lines += ["Identical prompts/options and `seed: 42` do not establish identical generated token sequences across CPU thread settings or runner restarts. Achieved output-token counts are reported per profile. Generation throughput is compared per token; an end-to-end latency delta can also reflect a different response length and must not be attributed wholly to CPU threading.", ""]
    if retained_original_threads:
        lines += ["The selected thread count equals the original setting. Any B0-versus-selected repeat difference is an observed repeat-run delta, not evidence that changing the thread configuration made the engine faster. Acceptance on this path requires a complete comparison demonstrating that the original setting is the best verified reliable choice; a faster profile with established-solution failures is rejected.", ""]
    lines += ["## B. CPU Benchmark Results", ""]
    lines.append(table(["Threads", "Median tok/s", "Median latency", "p95 latency", "Peak RAM", "Result"], [
        [count, format_number(item["median_tps"]), format_number(item["median_latency"], " s"), format_number(item["p95_latency"], " s"), format_ram(item["peak_ram"]), result_text(item)] for count, item in threads.items()
    ]))
    lines += ["", table(["Threads", "Minimum latency", "Maximum latency", "Min–max tok/s", "Generated-token counts", "Sample throughput CV", "Median prompt tok/s"], [
        [count, format_number(item["min_latency"], " s"), format_number(item["max_latency"], " s"), f"{format_number(item['min_tps'])}–{format_number(item['max_tps'])}", compact(item["output_tokens"]) if item["output_tokens"] else "NOT RUN", format_number(item["tps_cv"] * 100 if item["tps_cv"] is not None else None, "%", 3), format_number(item["median_prompt_tps"])] for count, item in threads.items()
    ]), "", "Sample throughput CV = sample standard deviation (denominator n−1) / arithmetic mean × 100%. It describes the observed runs and does not establish population-level statistical significance.", "", f"Consistency assessment: {consistency_note}", ""]
    lines += [f"Controlled baseline B0 generated-token counts: **{compact(baseline['output_tokens']) if baseline['output_tokens'] else 'NOT RUN'}**. Original 512-token-override baseline counts: **{compact(baseline_current['output_tokens']) if baseline_current['output_tokens'] else 'NOT RUN'}**.", ""]
    lines += ["## C. Context and Output Benchmarks", ""]
    context_output = [("C1", "context", 4096, 256), ("C2", "context", 8192, 256), ("O1", "output", 8192, 128), ("O2", "output", 8192, 256), ("O3", "output", 8192, 512)]
    profile_stats = [(test, context, output_limit, stats(selected_rows(phase, test))) for test, phase, context, output_limit in context_output]
    lines.append(table(["Test ID", "Context", "Output limit", "Median tok/s", "Median latency", "Peak RAM"], [
        [test, context, output_limit, format_number(item["median_tps"]), format_number(item["median_latency"], " s"), format_ram(item["peak_ram"])] for test, context, output_limit, item in profile_stats
    ]))
    lines += ["", table(["Test ID", "Min / max / p95 latency", "Median total-count prompt tok/s (cache-dependent)", "Median cached prompt tokens", "Median uncached-count prompt tok/s (supplement)", "Generated-token counts", "Token-cap stops", "Result"], [
        [test, " / ".join(format_number(item[key], " s") for key in ("min_latency", "max_latency", "p95_latency")), format_number(item["median_prompt_tps"]), format_number(item["median_cached_prompt_tokens"], digits=0), format_number(item["median_uncached_prompt_tps"]), compact(item["output_tokens"]) if item["output_tokens"] else "NOT RUN", f"{item['caps']}/{item['successes']} successful measured runs", result_text(item)] for test, _, _, item in profile_stats
    ]), "", "Output-cap comparisons report achieved token counts and cap-stop frequency. A shorter answer caused by the 128/256-token ceiling is **not** evidence of a faster inference engine. Throughput and complete-response behavior must be considered separately.", "", "### First post-context-reload observations", ""]
    context_warmups = []
    for test, context in (("C1", 4096), ("C2", 8192)):
        context_rows = selected_rows("context", test)
        recorded_warmups = [row for row in context_rows if warmup(row)]
        observation = recorded_warmups[0] if recorded_warmups else {}
        measured_summary = stats(context_rows)
        context_warmups.append([
            test, context, observation.get("run_id", "NOT RUN"),
            format_number(number(observation.get("prompt_eval_count")), digits=0),
            format_number(duration(observation, "prompt_eval_duration"), " s"),
            format_number(prompt_speed(observation)),
            format_number(duration(observation, "load_duration"), " s"),
            format_number(latency(observation), " s"),
            format_number(measured_summary["median_prompt_tps"]),
            observation.get("prompt_eval_cached_count", "not reported") if observation else "NOT RUN",
            (f"{'API SUCCESS' if success(observation) else 'FAIL'}; audit {audit_state(observation)}; first of {len(recorded_warmups)} recorded warm-up(s)") if observation else "NOT RUN — no warm-up row",
        ])
    lines += [table(["Test ID", "Context", "Warm-up run ID", "Warm-up prompt_eval_count", "Warm-up prompt_eval_duration_s", "Warm-up prompt tok/s", "Warm-up load_duration_s", "Warm-up total API latency", "Measured median prompt tok/s (n=5 when complete)", "Warm-up cached prompt tokens", "Observation status"], context_warmups), "",
              "Each context row above is the first post-context-reload warm-up observation (**n=1**), reported separately from the five measured runs. C1 and C2 change the runner context; the recorded load and total durations show the corresponding first-request cost. One observation does not establish an uncached-prefill population rate or support a significance claim, and runner reload does not establish physically cold CPU caches.", "",
              "Native slot erasure was unavailable (HTTP 501), so this campaign consistently preserves the cache policy. Repeated measured prompts may reuse KV-prefix entries; their reported prompt throughput uses the requested API formula and may include cached-prefill inflation. The measured median prompt rate must not be interpreted as pure uncached prompt-processing performance. The warm-up remains excluded from all five-run medians and acceptance sample counts.", "", "### Fixed prompt workloads", ""]
    workloads = []
    for name in ("P1", "P2", "P3"):
        item = workload_stats[name]
        workloads.append([name, compact(item["prompt_tokens"]) if item["prompt_tokens"] else "NOT RUN", compact(item["output_tokens"]) if item["output_tokens"] else "NOT RUN", format_number(item["median_latency"], " s"), format_number(item["median_tps"]), format_number(item["median_prompt_tps"]), format_number(item["median_cached_prompt_tokens"], digits=0), format_number(item["median_uncached_prompt_tps"]), result_text(item)])
    lines += [table(["Workload", "Prompt tokens", "Output tokens", "Median latency", "Generation tok/s", "Total-count prompt tok/s (cache-dependent)", "Median cached prompt tokens", "Uncached-count prompt tok/s (supplement)", "Result"], workloads), "",
              "The primary prompt rate remains the requested `prompt_eval_count / prompt_eval_duration_seconds`. When the API supplies `prompt_eval_cached_count`, the supplemental rate is `max(0, prompt_eval_count − prompt_eval_cached_count) / prompt_eval_duration_seconds`; medians are calculated from per-run rates. Both depend on the observed cache state. A repeated request processing one new token after a cached prefix does not measure bulk uncached prefill. Missing cache counters produce no supplemental rate.", "",
              "P3's exact 2000 native tokens describe the document only (`add_special=false`), excluding the question and chat template. API-reported prompt counts can differ because of formatting and cache accounting; they need not equal 2000. Cached token counts remain separate from the primary API count.", ""]
    if fixture:
        lines += [f"P3 fixture evidence: `{compact(fixture)}`. Exact-token fixture gate: **{'VERIFIED' if fixture_ok else 'NOT VERIFIED'}**.", ""]
    else:
        lines += ["P3 exact-token document verification: **NOT RUN — fixture metadata not supplied**. A word-count estimate does not establish 2,000 model tokens.", ""]
    lines += ["## D. Nine-Lab Regression Matrix", "", "Matrix cells distinguish real-model execution, structural API/evaluator checks, and injected transport faults. A live valid solution passes only when the unmodified evaluator releases a flag and the server accepts its submission. Scripted or structural success cannot substitute for a live valid flag.", ""]
    matrix = []
    for lab, technique in LABS.items():
        cases = final_cases[lab]
        all_pass = all(case_pass(cases.get(category), category == "valid_solution") for category in required_categories) and matches_proof(cases.get("valid_solution"), proofs.get(str(lab)))
        state = "PASS" if all_pass else "NOT RUN" if not cases else "FAIL / INCOMPLETE"
        matrix.append([f"Lab {lab} — {technique}", case_cell(cases.get("normal_request")), case_cell(cases.get("valid_solution"), True), case_cell(cases.get("invalid_attempt")), case_cell(cases.get("session_isolation")), state])
    lines += [table(["Lab", "Normal request", "Valid solution", "Invalid attempt", "Isolation", "Result"], matrix), "", "### Baseline validity and protected/error behavior", ""]
    lines.append(table(["Lab", "Baseline valid flag", "No Protection", "Protected L1", "Protected L2", "Long prompt", "Timeout response", "Duplicate award"], [
        [lab, case_cell(base_cases[lab].get("valid_solution"), True), case_cell(final_cases[lab].get("no_protection")), case_cell(final_cases[lab].get("protected_mode_l1")), case_cell(final_cases[lab].get("protected_mode_l2")), case_cell(final_cases[lab].get("excessively_long_prompt")), case_cell(final_cases[lab].get("model_timeout")), case_cell(final_cases[lab].get("duplicate_award"))] for lab in LABS
    ]))
    regressions_worse = [lab for lab in LABS if case_pass(base_cases[lab].get("valid_solution"), True) and not case_pass(final_cases[lab].get("valid_solution"), True)]
    mismatched_final = sum(str(row.get("phase", "")).lower() == "final" and not regression_profile_matches(row, final_model, final_threads, final_context, final_budget, final_per_lab_budgets) for row in lab_rows)
    lines += ["", f"Baseline proven live valid flags: **{baseline_valid}/9**. Final live valid flags: **{final_valid}/9**. Final replays matching the immutable baseline candidate hashes: **{established_replays}/9**. Labs with a verified baseline flag but no verified final flag: **{compact(regressions_worse)}**.", "", f"The final matrix counts only the confirmed final model/thread/context and per-lab output budget. Historical final-phase rows excluded for a different or unspecified profile: **{mismatched_final}**. Test seed42 is kept separate from production sampling behavior.", "", "The matrix selects the last result for the exact baseline-proven candidate payload/hash, not the last exploratory candidate. A failed legacy sample does not permanently block a later documented, untruncated, actually awarded baseline solution. Every candidate attempt stays visible below and in the raw records. A later failure of the same selected proven payload still counts as a failure; the report does not cherry-pick an earlier pass.", "", "### Existing suite history (unmodified tests)", "", table(["Phase", "Suite", "Passed", "Failed", "Execution"], [[row.get("phase", "unknown"), row.get("suite", "unknown"), row.get("passed", "NOT RUN"), row.get("failed", "NOT RUN"), row.get("execution", "unspecified")] for row in existing_suites]) if existing_suites else "**NOT RUN — no existing-suite records.**", "", "The original unseeded `tests/live_ctfs.py` sample run is evaluated separately from the documented seeded proof cases. Pre-existing baseline sample failures remain visible; a failing final original sample run is not converted into a full acceptance pass by a different seeded solution.", ""]
    lines += ["### Final-phase candidate profile history", "", table(["Threads", "Context / default output cap", "Established valid flags", "Established-solution failures", "Disposition"], [
        [count, f"{final_context} / {final_budget}", f"{sum(case_pass(cases.get('valid_solution'), True) and matches_proof(cases.get('valid_solution'), proofs.get(str(lab))) for lab, cases in by_lab.items())}/9", compact(rejected_profile_labs[count]), "Selected final profile" if number(count) == number(selected) else "Rejected candidate — established solution failed" if rejected_profile_labs[count] else "Not selected / full lab verification not established"]
        for count, by_lab in candidate_profile_cases.items()
        if any(by_lab.values())
    ]) if any(any(cases.values()) for cases in candidate_profile_cases.values()) else "**NOT RUN — no final-profile candidate records.**", "", "Every profile uses the unchanged evaluator. A candidate rejection remains visible even after the final matrix is rebuilt for a different selected profile.", "", "### Solution-candidate history, including legacy failures", ""]
    lines += ["Failed replay rows can lose their candidate details when the harness raises an assertion. The report identifies such a failure only when its immutable payload ID, case hash, source hash, and nonempty executed request-hash sequence match a directly verified baseline proof (the sequence may be a prefix if execution stopped early). Baseline identity evidence is derived in memory from an actual untruncated awarded flag and the stored candidate SHA-256; raw rows and proof files remain unchanged. This fallback applies only to failed rows and cannot create a successful proof. A later identified failure therefore overrides an older pass for that same established payload.", "",
              "<details>", "<summary>Verified baseline identity evidence for failed-replay matching</summary>", "",
              table(["Lab", "Payload ID", "Source SHA-256", "Case SHA-256", "Executed prompt SHA-256 sequence"], [
                  [lab, proof["payload_id"], evidence["source_sha256"], evidence["case_sha256"], compact(evidence["prompt_sha256_sequence"])]
                  for lab, proof in proofs.items() for evidence in proof.get("_verified_baseline_request_evidence", [])
              ]), "", "</details>", ""]
    solution_history = [row for row in lab_rows if row.get("case_category") == "valid_solution"]
    lines += [table(["Phase", "Lab", "Payload ID", "Effective settings", "Result", "Flag awarded", "Truncated", "Chosen-proof identity evidence", "Latency"], [
        [row.get("phase", "unknown"), first(row, "lab_number", "lab", default="unknown"), row.get("test_payload_id", "unknown"), compact(scalar(row.get("effective_settings"))), row.get("result", "NOT RUN"), row.get("flag_awarded", "unknown"), row.get("truncated", "unknown"), proof_match_method(row, proofs.get(str(first(row, "lab_number", "lab")))) or "NOT VERIFIED", format_number(number(row.get("response_latency_seconds")), " s")]
        for row in solution_history
    ]) if solution_history else "**NOT RUN — no recorded solution-candidate attempts.**", "", "Model refusals are recorded separately from application defense rejections. A refusal by the underlying LLM in No Protection mode is not relabelled as an application guard blocking the attempt. Fault-injection timeout tests establish controlled error handling, not a successful real-model response.", ""]
    if settings.get("refusal_detection_method"):
        lines += [f"Refusal classification method and limits: {settings['refusal_detection_method']}", ""]
    lines += ["<details>", "<summary>Detailed final regression case ledger</summary>", "", table(["Lab", "Payload ID", "Category / kind", "Expected", "Actual", "Model refusal", "Defense rejection", "Flag awarded", "Result", "Latency"], [
        [lab, first(row, "test_payload_id", default="unspecified"), f"{category} / {row.get('execution_kind', 'unspecified')}", first(row, "expected_behavior", default="NOT RUN"), first(row, "actual_behavior", default="NOT RUN"), row.get("model_refusal", "unknown"), row.get("defense_rejection", "unknown"), row.get("flag_awarded", "unknown"), row.get("result", "NOT RUN"), format_number(number(row.get("response_latency_seconds")), " s")]
        for lab, cases in final_cases.items() for category, row in cases.items()
    ] if any(final_cases.values()) else [["All", "NOT RUN", "NOT RUN", "No recorded final cases", "NOT RUN", "unknown", "unknown", "unknown", "NOT RUN", "NOT RUN"]]), "", "</details>", ""]
    lines += ["## E. Reliability Summary", ""]
    lines.append(table(["Metric", "Recorded result"], [
        ["Planned total measured requests", 30], ["Executed measured requests", reliability["attempts"]],
        ["Successful requests", reliability["successes"]], ["Errors", reliability["failures"]], ["Timeouts", reliability["timeouts"]],
        ["Average API latency", format_number(reliability["mean_latency"], " s")], ["Median API latency", format_number(reliability["median_latency"], " s")],
        ["Minimum / maximum API latency", f"{format_number(reliability['min_latency'], ' s')} / {format_number(reliability['max_latency'], ' s')}"],
        ["p95 API latency", format_number(reliability["p95_latency"], " s")], ["Peak container RAM", format_ram(reliability["peak_ram"])],
        ["Peak container working set", format_ram(reliability["peak_working_set"])],
        ["Median per-request mean Docker CPU utilization", format_number(reliability["mean_cpu"], "%")],
        ["Peak Docker CPU utilization", format_number(reliability["peak_cpu"], "%")],
        ["API-load-duration reload heuristic flags", reliability["unexpected_reloads"] if reliability["attempts"] else "NOT RUN"],
        ["Measured requests with load duration ≥ 1s", reliability["model_loads"] if reliability["attempts"] else "NOT RUN"],
        ["Runner observations, including initial warm-up", runner_audit["rows"] if runner_audit["rows"] else "NOT RUN"],
        ["Native runner PIDs observed", compact(runner_audit["observed_pids"]) if runner_audit["rows"] else "NOT RUN"],
        ["Runner PID transitions after warm-up", runner_audit["pid_changes_after_warmup"] if runner_audit["rows"] else "NOT RUN"],
        ["Absent native runner observations", runner_audit["missing_runner_observations"] if runner_audit["rows"] else "NOT RUN"],
        ["Unknown / unparseable runner observations", runner_audit["unknown_runner_observations"] if runner_audit["rows"] else "NOT RUN"],
        ["Absent model-presence observations (before/after)", runner_audit["missing_model_observations"] if runner_audit["rows"] else "NOT RUN"],
        ["Unknown model-presence observations (before/after)", runner_audit["unknown_model_observations"] if runner_audit["rows"] else "NOT RUN"],
        ["Runner-sequence errors", compact(runner_audit["sequence_errors"]) if runner_audit["rows"] else "NOT RUN"],
        ["Independent runner audit", "PASS — stable observed PID and model presence" if runner_audit["ok"] else "FAIL / INCOMPLETE"],
        ["Maximum recorded restart count/change", reliability["restarts"] if reliability["attempts"] else "NOT RUN"],
        ["OOM observed", reliability["oom"] if reliability["attempts"] else "NOT RUN"],
        ["Monitoring errors", reliability["monitor_errors"] if reliability["attempts"] else "NOT RUN"],
        ["Intentional token-cap stops", f"{reliability['caps']}/{reliability['successes']} API-successful measured requests"],
        ["Contaminated / unverified measured runs", f"{reliability['contaminated']} / {reliability['unverified']}"],
        ["Reliability gate", "PASS — 30/30 with required checks" if reliability_ok else "FAIL / INCOMPLETE — see recorded counts"],
    ]))
    lines += ["", "The native runner audit reads `runner_args_json` and model-presence observations in recorded request order. The initial warm-up's runner PID set establishes the baseline; subsequent PID-set transitions are counted separately from the API load-duration heuristic. Missing or unparseable runner records, model absence, unknown before/after model observations, and ambiguous sequence evidence fail this audit. Presence checks cover the recorded warm-up and every measured request.", "",
              "These are request-boundary process and residency observations. They do not exclude events entirely between observations or PID reuse. Docker container restart, OOM, and preservation checks remain separate evidence; a short API load duration alone does not prove that the runner remained loaded.", "",
              "<details>", "<summary>Reliability runner observation ledger</summary>", "",
              table(["Run ID", "Observation", "Runner PIDs", "Runner status", "Model present before", "Model present after"], [
                  [item["run_id"], "Warm-up baseline" if item["is_warmup"] else "Measured request", compact(item["runner_pids"]), item["runner_observation"], item["model_present_before"], item["model_present_after"]]
                  for item in runner_audit["ledger"]
              ]) if runner_audit["ledger"] else "NOT RUN — no runner observations.", "",
              "PID transitions: " + compact(runner_audit["changes"]), "", "</details>", ""]
    lines += ["", "## F. Final Configuration", "", "Exact final configuration, as confirmed by the operator. This section is NOT RUN when no applied configuration snapshot was supplied.", ""]
    if final_config:
        lines += ["```json", *yaml_like(final_config), "```", ""]
    else:
        lines += ["**NOT RUN — final applied configuration not supplied.**", ""]
    benchmark_options = settings.get("benchmark_request_options", {"temperature": .3, "top_p": .9, "top_k": 40, "repeat_penalty": 1.0, "seed": 42, "think": False, "keep_alive": "24h"})
    lines += ["Benchmark request settings (the exact per-run options, including thread/context/output variants, remain in `options_json`):", "", "```json", *yaml_like(benchmark_options), "```", "", "`seed: 42` is a benchmark reproducibility setting. Existing application-specific inference overrides must remain explicit; a benchmark seed must not be silently described as a production behavior change.", ""]
    overrides = settings.get("application_overrides")
    if overrides:
        lines += ["Preserved application-specific overrides:", "", "```json", *yaml_like(overrides), "```", ""]
    else:
        lines += ["Application-specific override capture: **NOT RUN — not supplied**.", ""]
    lines += ["### Preserved state and reversible changes", "", table(["Invariant", "Verification"], [[key, "VERIFIED" if preservation.get(key) is True else "FAILED" if preservation.get(key) is False else "NOT RUN"] for key in PRESERVATION_KEYS]), ""]
    if settings.get("source_hashes"):
        lines += ["Source/model invariants and hash evidence:", "", "```json", *yaml_like(settings["source_hashes"]), "```", ""]
    lines += ["Backups and restoration evidence:", "", "```json", *yaml_like(settings.get("backups", {"status": "NOT RUN — no backup evidence supplied"})), "```", ""]
    changes = settings.get("files_changed", [])
    lines += [table(["File", "Reason"], [[change.get("path", "unknown"), change.get("reason", "unspecified")] if isinstance(change, dict) else [change, "Reason not supplied"] for change in changes]) if changes else "Files changed: **NOT RUN — no change manifest supplied**.", ""]
    lines += ["## G. Conclusions", ""]
    questions = [
        ("1. Which thread count performed best?", (f"{measured_winner} threads had the highest median generation throughput among complete error-free five-run thread groups. The selected setting is {selected} threads. " + (f"The faster candidate failed established solutions in labs {rejected_profile_labs[measured_winner]} and was rejected to preserve lab reliability." if reliability_selection_ok else configuration_reason)) if measured_winner is not None else "NOT RUN — a complete measured winner is not available."),
        ("2. Was the improvement statistically consistent across the five runs?", consistency_note + " Review the min/max range and throughput CV in section B. Five observations are insufficient for a strong population-level significance claim."),
        ("3. Is 8192 context justified for this application?", settings.get("context_conclusion", "NOT RUN — no complete context/long-context and lab-functionality conclusion supplied. Preserve 8192 until a smaller context is verified reliable.")),
        ("4. What is the measured response-speed improvement?", (f"The original thread count was retained; no threading-configuration gain is claimed. The observed B0-versus-same-thread repeat throughput delta was {format_number(improvement, '%', 2)}, with an observed median API latency reduction of {format_number(reduction, '%', 2)}. " if retained_original_threads else f"Median generation throughput changed by {format_number(improvement, '%', 2)}; observed median API latency changed by a {format_number(reduction, '%', 2)} reduction. ") + "Achieved output lengths may differ despite the same seed, so the total-latency delta is not wholly attributable to thread speed and is not a universal gain for every lab." if improvement is not None else "NOT RUN — comparable complete baseline and selected measurements are missing."),
        ("5. Did any lab become less reliable?", settings.get("lab_reliability_conclusion", f"{len(regressions_worse)} lab(s) have a verified baseline valid flag without a verified final valid flag: {regressions_worse}. Full reliability is {'verified by the required final suites' if lab_ok else 'NOT VERIFIED; unexecuted or failed cases remain'}.")),
        ("6. What bottleneck remains?", settings.get("remaining_bottleneck", "NOT RUN — no measured bottleneck conclusion supplied.")),
        ("7. Should the model or container be changed further?", settings.get("further_changes_conclusion", "Keep the existing model, quantization, objectives, and Docker volumes. Further changes require measured evidence and a new reliability/regression check.")),
    ]
    for question, answer in questions:
        lines += [f"**{question}**", "", str(answer), ""]
    lines += ["### Artifact index", ""]
    for label, path in artifacts.items():
        try:
            target = str(path.relative_to(output.parent))
        except ValueError:
            target = str(path)
        lines.append(f"- {label}: [{path.name}]({target})" + ("" if path.exists() else " — NOT RUN / file absent"))
    omitted = settings.get("not_run", [])
    if omitted:
        lines += ["", "Explicit NOT RUN register:", ""] + [f"- {compact(value)}" for value in omitted]
    lines += ["", "This report is generated from captured artifacts. It does not apply settings or run tests. A missing measurement is not a zero, and a scripted response is not a successful live-model exploit.", ""]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
    return {"report": str(output), "accepted": accepted, "measured_winner": measured_winner, "selected_threads": selected, "baseline_valid_flags": baseline_valid, "final_valid_flags": final_valid, "established_solution_replays": established_replays, "complete_final_lab_suites": complete_labs, "reliability_successes": reliability["successes"], "reliability_attempts": reliability["attempts"], "reliability_runner_audit": runner_audit, "issues": issues}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--results", type=Path, help="Raw benchmark CSV or JSONL")
    parser.add_argument("--regression", type=Path, help="Raw per-case regression JSONL or CSV")
    parser.add_argument("--settings", type=Path, help="Operator-confirmed snapshots, final state, preservation checks")
    parser.add_argument("--proofs", type=Path, help="Baseline-proven candidate payload/hash receipts")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    results = args.results or root / "ollama_benchmark_results.csv"
    regression = args.regression or root / "lab_regression_results.jsonl"
    settings_path = args.settings or root / "performance_report_settings.json"
    settings = load_json(settings_path, {})
    output = args.output or root / "OLLAMA_PERFORMANCE_REPORT.md"
    artifacts = {
        "Raw benchmark measurements (one row per run)": results,
        "Full Ollama responses and effective per-run settings": results.parent / "ollama_benchmark_runs.jsonl",
        "Benchmark metadata": results.parent / "ollama_benchmark_metadata.json",
        "Per-case lab regression records": regression,
        "Baseline-proven solution receipts": args.proofs or regression.parent / "proven_lab_solutions.json",
        "Operator snapshots and final-configuration evidence": settings_path,
        "Repeatable benchmark script": root / "scripts" / "ollama_benchmark.py",
        "Repeatable isolated lab regression harness": root / "scripts" / "lab_regression.py",
        "Documented regression payloads": root / "lab_regression_cases.json",
    }
    outcome = write_report(root, load_rows(results), load_rows(regression), settings, artifacts, output)
    print(json.dumps(outcome, indent=2))


if __name__ == "__main__":
    main()
