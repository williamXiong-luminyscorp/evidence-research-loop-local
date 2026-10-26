from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path


def percentile(values, fraction):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def main():
    candidate = Path(sys.argv[1]).resolve()
    result_dir = Path(sys.argv[2]).resolve()
    split = sys.argv[3]
    seed_sets = json.loads((Path(__file__).resolve().parent / "seeds.json").read_text(encoding="utf-8"))
    seeds = [int(value) for value in seed_sets[split]]
    spec = importlib.util.spec_from_file_location("erl_candidate", candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "solve", None)):
        raise ValueError("candidate must define callable solve(values)")
    records = []
    all_correct = True
    durations = []
    behavior = hashlib.sha256()
    for seed in seeds:
        rng = random.Random(seed)
        size = 900 + seed % 7 * 100
        values = [rng.randint(-700, 700) for _ in range(size)]
        expected = sorted(set(values))
        started = time.perf_counter_ns()
        actual = module.solve(list(values))
        duration = time.perf_counter_ns() - started
        correct = type(actual) is list and actual == expected
        all_correct = all_correct and correct
        output_hash = hashlib.sha256(json.dumps(actual, separators=(",", ":")).encode()).hexdigest()
        behavior.update(output_hash.encode())
        durations.append(duration)
        records.append({"seed": seed, "input_count": size, "correct": correct,
                        "actual_elapsed_ns": duration, "output_sha256": output_hash})
    metrics = {
        "median_elapsed_ns": float(statistics.median(durations)),
        "p95_elapsed_ns": float(percentile(durations, 0.95)),
        "correct_case_count": sum(row["correct"] for row in records),
        "case_count": len(records),
        "behavior_digest": behavior.hexdigest(),
        "repeat_index": os.environ.get("ERL_REPEAT_INDEX"),
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "correct.json").write_text(json.dumps({"correct": all_correct}, indent=2) + "\n")
    (result_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (result_dir / "raw-results.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
