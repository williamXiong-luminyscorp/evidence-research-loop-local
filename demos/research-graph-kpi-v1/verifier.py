#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
from typing import Any


REQUIRED_CATEGORIES = {"nominal", "lower_boundary", "upper_boundary", "missing", "out_of_range", "unknown_extra"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def active_asset(project: Path, node_id: str) -> tuple[str, Path]:
    graph = read_json(project / "research-graph.json")
    asset_id = graph["nodes"][node_id]["active_asset_id"]
    if not asset_id:
        raise ValueError(f"dependency {node_id} has no active verified asset")
    asset = read_json(project / "verified-assets.json")["assets"][asset_id]
    return asset_id, project / asset["artifact_path"]


def validate_contract(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return ["contract must be a schema_version 1 object"]
    components = value.get("components")
    if not isinstance(components, list) or len(components) < 3:
        errors.append("contract needs at least three components")
        return errors
    names = [row.get("id") for row in components if isinstance(row, dict)]
    if len(names) != len(components) or any(not isinstance(name, str) or not name for name in names):
        errors.append("every component needs a non-empty id")
    if len(set(names)) != len(names):
        errors.append("component ids must be unique")
    weights = [row.get("weight") for row in components if isinstance(row, dict)]
    if len(weights) != len(components) or any(not isinstance(weight, (int, float)) or weight <= 0 for weight in weights):
        errors.append("component weights must be positive numbers")
    elif not math.isclose(sum(float(weight) for weight in weights), 1.0, abs_tol=1e-9):
        errors.append("component weights must sum to one")
    if value.get("score_range") != {"minimum": 0, "maximum": 100}:
        errors.append("score_range must be exactly 0..100")
    if value.get("missing_input_policy") != "reject":
        errors.append("missing_input_policy must be reject")
    return errors


def validate_cases(value: Any) -> list[str]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return ["test case artifact must be a schema_version 1 object"]
    cases = value.get("cases")
    if not isinstance(cases, list) or len(cases) < 6:
        return ["at least six cases are required"]
    categories = {row.get("category") for row in cases if isinstance(row, dict)}
    errors = []
    missing = REQUIRED_CATEGORIES - categories
    if missing:
        errors.append(f"missing categories: {sorted(missing)}")
    ids = [row.get("case_id") for row in cases if isinstance(row, dict)]
    if len(ids) != len(cases) or len(set(ids)) != len(ids) or any(not value for value in ids):
        errors.append("case ids must be present and unique")
    for row in cases:
        if not isinstance(row, dict) or not isinstance(row.get("expected_behavior"), str):
            errors.append("each case must explain expected_behavior")
            break
    return errors


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("candidate_kpi_calculator", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot import calculator artifact")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "calculate", None)):
        raise ValueError("artifact must define calculate(scores, contract)")
    return module


def exercise_calculator(path: Path, contract: dict[str, Any]) -> list[str]:
    module = load_module(path)
    names = [row["id"] for row in contract["components"]]
    weights = {row["id"]: float(row["weight"]) for row in contract["components"]}
    errors: list[str] = []
    nominal = {name: float(20 + index * 20) for index, name in enumerate(names)}
    expected = sum(nominal[name] * weights[name] for name in names)
    try:
        observed = module.calculate(dict(nominal), contract)
        if not isinstance(observed, (int, float)) or not math.isclose(float(observed), expected, abs_tol=1e-9):
            errors.append("nominal weighted result is incorrect")
        lower = module.calculate({name: 0 for name in names}, contract)
        upper = module.calculate({name: 100 for name in names}, contract)
        if lower != 0 or upper != 100:
            errors.append("0/100 boundaries are incorrect")
    except Exception as exc:
        errors.append(f"valid calculation raised: {exc}")
    invalid_inputs = [
        {name: 50 for name in names[:-1]},
        {**{name: 50 for name in names}, names[0]: -1},
        {**{name: 50 for name in names}, names[0]: 101},
        {**{name: 50 for name in names}, "unknown-component": 50},
    ]
    for index, scores in enumerate(invalid_inputs):
        try:
            module.calculate(scores, contract)
            errors.append(f"invalid input {index} was accepted")
        except (TypeError, ValueError, KeyError):
            pass
        except Exception as exc:
            errors.append(f"invalid input {index} raised unexpected {type(exc).__name__}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--result-dir", required=True)
    args = parser.parse_args()
    project = Path(args.project)
    artifact = Path(args.artifact)
    errors: list[str] = []
    details: dict[str, Any] = {}
    try:
        if args.node == "metric-contract":
            errors.extend(validate_contract(read_json(artifact)))
        elif args.node == "adversarial-cases":
            errors.extend(validate_cases(read_json(artifact)))
        elif args.node == "calculator-implementation":
            _, contract_path = active_asset(project, "metric-contract")
            _, cases_path = active_asset(project, "adversarial-cases")
            contract = read_json(contract_path)
            errors.extend(validate_contract(contract))
            errors.extend(validate_cases(read_json(cases_path)))
            errors.extend(exercise_calculator(artifact, contract))
        elif args.node == "integration-evidence":
            report = read_json(artifact)
            expected_assets = {}
            for dependency in ("metric-contract", "adversarial-cases", "calculator-implementation"):
                asset_id, asset_path = active_asset(project, dependency)
                expected_assets[dependency] = asset_id
                if dependency == "calculator-implementation":
                    implementation_path = asset_path
                elif dependency == "metric-contract":
                    contract_path = asset_path
            if report.get("dependency_assets") != expected_assets:
                errors.append("integration report does not name the exact dependency assets")
            errors.extend(exercise_calculator(implementation_path, read_json(contract_path)))
            if report.get("independent_test_status") != "passed":
                errors.append("integration report must record independent_test_status=passed")
            details["dependency_assets"] = expected_assets
        else:
            errors.append(f"unsupported node {args.node}")
    except Exception as exc:
        errors.append(f"verifier exception: {type(exc).__name__}: {exc}")
    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(json.dumps({
        "hard_pass": not errors,
        "node_id": args.node,
        "errors": errors,
        "details": details,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
