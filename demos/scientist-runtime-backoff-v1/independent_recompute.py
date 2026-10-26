#!/usr/bin/env python3
"""Recompute the backoff study directly from raw rows and compare the report."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("backoff_adapter", ROOT / "stage_adapter.py")
adapter = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(adapter)


def main() -> None:
    state = adapter.read_json(ROOT / "scientist-state.json")
    experiment_path = ROOT / state["stages"]["experiment"]["output_path"]
    experiment = adapter.read_json(experiment_path)
    raw_path = experiment_path.parent / experiment["experiments"][0]["evidence_artifacts"][0]["path"]
    protocol = adapter.read_json(ROOT / "protocol.json")
    recomputed = adapter.analyze_rows(adapter.read_json(raw_path)["rows"], protocol)
    analysis = adapter.read_json(ROOT / "analysis-results.json")["analysis"]
    result = {
        "matches_recorded_analysis": recomputed == analysis,
        "selected_policy_id": recomputed["selected_policy_id"],
        "selected_p95_success_latency_ms": recomputed["selected_p95_success_latency_ms"],
        "eligible_policy_ids": recomputed["eligible_policy_ids"],
        "raw_observation_count": sum(row["seed_count"] for row in recomputed["summaries"]),
    }
    (ROOT / "independent-recompute.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["matches_recorded_analysis"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
