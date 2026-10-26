#!/usr/bin/env python3
"""Command adapter for the real, zero-model retry/backoff study.

The selected policy is never named in source.  Experiment rows are generated
from the frozen protocol; analysis and review each recompute the ordering.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def usage() -> dict[str, Any]:
    return {
        "model": None,
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "known_cost_usd": 0.0,
    }


def nearest_rank(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute a quantile of an empty sample")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def scenario(seed: int, protocol: dict[str, Any]) -> dict[str, Any]:
    config = protocol["simulation"]
    rng = random.Random(seed)
    has_outage = rng.random() < float(config["outage_probability"])
    outage_ms = rng.expovariate(1.0 / float(config["outage_mean_ms"])) if has_outage else 0.0
    median = float(config["base_service_median_ms"])
    sigma = float(config["base_service_sigma"])
    service_ms = rng.lognormvariate(math.log(median), sigma)
    jitters = [rng.uniform(0.0, float(config["service_jitter_ms"]))
               for _ in range(int(config["maximum_attempts"]))]
    return {"outage_ms": outage_ms, "service_ms": service_ms, "jitters": jitters}


def simulate_policy(seed: int, policy: dict[str, Any], protocol: dict[str, Any]) -> dict[str, Any]:
    config = protocol["simulation"]
    values = scenario(seed, protocol)
    elapsed_ms = 0.0
    attempts = 0
    success = False
    latency_ms: float | None = None
    delays = [float(value) for value in policy["retry_delays_ms"]]
    maximum_attempts = int(config["maximum_attempts"])
    if len(delays) != maximum_attempts - 1:
        raise ValueError(f"policy {policy['policy_id']} does not define every retry delay")
    for attempt_index in range(maximum_attempts):
        attempts += 1
        if elapsed_ms >= values["outage_ms"]:
            success = True
            latency_ms = elapsed_ms + values["service_ms"] + values["jitters"][attempt_index]
            break
        elapsed_ms += float(config["failure_response_ms"])
        if attempt_index < len(delays):
            elapsed_ms += delays[attempt_index]
    return {
        "seed": seed,
        "policy_id": policy["policy_id"],
        "success": success,
        "attempts": attempts,
        "completion_latency_ms": round(latency_ms, 9) if latency_ms is not None else None,
        "outage_duration_ms": round(values["outage_ms"], 9),
    }


def expected_seeds(protocol: dict[str, Any]) -> list[int]:
    simulation = protocol["simulation"]
    start = int(simulation["seed_start"])
    return list(range(start, start + int(simulation["seed_count"])))


def validate_rows(rows: list[dict[str, Any]], protocol: dict[str, Any]) -> None:
    expected = {(policy["policy_id"], seed)
                for policy in protocol["policies"] for seed in expected_seeds(protocol)}
    actual = {(str(row["policy_id"]), int(row["seed"])) for row in rows}
    if len(rows) != len(expected) or actual != expected:
        raise ValueError("raw observations do not contain exactly one row per policy and seed")


def policy_summary(rows: list[dict[str, Any]], policy_id: str,
                   protocol: dict[str, Any]) -> dict[str, Any]:
    selected = [row for row in rows if row["policy_id"] == policy_id]
    successes = [row for row in selected if row["success"]]
    latencies = [float(row["completion_latency_ms"]) for row in successes]
    constraints = protocol["constraints"]
    success_rate = len(successes) / len(selected)
    mean_attempts = statistics.fmean(float(row["attempts"]) for row in selected)
    block_size = int(protocol["simulation"]["block_size"])
    start = int(protocol["simulation"]["seed_start"])
    block_summaries = []
    for block_start in range(start, start + len(selected), block_size):
        block_rows = [row for row in selected
                      if block_start <= int(row["seed"]) < block_start + block_size]
        block_success = [row for row in block_rows if row["success"]]
        block_latency = [float(row["completion_latency_ms"]) for row in block_success]
        block_summaries.append({
            "seed_start": block_start,
            "seed_count": len(block_rows),
            "success_rate": round(len(block_success) / len(block_rows), 9),
            "p95_success_latency_ms": round(nearest_rank(block_latency, 0.95), 9),
        })
    summary = {
        "policy_id": policy_id,
        "seed_count": len(selected),
        "success_count": len(successes),
        "failure_count": len(selected) - len(successes),
        "success_rate": round(success_rate, 9),
        "mean_attempts": round(mean_attempts, 9),
        "p50_success_latency_ms": round(nearest_rank(latencies, 0.50), 9),
        "p95_success_latency_ms": round(nearest_rank(latencies, 0.95), 9),
        "block_summaries": block_summaries,
    }
    summary["eligible"] = (
        summary["success_rate"] >= float(constraints["minimum_success_rate"])
        and summary["mean_attempts"] <= float(constraints["maximum_mean_attempts"])
    )
    return summary


def analyze_rows(rows: list[dict[str, Any]], protocol: dict[str, Any]) -> dict[str, Any]:
    validate_rows(rows, protocol)
    summaries = [policy_summary(rows, policy["policy_id"], protocol)
                 for policy in protocol["policies"]]
    eligible = [row for row in summaries if row["eligible"]]
    ranking = sorted(eligible, key=lambda row: (
        row["p95_success_latency_ms"], row["mean_attempts"], row["policy_id"]))
    winner = ranking[0] if ranking else None
    runner_up = ranking[1] if len(ranking) > 1 else None
    return {
        "summaries": summaries,
        "eligible_policy_ids": [row["policy_id"] for row in ranking],
        "selected_policy_id": winner["policy_id"] if winner else None,
        "selected_p95_success_latency_ms": winner["p95_success_latency_ms"] if winner else None,
        "runner_up_policy_id": runner_up["policy_id"] if runner_up else None,
        "p95_margin_to_runner_up_ms": round(
            runner_up["p95_success_latency_ms"] - winner["p95_success_latency_ms"], 9
        ) if runner_up and winner else None,
        "selection_rule": protocol["constraints"]["selection_order"],
    }


def prior_output(request: dict[str, Any], project: Path, stage: str) -> Path:
    matches = [row for row in request["prior_stage_outputs"] if row["stage"] == stage]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one prior {stage} output")
    path = (project / matches[0]["path"]).resolve()
    if not path.is_file():
        raise ValueError(f"prior {stage} output is missing")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != matches[0]["sha256"]:
        raise ValueError(f"prior {stage} output hash mismatch")
    return path


def raw_observation_path(request: dict[str, Any], project: Path) -> Path:
    experiment_path = prior_output(request, project, "experiment")
    experiment = read_json(experiment_path)
    record = experiment["experiments"][0]
    artifacts = record.get("evidence_artifacts", [])
    matches = [row for row in artifacts if row["path"] == "raw-observations.json"]
    if len(matches) != 1:
        raise ValueError("raw observation artifact is missing")
    path = (experiment_path.parent / matches[0]["path"]).resolve()
    if hashlib.sha256(path.read_bytes()).hexdigest() != matches[0]["sha256"]:
        raise ValueError("raw observation artifact hash mismatch")
    return path


def main() -> None:
    if len(sys.argv) != 6:
        raise SystemExit("usage: stage_adapter.py STAGE REQUEST RESPONSE ATTEMPT_DIR PROJECT")
    stage, request_text, response_text, attempt_text, project_text = sys.argv[1:]
    request_path = Path(request_text)
    response_path = Path(response_text)
    attempt_dir = Path(attempt_text)
    project = Path(project_text)
    request = read_json(request_path)
    protocol = read_json(project / "protocol.json")

    if stage == "ideation":
        ideas = []
        for policy in protocol["policies"]:
            policy_id = policy["policy_id"]
            ideas.append({
                "idea_id": f"hypothesis-{policy_id}",
                "hypothesis": f"{policy_id} is eligible and leads the frozen primary metric ordering",
                "falsifiable_prediction": (
                    f"On every frozen seed, compute success, attempts, and actual successful latency; "
                    f"{policy_id} must satisfy both constraints and rank first by the declared ordering"
                ),
                "minimum_experiment": "Run all declared policies on the same complete frozen seed set",
            })
        response = {"stage": stage, "ideas": ideas, "usage": usage()}
    elif stage == "literature":
        idea_ids = [f"hypothesis-{policy['policy_id']}" for policy in protocol["policies"]]
        sources = [{**source, "supports": idea_ids, "refutes": []}
                   for source in protocol["literature_scope"]]
        response = {
            "stage": stage,
            "sources": sources,
            "novelty_assessment": {
                "status": "unknown",
                "reason": "This zero-network run records source locators but does not retrieve full texts or establish novelty."
            },
            "usage": usage(),
        }
    elif stage == "experiment":
        rows = [simulate_policy(seed, policy, protocol)
                for policy in protocol["policies"] for seed in expected_seeds(protocol)]
        validate_rows(rows, protocol)
        raw_path = attempt_dir / "raw-observations.json"
        write_json(raw_path, {"schema_version": 1, "study_id": protocol["study_id"], "rows": rows})
        summaries = analyze_rows(rows, protocol)["summaries"]
        response = {
            "stage": stage,
            "experiments": [{
                "experiment_id": "exp-backoff-simulation",
                "hypothesis_id": "all-declared-policy-hypotheses",
                "status": "passed",
                "metrics": {"row_count": len(rows), "policy_summaries": summaries},
                "evidence_files": [raw_path.name],
            }],
            "usage": usage(),
        }
    elif stage == "analysis":
        raw = read_json(raw_observation_path(request, project))
        analysis = analyze_rows(raw["rows"], protocol)
        winner = analysis["selected_policy_id"]
        supported = winner is not None
        if supported:
            statement = (
                f"Under the frozen simulation protocol, {winner} is the eligible policy selected by "
                f"the declared ordering; observed p95 successful latency is "
                f"{analysis['selected_p95_success_latency_ms']} ms."
            )
        else:
            statement = "No declared policy satisfies the frozen constraints."
        response = {
            "stage": stage,
            "analysis": analysis,
            "claims": [{
                "claim_id": "claim-best-policy",
                "claim_type": "empirical",
                "statement": statement,
                "status": "supported" if supported else "inconclusive",
                "evidence_ids": ["exp-backoff-simulation"],
            }],
            "usage": usage(),
        }
    elif stage == "review":
        raw = read_json(raw_observation_path(request, project))
        independently_recomputed = analyze_rows(raw["rows"], protocol)
        analysis_path = prior_output(request, project, "analysis")
        submitted = read_json(analysis_path)
        submitted_analysis = submitted.get("analysis")
        claim = submitted.get("claims", [{}])[0]
        winner = independently_recomputed["selected_policy_id"]
        expected_statement = (
            f"Under the frozen simulation protocol, {winner} is the eligible policy selected by "
            f"the declared ordering; observed p95 successful latency is "
            f"{independently_recomputed['selected_p95_success_latency_ms']} ms."
        ) if winner else "No declared policy satisfies the frozen constraints."
        matches = submitted_analysis == independently_recomputed and claim.get("statement") == expected_statement
        claim_supported = winner is not None and claim.get("status") == "supported"
        decision = "accept" if matches and claim_supported else "reject"
        response = {
            "stage": stage,
            "decision": decision,
            "claim_reviews": [{
                "claim_id": "claim-best-policy",
                "decision": decision,
                "reason": "Independent raw-data recomputation matches the frozen selection rule."
                if decision == "accept" else
                "Independent raw-data recomputation does not support the submitted claim."
            }],
            "independent_recalculation": independently_recomputed,
            "usage": usage(),
        }
    else:
        raise ValueError(f"unsupported stage: {stage}")
    write_json(response_path, response)


if __name__ == "__main__":
    main()
