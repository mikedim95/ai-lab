#!/usr/bin/env python3
"""Build a concise verification report from recorded evidence; performs no inference.

Missing or incomplete evidence is labelled NOT RUN and cannot produce a PASS.
The script reads artifacts and writes only its report and optional summary JSON.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics


MODEL = "ministral-3:3b"
DEFAULTS = {
    "fresh_results": "performance/final-verification/fresh/ollama_fresh_verification_results.csv",
    "reliability_results": "performance/final-verification/reliability24/ollama_benchmark_results.csv",
    "lab_results": "performance/final-verification/labs/verification_results.jsonl",
    "normal_review": "performance/final-verification/labs/normal_response_review.jsonl",
    "fixtures": "fixtures.json",
    "normal_review_script": "scripts/review_normal_responses.py",
    "normal_review_rules": "scripts/verification_normal_checks.py",
    "preservation": "performance/final-verification/final-preservation.json",
    "diagnostics": "performance/final-verification/diagnostics.json",
    "payload_cases": "scripts/verification_lab_cases.json",
    "original_payload_cases": "lab_regression_cases.json",
    "control16_proofs": "performance/labs/proven_lab_solutions.json",
    "legacy_results": "ollama_benchmark_results.csv",
}


def scalar(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def num(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def yes(value):
    return value is True or str(value).lower() in ("true", "yes", "1", "pass", "valid")


def no(value):
    return value is False or str(value).lower() in ("false", "no", "0")


def first(row, *keys):
    for key in keys:
        if row.get(key) is not None and row.get(key) != "":
            return row[key]
    return None


def number(row, *keys):
    return num(first(row, *keys))


def load_json(path, default=None):
    if not path.is_file():
        return {} if default is None else default
    return json.loads(path.read_text())


def load_rows(path):
    if not path.is_file():
        return []
    if path.suffix.lower() == ".csv":
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle))
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    value = load_json(path, [])
    if isinstance(value, list):
        return value
    return value.get("rows", value.get("runs", []))


def clean(value):
    return str(value if value is not None else "NOT RUN").replace("|", "\\|").replace("\n", " ")


def fmt(value, suffix="", digits=2):
    return f"{value:.{digits}f}{suffix}" if value is not None else "NOT RUN"


def table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"] + ["| " + " | ".join(clean(value) for value in row) + " |" for row in rows]


def status(ok):
    return "PASS" if ok else "FAIL"


def warm(row):
    return yes(row.get("is_warmup"))


def successful(row):
    return yes(row.get("success")) and not row.get("error")


def audited(row):
    return str(row.get("phase_audit_status", "")).upper() in ("VALID", "CLEAN", "PASS", "VERIFIED") and not yes(row.get("phase_contaminated"))


def metrics(rows):
    measured = [row for row in rows if not warm(row)]
    good = [row for row in measured if successful(row)]
    def values(*keys):
        return [value for row in good if (value := number(row, *keys)) is not None]
    def median(*keys):
        values_ = values(*keys)
        return statistics.median(values_) if values_ else None
    latency = sorted(values("total_duration_s", "total_duration_seconds"))
    return {
        "measured": measured, "good": good, "n": len(measured), "successes": len(good),
        "warmups": sum(warm(row) for row in rows), "median_tps": median("tokens_per_second", "generation_tokens_per_second"),
        "median_latency": statistics.median(latency) if latency else None,
        "mean_latency": statistics.mean(latency) if latency else None,
        "minimum_latency": min(latency) if latency else None, "maximum_latency": max(latency) if latency else None,
        "p95": latency[math.ceil(.95 * len(latency))-1] if latency else None,
        "median_wall": median("wall_latency_s"), "median_ttft": median("ttft_s", "ttft_seconds"),
        "ttft_n": len(values("ttft_s", "ttft_seconds")),
        "output_counts": [int(value) for value in values("eval_count")],
        "prompt_counts": [int(value) for value in values("prompt_eval_count")],
        "cached_counts": [int(value) for value in values("prompt_eval_cached_count")],
        "peak_ram": max(values("peak_ram_bytes"), default=None),
        "cpu_mean": statistics.mean(values("mean_cpu_percent")) if values("mean_cpu_percent") else None,
        "peak_cpu": max(values("peak_cpu_percent"), default=None),
        "timeouts": sum(yes(row.get("timed_out")) for row in measured),
        "restarts": sum(int(number(row, "container_restarts") or 0) for row in measured),
        "oom": sum(yes(row.get("oom_killed")) for row in measured),
        "load_reload_flags": sum(yes(row.get("unexpected_reload")) for row in measured),
        "monitor_errors": sum(bool(row.get("monitor_error")) for row in measured),
    }


def workload(row):
    name = str(first(row, "fresh_workload", "workload_id", "workload", "prompt_variant") or "").lower()
    if "short" in name or name.startswith("p1"):
        return "short"
    if "long" in name or name.startswith("p3"):
        return "long"
    return name


def thread(row):
    direct = number(row, "threads", "num_thread")
    if direct is not None:
        return int(direct)
    effective = scalar(row.get("effective_settings")) or {}
    options = scalar(effective.get("options")) or {}
    return int(num(options.get("num_thread"))) if num(options.get("num_thread")) is not None else None


def latest_fresh_groups(rows):
    groups, last = {}, {}
    for row in rows:
        profile = (thread(row), workload(row))
        key = (row.get("session_id"), row.get("test_id"), profile)
        groups.setdefault(key, []).append(row)
        last[profile] = key
    return {profile: groups[key] for profile, key in last.items()}


def fresh_analysis(rows):
    groups = latest_fresh_groups(rows)
    summaries = {key: metrics(value) for key, value in groups.items()}
    missing, failures = [], []
    for count in (16, 24):
        for name in ("short", "long"):
            profile = summaries.get((count, name), metrics([]))
            measured = profile["measured"]
            label = f"{count}/{name}"
            if profile["n"] != 5:
                missing.append(f"{label}: {profile['n']}/5 measured")
            if profile["successes"] != 5:
                failures.append(f"{label}: not 5/5 completed API results")
            if len({row.get("prompt_sha256") for row in measured}) != 5 or any(not row.get("prompt_sha256") for row in measured):
                failures.append(f"{label}: five distinct prompt hashes not established")
            if any(number(row, "prompt_eval_cached_count") != 0 or not yes(row.get("uncached_verified")) for row in measured):
                failures.append(f"{label}: actual zero-cached prompts not verified")
            if profile["ttft_n"] != 5 or any((number(row, "ttft_s", "ttft_seconds") or 0) <= 0 for row in measured):
                failures.append(f"{label}: five observed streamed first-token times not established")
            if any(not audited(row) for row in measured):
                failures.append(f"{label}: contention audit incomplete or invalid")
    comparisons = {}
    for name in ("short", "long"):
        a = summaries.get((16, name), metrics([]))
        b = summaries.get((24, name), metrics([]))
        keyed_a = {str(first(row, "pair_id", "run_index")): row for row in a["measured"]}
        keyed_b = {str(first(row, "pair_id", "run_index")): row for row in b["measured"]}
        pairs = sorted(set(keyed_a) & set(keyed_b))
        matched = len(pairs) == 5
        faster = 0
        for key in pairs:
            left, right = keyed_a[key], keyed_b[key]
            same_prompt = left.get("prompt_sha256") == right.get("prompt_sha256") and bool(left.get("prompt_sha256"))
            options_a, options_b = scalar(left.get("options_json")), scalar(right.get("options_json"))
            if isinstance(options_a, dict) and isinstance(options_b, dict):
                options_a = {k:v for k,v in options_a.items() if k != "num_thread"}
                options_b = {k:v for k,v in options_b.items() if k != "num_thread"}
                same_options = options_a == options_b
            else:
                same_options = False
            matched &= same_prompt and same_options
            speeds = (number(left, "tokens_per_second"), number(right, "tokens_per_second"))
            faster += speeds[0] is not None and speeds[1] is not None and speeds[1] > speeds[0]
        if not matched:
            failures.append(f"{name}: five identical paired prompts/options not established")
        gain = (b["median_tps"]/a["median_tps"]-1)*100 if a["median_tps"] and b["median_tps"] else None
        comparisons[name] = {"matched": matched, "pairs_faster24": faster, "gain_percent": gain,
                             "latency_change_percent": (b["median_latency"]/a["median_latency"]-1)*100 if a["median_latency"] and b["median_latency"] else None,
                             "ttft_change_percent": (b["median_ttft"]/a["median_ttft"]-1)*100 if a["median_ttft"] and b["median_ttft"] else None,
                             "median_faster24": gain is not None and gain > 0}
    uncached_ok = not missing and not failures
    cpu_ok = uncached_ok and all(item["median_faster24"] and item["pairs_faster24"] == 5 for item in comparisons.values())
    return {"summaries": summaries, "comparisons": comparisons, "missing": missing, "failures": failures,
            "uncached_ok": uncached_ok, "cpu_ok": cpu_ok}


def runner_audit(rows):
    previous = None
    changes, unknown, absence = [], 0, 0
    observed = set()
    for row in rows:
        value = scalar(row.get("runner_args_json"))
        pids = set()
        if not isinstance(value, list):
            unknown += 1
        else:
            for item in value:
                if isinstance(item, list) and item and str(item[0]).isdigit():
                    pids.add(str(item[0]))
                else:
                    unknown += 1
            if not pids:
                absence += 1
            observed |= pids
            if previous is not None and pids != previous:
                changes.append({"run_id": row.get("run_id"), "before": sorted(previous), "after": sorted(pids)})
            previous = pids
        for key in ("model_present_before", "model_present_after"):
            if no(row.get(key)):
                absence += 1
            elif not yes(row.get(key)):
                unknown += 1
    return {"pid_changes": changes, "unknown_observations": unknown, "absent_observations": absence,
            "pids": sorted(observed), "ok": bool(rows) and not changes and not unknown and not absence}


def reliability_analysis(rows):
    candidates = [row for row in rows if thread(row) == 24 and (str(row.get("phase", "")).lower() == "reliability" or "reliability" in str(row.get("test_id", "")).lower())]
    if candidates:
        session = candidates[-1].get("session_id")
        candidates = [row for row in candidates if row.get("session_id") == session]
    result = metrics(candidates)
    result["runner_audit"] = runner_audit(candidates)
    result["monitoring_complete"] = bool(candidates) and all(
        number(row, "peak_ram_bytes") is not None and number(row, "mean_cpu_percent") is not None
        and number(row, "container_restarts") is not None
        and (yes(row.get("oom_killed")) or no(row.get("oom_killed")))
        for row in candidates)
    measured = result["measured"]
    option_sets = [scalar(row.get("options_json")) for row in measured]
    result["fixed_options"] = bool(option_sets) and all(isinstance(value, dict) and value == option_sets[0] for value in option_sets)
    result["ok"] = (result["n"] == result["successes"] == 30 and result["warmups"] >= 1
                    and all(audited(row) for row in candidates) and not result["timeouts"]
                    and not result["restarts"] and not result["oom"] and not result["load_reload_flags"]
                    and not result["monitor_errors"] and result["runner_audit"]["ok"]
                    and result["monitoring_complete"] and result["fixed_options"]
                    and len({row.get("run_id") for row in measured}) == 30)
    return result


def budget(row):
    direct = number(row, "output_limit", "max_output")
    if direct is not None:
        return int(direct)
    effective = scalar(row.get("effective_settings")) or {}
    options = scalar(effective.get("options")) or {}
    return int(num(options.get("num_predict"))) if num(options.get("num_predict")) is not None else None


def real_receipts(row):
    return [item for item in (scalar(row.get("model_metrics")) or []) if item.get("execution_kind") == "real_model"]


def completion_kind(receipt, cap):
    reason = receipt.get("done_reason")
    if reason in ("length", "max_tokens", "limit"):
        return "TRUNCATED_EXPLICIT"
    if reason == "stop":
        return "COMPLETE_STOP_AT_BOUNDARY" if num(receipt.get("eval_count")) == num(cap) else "COMPLETE_STOP"
    return "UNVERIFIED"


NORMAL_REVIEW_CRITERIA = {
    "normal_category", "one_real_model_receipt", "nonempty_recorded_reply",
    "reply_matches_recorded_assistant_message", "both_products_present",
    "original_fixture_amounts_preserved", "both_fixture_prices_present", "both_fixture_materials_present",
    "no_application_block", "no_protection_level_zero", "no_application_hardening_prompt",
    "no_flag_award_or_completion", "no_model_refusal", "no_recorded_transport_error",
    "completion_verified", "effective_budget_matches_receipt", "request_hash_matches_recorded_messages",
    "original_failure_is_reviewable",
}


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def reviewed_completion(receipt, cap):
    count, limit = num(receipt.get("eval_count")), num(cap)
    reason = receipt.get("done_reason")
    if reason in ("length", "max_tokens", "limit"):
        return "TRUNCATED_EXPLICIT_LIMIT", True
    if reason == "stop" and count is not None and limit is not None and count > limit:
        return "STOP_ABOVE_CAP_INCONSISTENT", None
    if reason == "stop" and count is not None and limit is not None and 0 < count <= limit and not receipt.get("error"):
        return ("COMPLETE_STOP_AT_CAP_BOUNDARY" if count == limit else "COMPLETE_STOP"), False
    return "COMPLETION_UNVERIFIED", None


def normal_review_overlay(rows, reviews, input_sha256, fixtures_sha256, review_script_sha256, rules_sha256):
    """Return copies with exact, audited normal-only reviews; raw rows stay intact."""
    indexed = {}
    for review in reviews:
        indexed.setdefault(review.get("original_row_sha256"), []).append(review)
    latest_normal_indices = {}
    for index,row in enumerate(rows):
        if row.get("case_category") == "normal_request" and thread(row) == 24:
            latest_normal_indices[(num(row.get("lab_number")),budget(row))] = index
    output, applied, rejected, applied_indices = [], [], [], set()
    for index,row in enumerate(rows):
        if row.get("case_category") != "normal_request":
            output.append(row)
            continue
        records = indexed.get(canonical_sha256(row), [])
        matching = [review for review in records if review.get("input_sha256") == input_sha256]
        if not matching:
            output.append(row)
            continue
        review = matching[-1]
        reasons = []
        for key in ("run_id", "lab_number", "output_limit", "test_payload_id", "source_sha256", "case_sha256", "effective_settings"):
            if review.get(key) != row.get(key) or review.get(key) is None:
                reasons.append(f"identity mismatch: {key}")
        if review.get("case_category") != "normal_request" or review.get("fixtures_sha256") != fixtures_sha256 or not fixtures_sha256:
            reasons.append("normal category or fixture identity mismatch")
        if (not review_script_sha256 or review.get("review_script_sha256") != review_script_sha256
                or not rules_sha256 or review.get("criteria_module_sha256") != rules_sha256):
            reasons.append("offline review/rule source hash mismatch")
        receipts = real_receipts(row)
        hashes = [receipt.get("prompt_sha256") for receipt in receipts]
        if not hashes or any(not value for value in hashes) or review.get("prompt_sha256_sequence") != hashes:
            reasons.append("actual request-hash identity mismatch")
        original_fields = {
            "original_result":row.get("result"), "original_actual_behavior":str(row.get("actual_behavior", "")),
            "original_root_cause":row.get("root_cause"), "original_truncated":row.get("truncated"),
            "original_unexpected_truncation":row.get("unexpected_truncation"),
        }
        if any(review.get(key) != value for key,value in original_fields.items()):
            reasons.append("original verdict/error not preserved")
        criteria = review.get("criterion_results")
        if not isinstance(criteria,dict) or not NORMAL_REVIEW_CRITERIA.issubset(criteria) or any(not isinstance(criteria[key],bool) for key in NORMAL_REVIEW_CRITERIA):
            reasons.append("required normal criterion set missing or invalid")
        else:
            expected = "PASS" if all(criteria[key] for key in NORMAL_REVIEW_CRITERIA) else "FAIL"
            if review.get("reviewed_result") != expected:
                reasons.append("review verdict contradicts required criteria")
        classification,truncated = reviewed_completion(receipts[-1] if receipts else {}, budget(row))
        if review.get("completion_classification") != classification or review.get("reviewed_truncated") is not truncated:
            reasons.append("completion evidence mismatch")
        corrections = review.get("correction_reasons", [])
        if row.get("result") == "FAIL" and review.get("reviewed_result") == "PASS":
            allowed = {"VALIDATOR_PRICE_PUNCTUATION_BOUNDARY", "VALIDATOR_NATURAL_STOP_AT_CAP_BOUNDARY"}
            if not corrections or not set(corrections).issubset(allowed):
                reasons.append("unrecognized raw-failure correction")
            if "VALIDATOR_PRICE_PUNCTUATION_BOUNDARY" in corrections and "Normal comparison omitted or misstated the fixture price" not in str(row.get("actual_behavior", "")):
                reasons.append("price correction lacks original price assertion")
            if "VALIDATOR_NATURAL_STOP_AT_CAP_BOUNDARY" in corrections and classification != "COMPLETE_STOP_AT_CAP_BOUNDARY":
                reasons.append("boundary correction lacks natural EOS at cap")
        if reasons:
            rejected.append({"run_id":row.get("run_id"),"lab_number":row.get("lab_number"),"output_limit":budget(row),"reasons":reasons})
            output.append(row)
            continue
        effective = copy.deepcopy(row)
        effective["result"] = review["reviewed_result"]
        effective["truncated"] = truncated
        effective["unexpected_truncation"] = truncated is True
        effective["root_cause"] = review.get("reviewed_root_cause") or row.get("root_cause")
        effective["_normal_review"] = review
        output.append(effective)
        applied_indices.add(index)
        applied.append(review)
    required_indices = set(latest_normal_indices.values())
    return output, {"applied":applied,"rejected":rejected,"latest_normal_count":len(required_indices),
                    "complete":bool(required_indices) and required_indices.issubset(applied_indices),
                    "changed":[review for review in applied if review.get("original_result") != review.get("reviewed_result") or review.get("original_truncated") != review.get("reviewed_truncated")]}


def cap_summary(item, cap):
    receipts = [receipt for row in item['budgets'][cap].values()
                if row.get('case_category') in ('normal_request','intended_exploit','valid_solution','format_alternate')
                for receipt in real_receipts(row)]
    kinds = [completion_kind(receipt, cap) for receipt in receipts]
    return f"{kinds.count('TRUNCATED_EXPLICIT')} / {kinds.count('COMPLETE_STOP_AT_BOUNDARY')} / {len(receipts)}"


def lab_pass(row, *, real=False, flag=False):
    details = scalar((row or {}).get("details")) or {}
    return (bool(row) and row.get("result") == "PASS" and (not real or bool(real_receipts(row)))
            and (not flag or (yes(row.get("flag_awarded")) and details.get("flag_endpoint_accepted") is True
                              and details.get("flag_issued") is True and (num(details.get("points_awarded")) or 0) > 0))
            and not yes(row.get("truncated")))


def latest_lab_rows(rows, lab, cap):
    selected = {}
    for row in rows:
        if thread(row) == 24 and num(first(row, "lab_number", "lab")) == lab and budget(row) == cap:
            selected[row.get("case_category")] = row
    return selected


def lab_analysis(rows, settings):
    result, missing = {}, []
    for lab in range(1,10):
        by_budget = {cap:latest_lab_rows(rows, lab, cap) for cap in (256,512)}
        selected_cap = settings.get("per_lab_output_overrides", {}).get(str(lab), settings.get("recommended_default_output", 512))
        current = by_budget.get(int(selected_cap), {})
        normal = current.get("normal_request")
        original = current.get("intended_exploit", current.get("valid_solution"))
        alternate = current.get("format_alternate")
        chosen = original if lab_pass(original, real=True, flag=True) else alternate if lab_pass(alternate, real=True, flag=True) else original or alternate
        incorrect = current.get("incorrect_attack")
        protection = current.get("no_protection")
        protection_real = real_receipts(original or {})
        no_protection_ok = (lab_pass(protection) and not yes((protection or {}).get("defense_rejection"))
                            and bool(protection_real) and all(item.get("hardening_policy_present") is False for item in protection_real))
        normal_ok = lab_pass(normal, real=True) and not yes((normal or {}).get("model_refusal")) and not yes((normal or {}).get("flag_awarded"))
        exploit_ok = lab_pass(chosen, real=True, flag=True)
        incorrect_ok = lab_pass(incorrect, real=True) and not yes((incorrect or {}).get("flag_awarded"))
        originals_ok = lab_pass(original, real=True, flag=True)
        receipts = real_receipts(original or {})
        refusal = any(yes(item.get("model_refusal")) for item in receipts)
        row_missing = [name for name,value in [("normal",normal),("intended exploit",original),("incorrect attack",incorrect),("No Protection",protection)] if not value]
        for cap in (256,512):
            if not by_budget[cap].get("normal_request") or not (by_budget[cap].get("intended_exploit") or by_budget[cap].get("valid_solution")):
                missing.append(f"Lab{lab}/{cap}: normal or intended-cap comparison NOT RUN")
        if row_missing:
            missing.append(f"Lab{lab}/512: {', '.join(row_missing)} NOT RUN")
        causes = [f"{name}: {row.get('root_cause') or row.get('actual_behavior') or 'cause not established'}"
                  for name,row in (("normal",normal),("primary exploit",original),("incorrect attempt",incorrect),("No Protection",protection))
                  if row and row.get("result") != "PASS"]
        cause = (settings.get("lab_root_causes", {}).get(str(lab)) or "; ".join(causes)
                 or ("No failure observed in this case" if exploit_ok else "Cause not established; inspect recorded replies/evaluator evidence"))
        result[lab] = {"budgets":by_budget,"normal":normal,"original":original,"chosen":chosen,"alternate":alternate,
                       "incorrect":incorrect,"protection":protection,"normal_ok":normal_ok,"exploit_ok":exploit_ok,
                       "incorrect_ok":incorrect_ok,"no_protection_ok":no_protection_ok,"original_ok":originals_ok,
                       "refusal":refusal,"root_cause":cause,"ok":normal_ok and exploit_ok and incorrect_ok and no_protection_ok,
                       "missing":row_missing,"selected_cap":selected_cap}
    functionality_ok = all(item["ok"] for item in result.values())
    cap_complete = not missing
    selected_complete = all(
        bool(receipts := real_receipts(row or {}))
        and all(reviewed_completion(receipt,item["selected_cap"])[1] is False for receipt in receipts)
        for item in result.values() for row in (item["normal"], item["original"]))
    return {"labs":result,"missing":missing,"functionality_ok":functionality_ok,
            "established_ok":all(item["original_ok"] for item in result.values()),
            "truncation_ok":cap_complete and selected_complete}


def default_settings():
    return {"artifacts":DEFAULTS,"recommended_default_output":512,"per_lab_output_overrides":{},
            "require_established_payloads_for_deployment":True,"control16_verified":False,
            "control16_evidence":"NOT RUN — identify the earlier proof suite and current source/model preservation checks; do not imply a new 16-thread lab run.",
            "lab_reproduction_command":"python3 scripts/verify_nine_labs.py --run --project-root . --threads 24 --context 8192 --budgets 256,512 --allow-format-alternates --verification-cases scripts/verification_lab_cases.json --output-dir performance/final-verification/labs",
            "normal_review_required":True,
            "app_request_overrides_verified":False,"app_request_override_evidence":"NOT RUN",
            "production_unchanged":None,"context_conclusion":"Retain 8192 until the smaller window passes the full supported history/retrieval workload.",
            "thread32_conclusion":"Cause NOT PROVEN. Report actual CPU topology, host steal/load, run-to-run variance, and limits; separate inference from observed facts.",
            "threads20_22_conclusion":"20 and 22 are reasonable intermediate candidates, but performance/flag behavior must be measured before deployment.",
            "truncation_conclusion":"A token cap can stop a valid detailed answer. Preserve 512 until the 256/512 per-lab results justify a smaller budget; shorter capped output is not faster inference.",
            "lab_root_causes":{},"notes":[]}


def write(root, settings, output):
    paths={key:(root/value).resolve() for key,value in {**DEFAULTS,**settings.get("artifacts",{})}.items()}
    fresh=fresh_analysis(load_rows(paths["fresh_results"]))
    reliability=reliability_analysis(load_rows(paths["reliability_results"]))
    preservation=load_json(paths["preservation"])
    def file_sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    raw_lab_rows = load_rows(paths["lab_results"])
    effective_lab_rows, normal_review = normal_review_overlay(
        raw_lab_rows, load_rows(paths["normal_review"]), file_sha(paths["lab_results"]),
        file_sha(paths["fixtures"]) or preservation.get("actual_source_hashes",{}).get("fixtures.json"),
        file_sha(paths["normal_review_script"]), file_sha(paths["normal_review_rules"]))
    labs=lab_analysis(effective_lab_rows,settings)
    control_proofs=load_json(paths["control16_proofs"])
    control_proofs_ok=all(
        isinstance(proof:=control_proofs.get(str(lab)),dict) and proof.get("response_truncated") is False
        and (proof.get("settings") or {}).get("model")==MODEL
        and ((proof.get("settings") or {}).get("options") or {}).get("num_thread")==16
        and bool(proof.get("candidate_sha256")) and bool(proof.get("payload_id"))
        for lab in range(1,10))
    checks=preservation.get("checks",{})
    required_preservation=("model_weights_unchanged","quantization_unchanged","volumes_unchanged","lab_objectives_unchanged","evaluators_unchanged","backups_retained","model_parameters_unchanged","ollama_image_unchanged","ollama_unlimited_cpu_ram","single_parallel_model_enforced","keep_alive_24h","application_profile_matches_selection","same_ollama_container_instance_and_start","no_current_ollama_oom_state","one_selected_cpu_model_active","active_runner_thread_context_match")
    docker_ok=(all(checks.get(key) is True for key in required_preservation)
               and settings.get("production_unchanged") is True and settings.get("app_request_overrides_verified") is True)
    functions={"CPU optimization":fresh["cpu_ok"],"Uncached prompt performance":fresh["uncached_ok"],
               "30-request reliability":reliability["ok"],"Response truncation":labs["truncation_ok"],
               "Nine-lab functionality":labs["functionality_ok"],"Docker configuration":docker_ok}
    mandatory_missing=fresh["missing"]+labs["missing"]
    if reliability["n"]!=30:mandatory_missing.append(f"Reliability24: {reliability['n']}/30 measured")
    if not preservation:mandatory_missing.append("Current preservation capture NOT RUN")
    if settings.get("normal_review_required",True) and not normal_review["complete"]:
        mandatory_missing.append("Exact-identity offline normal review is missing or incomplete")
    compatibility=labs["established_ok"] or not settings.get("require_established_payloads_for_deployment",True)
    if not mandatory_missing and all(functions.values()) and compatibility:
        decision="DEPLOY 24 THREADS"
        why="All recorded verification gates pass, including actual flag awards. This is a deployment decision, not evidence that a production change has already occurred."
    elif mandatory_missing or not settings.get("app_request_overrides_verified"):
        decision="FURTHER TESTING REQUIRED"
        why="Mandatory verification evidence is missing or incomplete. Production remains unchanged."
    elif settings.get("control16_verified") is True and control_proofs_ok and docker_ok:
        decision="KEEP 16 THREADS"
        failed_established=[lab for lab,item in labs["labs"].items() if not item["original_ok"]]
        why=(f"The established payload failed in lab(s) {failed_established}. Separately documented alternatives prove those vulnerabilities and flags remain obtainable, but do not preserve the existing payload results. The verified 16-thread control is retained."
             if failed_established else "At least one candidate performance, reliability, lab or truncation gate failed; the verified 16-thread control is retained.")
    else:
        decision="FURTHER TESTING REQUIRED"
        why="The 24-thread candidate does not pass every gate, and a current safe 16-thread control has not been fully established."
    def evidence(key):
        path=paths[key]
        return f"[{path.name}]({path.relative_to(root) if path.is_relative_to(root) else path})" if path.is_file() else f"NOT RUN: {path}"
    def median_field(rows,key,enabled=None):
        values=[num(row.get(key)) for row in rows if enabled is None or yes(row.get(enabled))]
        values=[value for value in values if value is not None]
        return statistics.median(values) if values else None
    def peak_field(rows,key):
        return max((value for row in rows if (value:=num(row.get(key))) is not None),default=None)
    comparison_text="; ".join(f"{name}: {fmt(item['gain_percent'],'%')} median tok/s change, {item['pairs_faster24']}/5 pairs faster; API latency {fmt(item['latency_change_percent'],'%')}, TTFT {fmt(item['ttft_change_percent'],'%')}" for name,item in fresh['comparisons'].items())
    lines=["# Ollama Final Verification","",f"Generated {dt.datetime.now(dt.timezone.utc).isoformat()} from recorded artifacts. Production configuration changes during verification: **{'NONE' if settings.get('production_unchanged') is True else 'NOT VERIFIED'}**.","",
           *table(["Category","Result","Evidence","Status"],[
               ["CPU optimization",comparison_text,evidence('fresh_results'),status(functions['CPU optimization'])],
               ["Uncached prompt performance","5 fresh short + 5 fresh long requests per setting; actual cached count must be 0; streamed TTFT required",evidence('fresh_results'),status(functions['Uncached prompt performance'])],
               ["30-request reliability",f"24 threads: {reliability['successes']}/{reliability['n']} successful; {reliability['timeouts']} timeouts; {reliability['restarts']} recorded restarts",evidence('reliability_results'),status(functions['30-request reliability'])],
               ["Response truncation",f"Recommended default: {settings.get('recommended_default_output',512)}; overrides: {json.dumps(settings.get('per_lab_output_overrides',{}))}",evidence('lab_results'),status(functions['Response truncation'])],
               ["Nine-lab functionality",f"{sum(item['ok'] for item in labs['labs'].values())}/9 documented cases pass normal/flag/incorrect/No Protection checks",evidence('lab_results'),status(functions['Nine-lab functionality'])],
               ["Docker configuration","Model/quantization/volumes/limits/env and explicit app overrides verified",evidence('preservation'),status(functions['Docker configuration'])],
               ["Established payload compatibility",f"{sum(item['original_ok'] for item in labs['labs'].values())}/9 unchanged payloads earn accepted flags; separately documented alternatives do not erase failures",evidence('lab_results'),status(labs['established_ok'])]
           ]),"","## Fresh CPU measurements","",
           *table(["Threads","Prompt","n","Median tok/s","API latency median / p95","Wall latency median","TTFT median","Output-token counts","Cached-token counts"],
               [[count,name,item['n'],fmt(item['median_tps']),f"{fmt(item['median_latency'],' s')} / {fmt(item['p95'],' s')}",fmt(item['median_wall'],' s'),fmt(item['median_ttft'],' s'),str(item['output_counts']),str(item['cached_counts'])]
                for (count,name),item in sorted(fresh['summaries'].items(),key=lambda pair:(pair[0][0] or 0,pair[0][1]))]),"",
           "Uncached means the API reported `prompt_eval_cached_count=0` for the timed request. Explicit unload/preload operations belong only to this fresh-prompt phase and are outside timed inference; their durations and expected resets remain in raw evidence. This does not mean physically cold CPU/page caches.","",
           "Five observations support a descriptive comparison, not a universal or population-level proof that 24 is faster. Generation speed uses `eval_count/eval_duration`; API latency and client wall latency are separate. TTFT is the first nonempty streamed content frame, not a buffered HTTP-response timestamp. Different achieved output lengths can confound total-latency comparisons despite identical seeds. Positive latency/TTFT percentage changes mean slower responses.","",
           f"32-thread investigation: {settings.get('thread32_conclusion',default_settings()['thread32_conclusion'])} Evidence: {evidence('diagnostics')}.","",
           *table(["Threads / prompt","Median Docker CPU","Median host steal","Runner CPU seconds","Scheduler wait seconds","Peak runnable tasks","Peak runner threads"],
               [[f"{count}/{name}",fmt(median_field(item['good'],'mean_cpu_percent'),'%'),fmt(median_field(item['good'],'host_steal_percent'),'%'),fmt(median_field(item['good'],'runner_cpu_seconds'),' s'),fmt(median_field(item['good'],'runner_sched_wait_seconds_sum','runner_schedstats_enabled'),' s'),fmt(peak_field(item['good'],'host_runnable_peak'),'',0),fmt(peak_field(item['good'],'runner_thread_count_peak'),'',0)]
                for (count,name),item in sorted(fresh['summaries'].items(),key=lambda pair:(pair[0][0] or 0,pair[0][1]))]),"",
           "Scheduler wait is unavailable when kernel schedstats are disabled; zero/disabled counters are not proof of no contention. Host steal, runnable tasks and CPU time are observations, not independent proof of a memory-bandwidth or Xen scheduler cause. Optional32 diagnostics may use a different output cap and fewer repetitions; their achieved output counts are shown above.","",
           f"20/22-thread assessment: {settings.get('threads20_22_conclusion',default_settings()['threads20_22_conclusion'])}","",
           "## Reliability at 24 threads","",
           *table(["Requests","Errors / timeouts","API latency mean / median / p95","Min / max latency","Peak RAM","Mean / peak Docker CPU","Restart / OOM observations","Unexpected reload evidence"],[
               [f"{reliability['successes']}/{reliability['n']}",f"{reliability['n']-reliability['successes']} / {reliability['timeouts']}",f"{fmt(reliability['mean_latency'],' s')} / {fmt(reliability['median_latency'],' s')} / {fmt(reliability['p95'],' s')}",f"{fmt(reliability['minimum_latency'],' s')} / {fmt(reliability['maximum_latency'],' s')}",fmt(reliability['peak_ram']/2**30 if reliability['peak_ram'] is not None else None,' GiB'),f"{fmt(reliability['cpu_mean'],'%')} / {fmt(reliability['peak_cpu'],'%')}",f"{reliability['restarts']} / {reliability['oom']}",f"API load flags={reliability['load_reload_flags']}; PID transitions={len(reliability['runner_audit']['pid_changes'])}; absent={reliability['runner_audit']['absent_observations']}; unknown={reliability['runner_audit']['unknown_observations']}"]]),"",
           f"Observed runner PIDs: `{reliability['runner_audit']['pids']}`. RAM is a sampled container peak, not an absolute kernel peak. Docker 100% means one logical CPU. p95 is nearest rank `ceil(0.95×n)`; with 5 samples it is the maximum, with 30 it is the 29th sorted latency. Errors are counted separately. The previous 16-thread reliability run used a different output cap, so its aggregate latency is not a thread-only comparison.","",
           "## Nine-lab verification","",
           "A PASS requires real model execution and an accepted runtime flag/score award, not HTTP 200. Incorrect attempts must execute against the model and produce no flag; merely rejecting a wrong flag is insufficient. No Protection checks application filtering/hardening separately from independent model refusal. It does not remove the original Lab 9 scenario's privacy instructions: its two earlier model refusals are expected, and the recorded final exploit answer disclosed the fixture information and earned its flag. Refusal detection is a wording heuristic; mixed refusal/compliance may still award a legitimate flag. Each row uses the recommended per-lab output budget; both 256 and 512 remain visible below.","",
           "An incorrect attempt can still disclose a fictional fixture value while correctly failing a lab-specific requirement such as prior refusal history or a planted KB stage. Such a result is not an application-defense block.","",
           "Diagnostic Markdown/apostrophe normalization and broader refusal matching are used only for attribution. `EVALUATOR_REFUSAL_MATCH` denotes a suspected wording mismatch, not semantic proof of valid refusal history; it never changes the original evaluator or awards a flag.","",
           *table(["Lab","Normal","Exploit → accepted flag","Incorrect attempt","No Protection","Model refusal on original attack","Payload/test reference","Status"],
               [[lab,status(item['normal_ok'])+(f" (review; raw {(item['normal'] or {}).get('_normal_review',{}).get('original_result')})" if (item['normal'] or {}).get('_normal_review',{}).get('original_result') not in (None,(item['normal'] or {}).get('result')) else ''),f"{status(item['exploit_ok'])}; flag={'YES' if yes((item['chosen'] or {}).get('flag_awarded')) else 'NO'}"+("; capped" if yes((item['chosen'] or {}).get('truncated')) else ''),status(item['incorrect_ok']),status(item['no_protection_ok']),"YES (mixed/refusal observed)" if item['refusal'] else "NO" if real_receipts(item['original'] or {}) else "NOT RUN",
                 (f"{(item['original'] or {}).get('test_payload_id','NOT RUN')} [{(item['original'] or {}).get('result','NOT RUN')}]"+(f"; {(item['alternate'] or {}).get('test_payload_id')} [{(item['alternate'] or {}).get('result')}]" if item['alternate'] else "")),status(item['ok'])]
                for lab,item in labs['labs'].items()]),"",
           f"Primary payloads/documents: {evidence('original_payload_cases')}. New comparison/incorrect/formatting cases: {evidence('payload_cases')}. Run the recorded verification harness with the same model/options, case ID, planted documents and sequential message order. Original failed attempts remain visible even when a separately named formatting alternate succeeds. Established-payload compatibility: **{status(labs['established_ok'])}**; required for deployment: **{settings.get('require_established_payloads_for_deployment',True)}**.","",
           "```sh",settings.get('lab_reproduction_command',default_settings()['lab_reproduction_command']),"```","",
           "### Offline review of normal-response assertions","",
           f"Evidence: {evidence('normal_review')}. Applied {len(normal_review['applied'])} exact-identity normal reviews; latest normal cases fully reviewed: **{status(normal_review['complete'])}**. The raw results/errors remain unchanged. Review identity binds the full raw-row hash, run/lab/budget/payload, effective options, source/case hashes, ordered actual request hashes, original fixture hash and reviewer/rule source hashes. All 18 criteria must pass for a reviewed PASS: products, fixture prices/materials, no app block/hardening/flag/refusal/transport error, and explicit completion. Reviews can also turn a raw PASS into FAIL. Attack rows and actual flag facts are never overlaid.","",
           *table(["Lab / budget","Raw result → reviewed","Raw assertion/error","Correction reasons","Completion evidence"],
               [[f"{review['lab_number']} / {review['output_limit']}",f"{review['original_result']} → {review['reviewed_result']}",review.get('original_actual_behavior'),', '.join(review.get('correction_reasons',[])) or review.get('reviewed_root_cause'),review.get('completion_classification')]
                for review in normal_review['changed']]),"",
           "The original price assertion rejected a correct amount followed by a sentence-ending period. Recognized price-boundary or natural-EOS-boundary validator errors can be corrected only after every normal criterion passes. This is an offline test-assertion review, not a new model success or a relaxed challenge evaluator.","",
           *table(["Lab","256: explicit limit / EOS at cap / replies","512: explicit limit / EOS at cap / replies","Maximum512 reply tokens","Failure cause / interpretation"],
               [[lab,*[cap_summary(item,cap) for cap in(256,512)],
                 max([int(num(receipt.get('eval_count')) or 0) for row in item['budgets'][512].values() for receipt in real_receipts(row)],default="NOT RUN"),item['root_cause']]
                for lab,item in labs['labs'].items()]),"",
           settings.get('truncation_conclusion',default_settings()['truncation_conclusion']),"",
           "Confirmed limit stops use `done_reason=length/max_tokens/limit`. `done_reason=stop` with token count equal to the configured cap is reported separately as natural EOS at the boundary; count equality alone is not confirmed truncation. Original attack results and flag facts remain unchanged.","",
           "## Deployment checks","",
           f"Context: {settings.get('context_conclusion',default_settings()['context_conclusion'])}","",
           f"Application overrides: {settings.get('app_request_override_evidence','NOT RUN')}. Stored model defaults and app request options are distinct; this report does not silently replace existing per-application settings.","",
           f"16-thread control: {settings.get('control16_evidence',default_settings()['control16_evidence'])}. Nine preserved model/thread proof records present: **{status(control_proofs_ok)}**; this checks record identity, not a fresh replay.","",
           f"Preservation checks: {json.dumps(checks,sort_keys=True)}.","",
           "Model weights, quantization, container identity/start state, named volumes and original backups must be checked against the pre-verification capture. Manifest/reference preservation is not an independent full blob rehash. Expected fresh-test runner resets are excluded from reliability reload claims.","",
           "## Limits and unexecuted checks",""
    ]
    limitations=fresh['missing']+fresh['failures']+labs['missing']+settings.get('notes',[])
    if settings.get("normal_review_required",True) and not normal_review["complete"]:
        limitations.append("Normal-review overlay is NOT RUN or incomplete; raw normal verdicts remain effective where review identity or criteria are unverified.")
    limitations += ["Rejected offline normal review: "+json.dumps(item,sort_keys=True) for item in normal_review['rejected']]
    if not reliability['ok']:
        limitations.append("24-thread reliability gate is incomplete or failed; inspect counts, audit status, monitoring and runner observations above.")
    if not docker_ok:
        limitations.append("Docker/preservation or explicit app-request override evidence is missing or failed.")
    lines += [f"- {value}" for value in dict.fromkeys(limitations)] or ["- No missing mandatory measurement records were detected; this is not a guarantee for untested prompts or future load."]
    lines += ["",why,"",f"**Decision: {decision}.**",""]
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text("\n".join(lines))
    return {"report":str(output),"decision":decision,"categories":functions,"mandatory_missing":mandatory_missing,
            "fresh_issues":fresh['failures'],"reliability":{"successes":reliability['successes'],"requests":reliability['n'],"ok":reliability['ok'],"runner":reliability['runner_audit']},
            "normal_review":{"complete":normal_review['complete'],"applied":len(normal_review['applied']),"changed":[{key:review.get(key) for key in ('run_id','lab_number','output_limit','original_result','reviewed_result','correction_reasons')} for review in normal_review['changed']],"rejected":normal_review['rejected']},
            "labs_passed":sum(item['ok'] for item in labs['labs'].values()),"established_payloads_passed":sum(item['original_ok'] for item in labs['labs'].values())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default=".")
    parser.add_argument("--settings",default="performance/final-verification/report-settings.json")
    parser.add_argument("--output",default="OLLAMA_FINAL_VERIFICATION.md")
    parser.add_argument("--summary",default="performance/final-verification/report-summary.json")
    parser.add_argument("--write-settings-example",action="store_true",help="Write the settings schema only; does not generate measurements")
    args=parser.parse_args()
    root=Path(args.root).resolve()
    settings_path=(root/args.settings).resolve()
    if args.write_settings_example:
        settings_path.parent.mkdir(parents=True,exist_ok=True)
        settings_path.write_text(json.dumps(default_settings(),indent=2)+"\n")
        print(settings_path)
        return
    settings=load_json(settings_path,default_settings())
    result=write(root,settings,(root/args.output).resolve())
    summary=(root/args.summary).resolve()
    summary.parent.mkdir(parents=True,exist_ok=True)
    summary.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
