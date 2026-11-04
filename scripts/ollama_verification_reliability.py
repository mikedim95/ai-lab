#!/usr/bin/env python3
"""Run the existing sequential benchmark with an explicit reliability output cap.

Only request options change temporarily. This wrapper never edits the imported
benchmark, stored model, Compose files, or application settings. Default is plan.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ollama_benchmark as benchmark

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--verification-output-limit", type=int, default=512)
own, rest = parser.parse_known_args()
if own.verification_output_limit not in (128, 256, 512):
    raise SystemExit("Choose an explicit tested output cap: 128, 256, or 512")
original_cases = benchmark.cases_for


def verification_cases(phase, threads, args):
    cases = original_cases(phase, threads, args)
    if phase == "reliability":
        for case in cases:
            case["output_limit"] = own.verification_output_limit
            case["test_id"] = "V_RELIABILITY_24" if threads == 24 else "V_RELIABILITY_" + str(threads)
    return cases


benchmark.cases_for = verification_cases
sys.argv = [sys.argv[0], *rest]
if __name__ == "__main__":
    raise SystemExit(benchmark.main())
