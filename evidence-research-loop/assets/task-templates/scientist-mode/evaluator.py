from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path


MODULES = ["ideation", "literature", "tree_manager", "analysis", "paper_review"]


def run_case(seed, config):
    known = f"known-{seed % 4}"
    novel = f"novel-{seed}"
    proposals = [known, novel] if config["ideation"] else [known]
    literature_hits = [known] if config["literature"] else []
    if config["literature"]:
        proposals = [item for item in proposals if item not in literature_hits]
    selected = proposals[0] if proposals else None
    novel_selected = selected == novel

    branch_results = [
        {"branch": "quick-check", "status": "failed", "effect": None},
        {"branch": "single-replicate", "status": "ok", "effect": -0.18 - (seed % 3) * 0.01},
        {"branch": "controlled-a", "status": "ok", "effect": 0.24 + (seed % 5) * 0.005},
        {"branch": "controlled-b", "status": "ok", "effect": 0.27 + (seed % 7) * 0.004},
    ]
    explored = []
    if selected is not None:
        explored = branch_results if config["tree_manager"] else branch_results[:1]
    successful_effects = [row["effect"] for row in explored if row["status"] == "ok"]
    experiment_succeeded = bool(successful_effects)

    analyzed_effect = None
    if successful_effects:
        if config["analysis"]:
            analyzed_effect = statistics.median(successful_effects)
        else:
            analyzed_effect = successful_effects[0]
    conclusion_supported = bool(
        novel_selected and analyzed_effect is not None and analyzed_effect >= 0.20
    )
    package_complete = bool(config["paper_review"] and conclusion_supported)
    reviewer_rejected = bool(config["paper_review"] and not conclusion_supported)
    utility = (
        0.25 * float(novel_selected)
        + 0.25 * float(experiment_succeeded)
        + 0.25 * float(conclusion_supported)
        + 0.25 * float(package_complete)
    )
    stage_cost = (
        int(config["ideation"]) + int(config["literature"])
        + len(explored) + int(config["analysis"]) + int(config["paper_review"])
    )
    return {
        "seed": seed,
        "selected_hypothesis": selected,
        "literature_hits": literature_hits,
        "novel_hypothesis": novel_selected,
        "explored_branches": explored,
        "experiment_succeeded": experiment_succeeded,
        "analyzed_effect": analyzed_effect,
        "conclusion_supported": conclusion_supported,
        "package_complete": package_complete,
        "reviewer_rejected": reviewer_rejected,
        "structural_coverage_score": utility,
        "stage_cost": stage_cost,
    }


def main():
    candidate_path = Path(sys.argv[1]).resolve()
    result_dir = Path(sys.argv[2]).resolve()
    split = sys.argv[3]
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    valid_shape = set(candidate) == set(MODULES) and all(type(candidate[key]) is bool for key in MODULES)
    seed_sets = json.loads((Path(__file__).parent / "seeds.json").read_text(encoding="utf-8"))
    seeds = [int(value) for value in seed_sets[split]]
    records = [run_case(seed, candidate) for seed in seeds] if valid_shape else []
    count = len(records)
    mean = lambda key: sum(float(row[key]) for row in records) / count if count else math.nan
    behavior_payload = [
        {
            "selected": row["selected_hypothesis"],
            "novel": row["novel_hypothesis"],
            "experiment": row["experiment_succeeded"],
            "supported": row["conclusion_supported"],
            "package": row["package_complete"],
        }
        for row in records
    ]
    metrics = {
        "mean_structural_coverage_score": mean("structural_coverage_score"),
        "novel_hypothesis_rate": mean("novel_hypothesis"),
        "experiment_success_rate": mean("experiment_succeeded"),
        "supported_conclusion_rate": mean("conclusion_supported"),
        "complete_package_rate": mean("package_complete"),
        "mean_stage_cost": mean("stage_cost"),
        "enabled_module_count": sum(candidate.values()) if valid_shape else 0,
        "scenario_count": count,
        "behavior_digest": hashlib.sha256(
            json.dumps(behavior_payload, sort_keys=True).encode()
        ).hexdigest(),
        "repeat_index": os.environ.get("ERL_REPEAT_INDEX"),
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "correct.json").write_text(
        json.dumps({"correct": valid_shape and count == len(seeds)}, indent=2) + "\n"
    )
    (result_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (result_dir / "raw-results.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
