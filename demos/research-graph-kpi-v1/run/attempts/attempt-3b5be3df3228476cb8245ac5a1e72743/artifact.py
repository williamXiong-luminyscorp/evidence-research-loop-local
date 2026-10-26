"""Validated KPI score calculation for the frozen metric contract."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any


_REQUIRED_CONTRACT_FIELDS = {
    "schema_version",
    "contract_version",
    "components",
    "score_range",
    "missing_input_policy",
    "unknown_input_policy",
    "out_of_range_input_policy",
    "weighting_method",
}


def _finite_number(value: Any, label: str) -> float:
    """Return *value* as a finite float, rejecting booleans and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{label} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _validated_contract(contract: Any) -> tuple[list[tuple[str, float]], float, float]:
    if not isinstance(contract, Mapping):
        raise TypeError("contract must be a mapping")

    missing_fields = _REQUIRED_CONTRACT_FIELDS.difference(contract)
    if missing_fields:
        names = ", ".join(sorted(missing_fields))
        raise ValueError(f"contract is missing required field(s): {names}")

    if contract["schema_version"] != 1:
        raise ValueError("unsupported contract schema_version")
    if not isinstance(contract["contract_version"], str) or not contract["contract_version"].strip():
        raise ValueError("contract_version must be a non-empty string")
    if contract["weighting_method"] != "weighted_arithmetic_mean":
        raise ValueError("unsupported weighting_method")

    required_policies = {
        "missing_input_policy": "reject",
        "unknown_input_policy": "reject",
        "out_of_range_input_policy": "reject",
    }
    for field, expected in required_policies.items():
        if contract[field] != expected:
            raise ValueError(f"unsupported {field}: expected {expected!r}")

    score_range = contract["score_range"]
    if not isinstance(score_range, Mapping):
        raise TypeError("score_range must be a mapping")
    if "minimum" not in score_range or "maximum" not in score_range:
        raise ValueError("score_range must define minimum and maximum")
    minimum = _finite_number(score_range["minimum"], "score_range.minimum")
    maximum = _finite_number(score_range["maximum"], "score_range.maximum")
    if minimum >= maximum:
        raise ValueError("score_range.minimum must be less than score_range.maximum")

    components = contract["components"]
    if (
        not isinstance(components, Sequence)
        or isinstance(components, (str, bytes, bytearray))
        or not components
    ):
        raise ValueError("components must be a non-empty sequence")

    validated_components: list[tuple[str, float]] = []
    seen_ids: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            raise TypeError(f"components[{index}] must be a mapping")
        if "id" not in component or "weight" not in component:
            raise ValueError(f"components[{index}] must define id and weight")

        component_id = component["id"]
        if not isinstance(component_id, str) or not component_id:
            raise ValueError(f"components[{index}].id must be a non-empty string")
        if component_id in seen_ids:
            raise ValueError(f"duplicate component id: {component_id!r}")

        weight = _finite_number(component["weight"], f"components[{index}].weight")
        if weight < 0.0:
            raise ValueError(f"weight for {component_id!r} must be non-negative")

        seen_ids.add(component_id)
        validated_components.append((component_id, weight))

    weight_total = math.fsum(weight for _, weight in validated_components)
    if not math.isclose(weight_total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("component weights must sum to 1")

    return validated_components, minimum, maximum


def calculate(scores: Any, contract: Any) -> float:
    """Validate ``scores`` against ``contract`` and return its weighted score.

    Missing component scores, undeclared component scores, non-finite values,
    and values outside the inclusive contract range are rejected rather than
    ignored, imputed, normalized, or clamped.
    """
    components, minimum, maximum = _validated_contract(contract)
    if not isinstance(scores, Mapping):
        raise TypeError("scores must be a mapping")

    declared_ids = {component_id for component_id, _ in components}
    supplied_ids = set(scores)
    missing_ids = declared_ids - supplied_ids
    unknown_ids = supplied_ids - declared_ids
    if missing_ids:
        names = ", ".join(sorted(repr(value) for value in missing_ids))
        raise ValueError(f"missing component score(s): {names}")
    if unknown_ids:
        names = ", ".join(sorted(repr(value) for value in unknown_ids))
        raise ValueError(f"unknown component score(s): {names}")

    weighted_terms: list[float] = []
    for component_id, weight in components:
        score = _finite_number(scores[component_id], f"score for {component_id!r}")
        if score < minimum or score > maximum:
            raise ValueError(
                f"score for {component_id!r} must be between {minimum} and {maximum}, inclusive"
            )
        weighted_terms.append(weight * score)

    result = math.fsum(weighted_terms)
    if not math.isfinite(result):
        raise ValueError("calculated score is not finite")
    return result
