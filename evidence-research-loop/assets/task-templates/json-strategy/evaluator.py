from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
from pathlib import Path


def main():
    candidate_path = Path(sys.argv[1]).resolve()
    result_dir = Path(sys.argv[2]).resolve()
    split = sys.argv[3]
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    required = {"safety_capacity", "batch_size", "review_period"}
    valid_shape = set(candidate) == required and all(type(candidate[key]) is int for key in required)
    bounds = (valid_shape and 0 <= candidate["safety_capacity"] <= 40
              and 1 <= candidate["batch_size"] <= 60 and 1 <= candidate["review_period"] <= 7)
    seed_sets = json.loads((Path(__file__).resolve().parent / "seeds.json").read_text(encoding="utf-8"))
    seeds = [int(value) for value in seed_sets[split]]
    records = []
    if bounds:
        for seed in seeds:
            rng = random.Random(seed)
            pressure = rng.uniform(-0.012, 0.012)
            service = min(1.0, max(0.0, 0.80 + 0.01 * candidate["safety_capacity"]
                                   - 0.015 * (candidate["review_period"] - 1) + pressure))
            demand_factor = rng.uniform(0.96, 1.04)
            cost = ((10.0 * candidate["safety_capacity"]
                    + 400.0 / candidate["batch_size"] + 4.0 * candidate["batch_size"]
                    + 5.0 * candidate["review_period"]) * demand_factor)
            committed = candidate["safety_capacity"] + candidate["batch_size"] / 2.0
            records.append({"seed": seed, "service_level": service, "total_cost": cost,
                            "committed_capacity": committed})
    min_service = min((row["service_level"] for row in records), default=0.0)
    mean_cost = sum((row["total_cost"] for row in records), 0.0) / len(records) if records else math.inf
    mean_capacity = (sum((row["committed_capacity"] for row in records), 0.0) / len(records)
                     if records else math.inf)
    behavior_payload = [{"service": round(row["service_level"], 6), "capacity": row["committed_capacity"]}
                        for row in records]
    metrics = {"minimum_service_level": min_service, "mean_total_cost": mean_cost,
               "mean_committed_capacity": mean_capacity,
               "behavior_digest": hashlib.sha256(json.dumps(behavior_payload, sort_keys=True).encode()).hexdigest(),
               "scenario_count": len(records), "repeat_index": os.environ.get("ERL_REPEAT_INDEX")}
    correct = bounds and min_service >= 0.95
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "correct.json").write_text(json.dumps({"correct": correct}, indent=2) + "\n")
    (result_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (result_dir / "raw-results.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
