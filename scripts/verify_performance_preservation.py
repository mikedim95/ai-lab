#!/usr/bin/env python3
"""Collect read-only final preservation evidence; never changes Docker/model state."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--base-url", default="http://172.21.0.2:11434")
    parser.add_argument("--output", default="performance/final-preservation.json")
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--context", type=int, default=8192)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    old = root / "performance/original"

    def command(*parts):
        return subprocess.check_output(parts, text=True, timeout=30).strip()

    def api(path, body=None):
        encoded = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(args.base_url.rstrip("/") + path, data=encoded,
                                        headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    original_container = json.loads((old / "ollama-container-inspect.json").read_text())[0]
    current = json.loads(command("docker", "inspect", "aigoat-ollama-1"))[0]
    original_model = json.loads((old / "model-show.json").read_text())
    shown = api("/api/show", {"model": "ministral-3:3b"})
    tags = {model["name"]: model for model in api("/api/tags")["models"]}
    digest = json.loads((old / "api-ps.json").read_text())["models"][0]["digest"]
    old_hashes = json.loads((old / "source-sha256.json").read_text())
    actual_hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in old_hashes}
    changed = [name for name in old_hashes if old_hashes[name] != actual_hashes[name]]
    original_volumes = {line.split()[-1] for line in (old / "volumes.txt").read_text().splitlines()[1:] if line.strip()}
    current_volumes = set(command("docker", "volume", "ls", "-q").splitlines())
    missing_volumes = sorted(original_volumes - current_volumes)
    mount = lambda record: sorted((item.get("Name", item.get("Source")), item["Destination"])
                                  for item in record["Mounts"])
    source = next(line[5:] for line in original_model["modelfile"].splitlines() if line.startswith("FROM "))
    blob_exists = subprocess.run(["docker", "exec", "aigoat-ollama-1", "test", "-s", source],
                                 check=False, timeout=30).returncode == 0
    environment = dict(item.split("=", 1) for item in current["Config"]["Env"] if "=" in item)
    app = json.loads(command("docker", "inspect", "nine-labs-app-1"))[0]
    app_env = dict(item.split("=", 1) for item in app["Config"]["Env"] if "=" in item)
    active = api("/api/ps")
    loaded = active.get("models", [])
    runner_listing = command("docker", "top", "aigoat-ollama-1", "-eo", "pid,args")
    server_unchanged = (root / "server.py").read_bytes() == (old / "server.py").read_bytes()
    target_limits = {key: current["HostConfig"].get(key) for key in
                     ("Memory", "NanoCpus", "CpuQuota", "CpuPeriod", "CpusetCpus")}
    unlimited = all(target_limits[key] in (0, "", None) for key in target_limits)
    weights_preserved = tags["ministral-3:3b"]["digest"] == digest and shown["modelfile"] == original_model["modelfile"] and blob_exists
    checks = {
        "model_weights_unchanged": weights_preserved,
        "quantization_unchanged": shown["details"]["quantization_level"] == original_model["details"]["quantization_level"] == "Q4_K_M",
        "volumes_unchanged": not missing_volumes and mount(current) == mount(original_container),
        "lab_objectives_unchanged": not changed and server_unchanged,
        "evaluators_unchanged": not changed and server_unchanged,
        "backups_retained": "ministral-3:3b-before-performance" in tags and not missing_volumes,
        "model_parameters_unchanged": shown["parameters"] == original_model["parameters"],
        "ollama_image_unchanged": current["Image"] == original_container["Image"],
        "ollama_unlimited_cpu_ram": unlimited,
        "single_parallel_model_enforced": environment.get("OLLAMA_NUM_PARALLEL") == "1" and environment.get("OLLAMA_MAX_LOADED_MODELS") == "1",
        "keep_alive_24h": environment.get("OLLAMA_KEEP_ALIVE") == "24h",
        "same_ollama_container_instance_and_start": current["Id"] == original_container["Id"] and current["State"]["StartedAt"] == original_container["State"]["StartedAt"] and current["State"]["Pid"] == original_container["State"]["Pid"] and current["RestartCount"] == original_container["RestartCount"],
        "no_current_ollama_oom_state": not current["State"]["OOMKilled"],
        "application_profile_matches_selection": app_env.get("OLLAMA_MODEL") == "ministral-3:3b" and app_env.get("OLLAMA_NUM_THREAD") == str(args.threads) and app_env.get("OLLAMA_CONTEXT_LENGTH") == str(args.context) and app_env.get("OLLAMA_KEEP_ALIVE") == "24h" and app_env.get("OLLAMA_URL") == "http://aigoat-ollama-1:11434" and server_unchanged,
        "one_selected_cpu_model_active": len(loaded) == 1 and loaded[0].get("name") == "ministral-3:3b" and loaded[0].get("digest") == digest and loaded[0].get("context_length") == args.context and loaded[0].get("size_vram") == 0,
        "active_runner_thread_context_match": bool(re.search(r" -t " + str(args.threads) + r"(?: |$)", runner_listing)) and bool(re.search(r" -c " + str(args.context) + r"(?: |$)", runner_listing)),
    }
    report = {
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": checks,
        "all_checks_passed": all(checks.values()), "model_digest": tags["ministral-3:3b"]["digest"],
        "weight_verification_method": "Unchanged model manifest digest, Modelfile content-addressed blob reference and nonempty blob existence; no independent full weight rehash was performed.",
        "crash_check_scope": "Container identity, process ID, start timestamp and restart count compared with the snapshot; current OOM state checked. This alone is not a complete historical child-process OOM audit. Sequential reliability monitoring provides separate evidence for its request interval.",
        "source_hash_count": len(old_hashes), "changed_sources": changed, "actual_source_hashes": actual_hashes,
        "missing_volumes": missing_volumes, "volume_names": sorted(current_volumes),
        "model_names": sorted(tags), "original_mounts": mount(original_container), "final_mounts": mount(current),
        "container_image": current["Image"], "limits": target_limits,
        "ollama_environment": {key: value for key, value in environment.items() if key.startswith("OLLAMA_")},
        "application_environment": {key: value for key, value in app_env.items() if key.startswith("OLLAMA_")},
        "ollama_state": current["State"], "ollama_restart_count": current["RestartCount"],
        "model_parameters": shown["parameters"], "model_details": shown["details"],
        "active_models": active, "runner_processes": runner_listing,
        "backup_verification_scope": "Original named backup tag remains present; its weight bytes were not independently rehashed.",
    }
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(output), "checks": checks, "all_checks_passed": all(checks.values())}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
