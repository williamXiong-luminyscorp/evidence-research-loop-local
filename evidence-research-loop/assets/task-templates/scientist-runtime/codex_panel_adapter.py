#!/usr/bin/env python3
"""Optional Codex multi-panel adapter for ERL Scientist Runtime projects.

This adapter is not enabled by the safe-failing template.  Copy and freeze
``panel-config.json`` plus any custom output schemas before selecting it in a
real ``research-plan.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


CONFIG_NAME = "panel-config.json"


def schema_for(stage: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"stage": {"const": stage}},
        "required": ["stage"],
        "additionalProperties": False,
    }
    properties = base["properties"]
    required = base["required"]
    if stage == "ideation":
        properties["ideas"] = {
            "type": "array",
            "minItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "idea_id": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "falsifiable_prediction": {"type": "string"},
                    "minimum_experiment": {"type": "string"},
                    "architecture_class": {"type": "string"},
                    "mechanism": {"type": "string"},
                    "failure_condition": {"type": "string"},
                },
                "required": ["idea_id", "hypothesis", "falsifiable_prediction", "minimum_experiment", "architecture_class", "mechanism", "failure_condition"],
                "additionalProperties": False,
            },
        }
        required.append("ideas")
    elif stage == "literature":
        properties["sources"] = {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "title": {"type": "string"},
                    "locator": {"type": "string"},
                    "publisher": {"type": "string"},
                    "evidence_type": {"type": "string"},
                    "supports": {"type": "array", "items": {"type": "string"}},
                    "refutes": {"type": "array", "items": {"type": "string"}},
                    "assessment": {"type": "string"},
                },
                "required": ["source_id", "title", "locator", "publisher", "evidence_type", "supports", "refutes", "assessment"],
                "additionalProperties": False,
            },
        }
        properties["novelty_assessment"] = {
            "type": "object",
            "properties": {
                "status": {"enum": ["supported_distinct", "not_novel", "unknown"]},
                "reason": {"type": "string"},
            },
            "required": ["status", "reason"],
            "additionalProperties": False,
        }
        required.extend(["sources", "novelty_assessment"])
    elif stage == "experiment":
        properties["experiments"] = {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "experiment_id": {"type": "string"},
                    "status": {"enum": ["passed", "failed", "blocked"]},
                    "metrics": {
                        "type": "object",
                        "properties": {
                            "criteria_count": {"type": "integer"},
                            "profiles_count": {"type": "integer"},
                            "preferred_option_consistency": {"type": "number"},
                        },
                        "required": ["criteria_count", "profiles_count", "preferred_option_consistency"],
                        "additionalProperties": False,
                    },
                    "evidence_files": {"type": "array", "items": {"type": "string"}},
                    "protocol_note": {"type": "string"},
                },
                "required": ["experiment_id", "status", "metrics", "evidence_files", "protocol_note"],
                "additionalProperties": False,
            },
        }
        properties["comparison_matrix"] = {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "description": {"type": "string"},
                            "default_weight": {"type": "number"},
                            "resilience_weight": {"type": "number"},
                        },
                        "required": ["criterion_id", "description", "default_weight", "resilience_weight"],
                        "additionalProperties": False,
                    },
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "option_id": {"type": "string"},
                            "hard_gate_status": {"enum": ["pass", "fail", "unknown"]},
                            "default_total": {"type": "number"},
                            "resilience_total": {"type": "number"},
                            "evidence_summary": {"type": "string"},
                            "unknowns": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["option_id", "hard_gate_status", "default_total", "resilience_total", "evidence_summary", "unknowns"],
                        "additionalProperties": False,
                    },
                },
                "preferred_direction": {"type": "string"},
                "sensitivity_summary": {"type": "string"},
                "score_scale": {"type": "string"},
            },
            "required": ["criteria", "options", "preferred_direction", "sensitivity_summary", "score_scale"],
            "additionalProperties": False,
        }
        properties["poc_protocol"] = {
            "type": "object",
            "properties": {
                "design": {"type": "string"},
                "baseline": {"type": "string"},
                "candidate": {"type": "string"},
                "historical_windows": {"type": "array", "items": {"type": "string"}},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "hard_gates": {"type": "array", "items": {"type": "string"}},
                "shadow_run": {"type": "string"},
                "stop_rule": {"type": "string"},
            },
            "required": ["design", "baseline", "candidate", "historical_windows", "metrics", "hard_gates", "shadow_run", "stop_rule"],
            "additionalProperties": False,
        }
        required.extend(["experiments", "comparison_matrix", "poc_protocol"])
    elif stage == "analysis":
        properties["claims"] = {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "claim_type": {"enum": ["empirical", "literature"]},
                    "statement": {"type": "string"},
                    "status": {"enum": ["supported", "refuted", "inconclusive"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["claim_id", "claim_type", "statement", "status", "evidence_ids"],
                "additionalProperties": False,
            },
        }
        required.append("claims")
    elif stage == "review":
        properties["decision"] = {"enum": ["accept", "revise", "reject"]}
        properties["claim_reviews"] = {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "decision": {"enum": ["accept", "revise", "reject"]},
                    "reason": {"type": "string"},
                },
                "required": ["claim_id", "decision", "reason"],
                "additionalProperties": False,
            },
        }
        properties["limitations"] = {"type": "array", "items": {"type": "string"}}
        required.extend(["decision", "claim_reviews", "limitations"])
    else:
        raise ValueError(f"unknown stage: {stage}")
    return base


def project_from_request(request_path: Path) -> Path:
    # <project>/scientist-runs/<stage>/<attempt>/request.json
    return request_path.resolve().parents[3]


def load_prior(project: Path, request: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in request.get("prior_stage_outputs", []):
        relative = Path(item["path"])
        path = (project / relative).resolve()
        if project.resolve() not in path.parents or not path.is_file():
            raise RuntimeError(f"unsafe or missing prior output: {relative}")
        rows.append({"stage": item["stage"], "sha256": item["sha256"], "output": json.loads(path.read_text(encoding="utf-8"))})
    return rows


def load_config(project: Path) -> dict[str, Any]:
    config_path = project / CONFIG_NAME
    if not config_path.is_file():
        raise RuntimeError(f"missing frozen {CONFIG_NAME}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("panel-config.model must be a non-empty string")
    limit = config.get("absolute_max_model_calls")
    if not isinstance(limit, int) or limit <= 0:
        raise RuntimeError("panel-config.absolute_max_model_calls must be positive")
    stages = config.get("stages")
    if not isinstance(stages, dict):
        raise RuntimeError("panel-config.stages must be an object")
    for stage in ("ideation", "literature", "experiment", "analysis", "review"):
        row = stages.get(stage)
        if not isinstance(row, dict):
            raise RuntimeError(f"panel-config.stages.{stage} must be an object")
        roles = row.get("roles")
        if not isinstance(roles, list) or not roles or not all(
                isinstance(role, str) and role.strip() for role in roles):
            raise RuntimeError(f"panel-config.stages.{stage}.roles must be non-empty strings")
    return config


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_frozen_inputs(project: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    plan = json.loads((project / "research-plan.json").read_text(encoding="utf-8"))
    max_bytes = int(config.get("max_frozen_input_bytes", 250_000))
    rows: list[dict[str, Any]] = []
    for relative_text in plan.get("frozen_input_files", []):
        relative = Path(relative_text)
        path = (project / relative).resolve()
        if relative.is_absolute() or project.resolve() not in path.parents or not path.is_file():
            raise RuntimeError(f"unsafe or missing frozen input: {relative_text}")
        payload = path.read_bytes()
        if len(payload) > max_bytes:
            raise RuntimeError(
                f"frozen input exceeds panel-config.max_frozen_input_bytes: {relative_text}"
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"panel adapter accepts UTF-8 frozen inputs only: {relative_text}") from exc
        rows.append({
            "path": relative.as_posix(),
            "sha256": sha256_bytes(payload),
            "content": text,
        })
    return rows


def prompt_for(project: Path, request: dict[str, Any], config: dict[str, Any]) -> str:
    frozen_inputs = load_frozen_inputs(project, config)
    prior = load_prior(project, request)
    stage = request["stage"]
    special = config["stages"][stage].get("instructions", "")
    return f"""You are one stage in an Evidence Research Loop Scientist Runtime. Produce only JSON matching the supplied output schema.

STAGE: {stage}
RESEARCH QUESTION: {request['research_question']}
CLAIM BOUNDARY: {request['claim_boundary']}
STAGE INSTRUCTIONS: {request.get('stage_instructions', '')}
REQUIRED CLAIM IDS: {json.dumps(request.get('required_claim_ids', []), ensure_ascii=False)}

STAGE-SPECIFIC RULE:
{special}

DECLARED FROZEN INPUTS (path, sha256, UTF-8 content):
{json.dumps(frozen_inputs, ensure_ascii=False, indent=2)}

PRIOR IMMUTABLE STAGE OUTPUTS:
{json.dumps(prior, ensure_ascii=False, indent=2)}

Evidence discipline:
- Distinguish direct observations, literature, implemented/validated empirical evidence,
  design inference, proposed methods, vendor claims and unknowns.
- Do not invent that a URL was opened, a product was tested, or an operational benefit was measured.
- A panel majority is not evidence. Preserve material disagreement and source limitations.
- Literature synthesis, an unimplemented blueprint and a proposed protocol are not empirical results.
- For scored designs, record criterion -> source IDs -> interface -> evidence class -> uncertainty.
- Keep the declared baseline as a real comparator.
- A recommendation is conditional on this protocol and must include falsification conditions.
- Output JSON only.
"""


def token_usage(stdout_text: str) -> tuple[int | None, int | None]:
    input_tokens: int | None = None
    output_tokens: int | None = None
    for line in stdout_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [event]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"input_tokens", "inputTokens"} and isinstance(child, int):
                        input_tokens = max(input_tokens or 0, child)
                    elif key in {"output_tokens", "outputTokens"} and isinstance(child, int):
                        output_tokens = max(output_tokens or 0, child)
                    else:
                        stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
    return input_tokens, output_tokens


def durable_model_calls(project: Path) -> int:
    """Return the largest durable count of calls already started or committed."""
    state_calls = 0
    state_path = project / "scientist-state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state_calls = int(state.get("model_calls", 0))
    started_calls = sum(
        1 for path in (project / "scientist-runs").glob("**/codex-*-prompt.txt")
        if path.is_file()
    )
    return max(state_calls, started_calls)


def run_codex_call(project: Path, attempt_dir: Path, schema_path: Path,
                   prompt: str, label: str, model: str) -> tuple[dict[str, Any], str, str]:
    model_output = attempt_dir / f"codex-{label}-response.json"
    # The prompt is the durable call-start record. Write it before launching so
    # an interruption or non-zero exit still consumes the absolute call budget.
    (attempt_dir / f"codex-{label}-prompt.txt").write_text(prompt, encoding="utf-8")
    command = [
        "codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "--skip-git-repo-check", "--json", "--color", "never", "-s", "read-only",
        "-C", str(project), "-m", model, "--output-schema", str(schema_path),
        "--output-last-message", str(model_output), "-",
    ]
    completed = subprocess.run(
        command, input=prompt, text=True, capture_output=True,
        encoding="utf-8", errors="replace",
    )
    (attempt_dir / f"codex-{label}-events.jsonl").write_text(completed.stdout, encoding="utf-8")
    (attempt_dir / f"codex-{label}-stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"Codex panel call {label} failed: {completed.stderr}")
    if not model_output.is_file():
        raise RuntimeError(f"Codex panel call {label} did not create a response")
    return json.loads(model_output.read_text(encoding="utf-8")), completed.stdout, completed.stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()

    request_path = Path(args.request)
    response_path = Path(args.response)
    attempt_dir = request_path.parent
    project = project_from_request(request_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    stage = request["stage"]
    config = load_config(project)
    model = config["model"]
    roles = config["stages"][stage]["roles"]

    used_calls = durable_model_calls(project)
    planned_calls = len(roles) + 1
    if used_calls + planned_calls > config["absolute_max_model_calls"]:
        print(
            f"panel call budget exceeded: used={used_calls}, planned={planned_calls}, "
            f"limit={config['absolute_max_model_calls']}", file=sys.stderr,
        )
        return 22

    schema_path = attempt_dir / "codex-output-schema.json"
    schema_relative = config["stages"][stage].get("schema_file")
    if schema_relative:
        source_schema = (project / schema_relative).resolve()
        if project.resolve() not in source_schema.parents or not source_schema.is_file():
            print(f"unsafe or missing schema_file: {schema_relative}", file=sys.stderr)
            return 23
        schema = json.loads(source_schema.read_text(encoding="utf-8"))
    else:
        schema = schema_for(stage)
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    base_prompt = prompt_for(project, request, config)
    panel_outputs: list[dict[str, Any]] = []
    event_streams: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    try:
        for index, role in enumerate(roles, start=1):
            label = f"panel-{index:02d}"
            role_prompt = (
                base_prompt
                + f"\nPANEL ROLE: {role}\n"
                + "Work independently from the other panelists. Produce a complete stage response, "
                  "but emphasize this role's expertise, contradictions, failure modes and evidence gaps.\n"
            )
            panel_output, events, _ = run_codex_call(
                project, attempt_dir, schema_path, role_prompt, label, model
            )
            panel_outputs.append({"role": role, "output": panel_output})
            event_streams.append(events)
            input_count, output_count = token_usage(events)
            total_input_tokens += input_count or 0
            total_output_tokens += output_count or 0

        synthesis_prompt = (
            base_prompt
            + "\nINDEPENDENT PANEL RESPONSES:\n"
            + json.dumps(panel_outputs, ensure_ascii=False, indent=2)
            + "\nSYNTHESIS DUTY:\n"
              "Reconcile the panel responses into one conservative canonical result. Preserve material "
              "disagreement and unknowns. Apply the frozen protocol literally. Do not decide by majority "
              "when evidence quality differs. Output only the stage JSON.\n"
        )
        output, events, _ = run_codex_call(
            project, attempt_dir, schema_path, synthesis_prompt, "synthesis", model
        )
        event_streams.append(events)
        input_count, output_count = token_usage(events)
        total_input_tokens += input_count or 0
        total_output_tokens += output_count or 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 21

    (attempt_dir / "codex-events.jsonl").write_text(
        "".join(event_streams), encoding="utf-8"
    )
    (attempt_dir / "codex-final-response.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if stage == "experiment":
        raw_name = "raw-panel-evidence.json"
        (attempt_dir / raw_name).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        for experiment in output.get("experiments", []):
            if experiment.get("status") == "passed":
                experiment["evidence_files"] = [raw_name]

    output["usage"] = {
        "model": model,
        "model_calls": planned_calls,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "known_cost_usd": None,
    }
    response_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
