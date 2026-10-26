#!/usr/bin/env python3
"""Execute an evidence-constrained Scientist Mode workflow.

Unlike the deterministic scientist-mode fixture, this runtime invokes configured
commands for ideation, literature review, experiments, analysis, and independent
review.  Every stage writes an immutable attempt directory.  Completion and
acceptance are derived from the recorded evidence rather than from a preset
score.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STAGES = ("ideation", "literature", "experiment", "analysis", "review")
TERMINAL_ATTEMPTS = {"completed", "failed", "abandoned_unknown_outcome"}


def _load_process_runner():
    """Reuse the controller's process-tree timeout implementation."""
    try:
        from evolution_controller import run_process  # type: ignore
        return run_process
    except ModuleNotFoundError:
        path = Path(__file__).with_name("evolution_controller.py")
        spec = importlib.util.spec_from_file_location("_erl_evolution_controller", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load process runner: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run_process


RUN_PROCESS = _load_process_runner()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def replace_with_retry(source: Path, destination: Path) -> None:
    """Work around short-lived Windows/OneDrive file locks."""
    for attempt in range(10):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    replace_with_retry(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    replace_with_retry(temporary, path)


def plan_path(project: Path) -> Path:
    return project / "research-plan.json"


def state_path(project: Path) -> Path:
    return project / "scientist-state.json"


def events_path(project: Path) -> Path:
    return project / "scientist-events.jsonl"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                     ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # Access denied means the process exists but cannot be queried.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and
                            exit_code.value == still_active)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Do not reclaim an uncertain lock and risk two concurrent writers.
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@contextlib.contextmanager
def project_lock(project: Path, timeout_sec: float = 15.0) -> Iterator[None]:
    """Serialize start/resume writers and reclaim locks from dead processes."""
    project.mkdir(parents=True, exist_ok=True)
    lock_path = project / ".scientist-runtime.lock"
    deadline = time.monotonic() + timeout_sec
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = canonical({"pid": os.getpid(), "created_at": utc_now()}) + b"\n"
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except FileExistsError:
            stale = False
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                stale = not _pid_is_alive(int(owner.get("pid", -1)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # A just-created file can briefly be empty.  Only reclaim a
                # malformed lock after the acquisition timeout has elapsed.
                stale = time.monotonic() >= deadline
            if stale:
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out acquiring scientist runtime lock: {lock_path}")
            time.sleep(0.02)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def load_events(project: Path) -> list[dict[str, Any]]:
    path = events_path(project)
    if not path.is_file():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    previous = None
    for sequence, event in enumerate(events, start=1):
        event_hash = event.get("event_hash")
        unsigned = dict(event)
        unsigned.pop("event_hash", None)
        if event.get("sequence") != sequence or event.get("previous_hash") != previous:
            raise RuntimeError("scientist event chain sequence/hash link mismatch")
        if event_hash != sha256_bytes(canonical(unsigned)):
            raise RuntimeError("scientist event hash mismatch")
        previous = event_hash
    return events


def append_event(project: Path, event_type: str, **fields: Any) -> dict[str, Any]:
    events = load_events(project)
    event = {
        "sequence": len(events) + 1,
        "at": utc_now(),
        "type": event_type,
        "previous_hash": events[-1]["event_hash"] if events else None,
        **fields,
    }
    event["event_hash"] = sha256_bytes(canonical(event))
    append_jsonl(events_path(project), event)
    return event


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def validate_plan(plan: dict[str, Any], project: Path | None = None) -> None:
    if plan.get("schema_version") != 1:
        raise ValueError("research-plan.schema_version must equal 1")
    _require_string(plan.get("plan_version"), "plan_version")
    _require_string(plan.get("research_question"), "research_question")
    _require_string(plan.get("claim_boundary"), "claim_boundary")
    budget = plan.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    if not isinstance(budget.get("max_stage_calls"), int) or budget["max_stage_calls"] < 1:
        raise ValueError("budget.max_stage_calls must be a positive integer")
    if not isinstance(budget.get("max_wall_time_sec"), (int, float)) or budget["max_wall_time_sec"] <= 0:
        raise ValueError("budget.max_wall_time_sec must be positive")
    cost = budget.get("max_known_cost_usd")
    if cost is not None and (not isinstance(cost, (int, float)) or cost < 0):
        raise ValueError("budget.max_known_cost_usd must be null or non-negative")
    stages = plan.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise ValueError(f"stages must contain exactly: {', '.join(STAGES)}")
    for name in STAGES:
        stage = stages[name]
        if not isinstance(stage, dict) or not isinstance(stage.get("enabled"), bool):
            raise ValueError(f"stages.{name}.enabled must be boolean")
        if not stage["enabled"]:
            continue
        backend = stage.get("backend")
        if not isinstance(backend, dict) or backend.get("kind") != "command":
            raise ValueError(f"stages.{name}.backend.kind must be command")
        command = backend.get("command")
        if not isinstance(command, list) or not command or not all(
                isinstance(item, str) and item for item in command):
            raise ValueError(f"stages.{name}.backend.command must be a non-empty string array")
        timeout = backend.get("timeout_sec", 900)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"stages.{name}.backend.timeout_sec must be positive")
        attempts = stage.get("max_attempts", 1)
        if not isinstance(attempts, int) or attempts < 1:
            raise ValueError(f"stages.{name}.max_attempts must be a positive integer")
        allowlist = backend.get("env_allowlist", [])
        if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
            raise ValueError(f"stages.{name}.backend.env_allowlist must be a string array")
    required = plan.get("acceptance", {}).get("required_claim_ids", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
        raise ValueError("acceptance.required_claim_ids must be a string array")
    if len(required) != len(set(required)):
        raise ValueError("acceptance.required_claim_ids contains duplicates")
    frozen = plan.get("frozen_input_files", [])
    if not isinstance(frozen, list) or not all(isinstance(item, str) and item for item in frozen):
        raise ValueError("frozen_input_files must be a string array")
    if project is not None:
        for item in frozen:
            relative = Path(_require_string(item, "frozen_input_files item"))
            if relative.is_absolute() or ".." in relative.parts or not (project / relative).is_file():
                raise ValueError(f"invalid or missing frozen input: {item}")


def plan_digest(project: Path) -> str:
    return sha256_file(plan_path(project))


def initial_state(plan: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_version": plan["plan_version"],
        "plan_hash": digest,
        "status": "not_started",
        "current_stage": STAGES[0],
        "stage_calls_started": 0,
        "stage_calls_completed": 0,
        "model_calls": 0,
        "known_cost_usd": 0.0,
        "unknown_cost_calls": 0,
        "accumulated_active_sec": 0.0,
        "stages": {
            name: {"status": "pending", "attempts": [], "output_path": None,
                   "output_hash": None, "last_error": None}
            for name in STAGES
        },
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def initialize(project: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    project = project.resolve()
    plan = read_json(plan_path(project))
    if not isinstance(plan, dict):
        raise ValueError("research-plan.json must contain an object")
    validate_plan(plan, project)
    digest = plan_digest(project)
    if state_path(project).is_file():
        state = read_json(state_path(project))
        if state.get("plan_hash") != digest or state.get("plan_version") != plan["plan_version"]:
            raise RuntimeError("research plan changed after execution started; create a new plan version")
        verify_identity(project, state)
        return plan, state
    protocol_dir = project / "protocol"
    protocol_dir.mkdir(parents=True, exist_ok=True)
    snapshot = protocol_dir / f"research-plan-{digest}.json"
    shutil.copyfile(plan_path(project), snapshot)
    inputs = []
    for relative in plan.get("frozen_input_files", []):
        path = project / relative
        inputs.append({"path": relative, "sha256": sha256_file(path)})
    lock = {
        "schema_version": 1,
        "plan_version": plan["plan_version"],
        "plan_hash": digest,
        "plan_snapshot": snapshot.relative_to(project).as_posix(),
        "plan_snapshot_hash": sha256_file(snapshot),
        "frozen_inputs": inputs,
        "created_at": utc_now(),
    }
    lock_path = project / "protocol-lock.json"
    write_json(lock_path, lock)
    lock_hash = sha256_file(lock_path)
    state = initial_state(plan, digest)
    state["protocol_lock_hash"] = lock_hash
    write_json(state_path(project), state)
    append_event(project, "scientist_initialized", plan_version=plan["plan_version"],
                 plan_hash=digest, protocol_lock_hash=lock_hash,
                 plan_snapshot=lock["plan_snapshot"],
                 plan_snapshot_hash=lock["plan_snapshot_hash"],
                 frozen_inputs=inputs)
    return plan, state


def verify_identity(project: Path, state: dict[str, Any]) -> None:
    if plan_digest(project) != state["plan_hash"]:
        raise RuntimeError("research plan hash changed")
    lock_path = project / "protocol-lock.json"
    if not lock_path.is_file():
        raise RuntimeError("protocol lock is missing")
    lock_hash = sha256_file(lock_path)
    if state.get("protocol_lock_hash") != lock_hash:
        raise RuntimeError("protocol lock hash changed")
    lock = read_json(lock_path)
    if lock.get("plan_hash") != state["plan_hash"] or \
            lock.get("plan_version") != state["plan_version"]:
        raise RuntimeError("protocol lock does not match research plan")
    snapshot_relative = Path(_require_string(lock.get("plan_snapshot"), "plan_snapshot"))
    snapshot = (project / snapshot_relative).resolve()
    protocol_root = (project / "protocol").resolve()
    if snapshot_relative.is_absolute() or protocol_root not in snapshot.parents or not snapshot.is_file():
        raise RuntimeError("protocol plan snapshot is missing or unsafe")
    if sha256_file(snapshot) != state["plan_hash"] or \
            lock.get("plan_snapshot_hash") != state["plan_hash"]:
        raise RuntimeError("protocol plan snapshot hash changed")
    expected_paths = read_json(plan_path(project)).get("frozen_input_files", [])
    actual_paths = [item.get("path") for item in lock.get("frozen_inputs", [])]
    if actual_paths != expected_paths or len(actual_paths) != len(set(actual_paths)):
        raise RuntimeError("protocol frozen input inventory changed")
    for item in lock.get("frozen_inputs", []):
        path = project / item["path"]
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"frozen input changed: {item['path']}")
    events = load_events(project)
    initialized = [event for event in events if event.get("type") == "scientist_initialized"]
    if len(initialized) != 1 or events[0] is not initialized[0]:
        raise RuntimeError("scientist initialization event is missing or duplicated")
    anchor = initialized[0]
    for field, expected in (
            ("plan_hash", state["plan_hash"]),
            ("protocol_lock_hash", lock_hash),
            ("plan_snapshot", lock["plan_snapshot"]),
            ("plan_snapshot_hash", lock["plan_snapshot_hash"]),
            ("frozen_inputs", lock.get("frozen_inputs", []))):
        if anchor.get(field) != expected:
            raise RuntimeError(f"scientist initialization anchor mismatch: {field}")


def _safe_attempt_path(project: Path, value: str) -> Path:
    path = (project / value).resolve()
    attempts_root = (project / "scientist-runs").resolve()
    if attempts_root not in path.parents:
        raise RuntimeError(f"attempt path escapes scientist-runs: {value}")
    return path


def reconcile_attempts(project: Path, plan: dict[str, Any], state: dict[str, Any]) -> None:
    changed = False
    event_attempts = {event.get("attempt_id") for event in load_events(project)
                      if event["type"] == "stage_attempt_finished"}
    for stage_name in STAGES:
        stage_state = state["stages"][stage_name]
        for attempt in stage_state["attempts"]:
            if attempt["status"] != "running":
                continue
            attempt_dir = _safe_attempt_path(project, attempt["path"])
            result_file = attempt_dir / "attempt-result.json"
            if result_file.is_file():
                result = read_json(result_file)
                attempt.update({
                    "status": result["status"],
                    "finished_at": result["finished_at"],
                    "result_hash": sha256_file(result_file),
                })
                if attempt["id"] not in event_attempts:
                    append_event(project, "stage_attempt_finished", stage=stage_name,
                                 attempt_id=attempt["id"], status=result["status"],
                                 result_hash=attempt["result_hash"], recovered=True)
                normalized_file = attempt_dir / "normalized-response.json"
                if result["status"] == "completed" and normalized_file.is_file():
                    output = read_json(normalized_file)
                    output_hash = sha256_file(normalized_file)
                    stage_state.update({
                        "status": "completed",
                        "output_path": normalized_file.relative_to(project).as_posix(),
                        "output_hash": output_hash,
                        "last_error": None,
                    })
                    write_stage_ledgers(project, stage_name, output, attempt["id"], output_hash)
                changed = True
            else:
                attempt.update({"status": "abandoned_unknown_outcome", "finished_at": utc_now()})
                append_event(project, "stage_attempt_abandoned", stage=stage_name,
                             attempt_id=attempt["id"], reason="no durable attempt-result after interruption")
                changed = True
        terminal = [item for item in stage_state["attempts"] if item["status"] in TERMINAL_ATTEMPTS]
        if terminal and stage_state["status"] == "running":
            last = terminal[-1]
            stage_state["status"] = "completed" if last["status"] == "completed" else "failed"
    if changed:
        recompute_accounting(project, plan, state)
        state["updated_at"] = utc_now()
        write_json(state_path(project), state)


def previous_outputs(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name in STAGES:
        stage = state["stages"][name]
        if stage["status"] == "completed" and stage["output_path"]:
            rows.append({"stage": name, "path": stage["output_path"],
                         "sha256": stage["output_hash"]})
    return rows


def build_request(project: Path, plan: dict[str, Any], state: dict[str, Any],
                  stage: str, attempt_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "stage": stage,
        "plan_version": plan["plan_version"],
        "plan_hash": state["plan_hash"],
        "research_question": plan["research_question"],
        "claim_boundary": plan["claim_boundary"],
        "stage_instructions": plan["stages"][stage].get("instructions", ""),
        "required_claim_ids": plan.get("acceptance", {}).get("required_claim_ids", []),
        "prior_stage_outputs": previous_outputs(project, state),
        "output_contract": {
            "ideation": "ideas[] with idea_id, hypothesis, falsifiable_prediction, minimum_experiment",
            "literature": "sources[] and novelty_assessment; every source needs source_id/title/locator",
            "experiment": "experiments[] with experiment_id/status/metrics/evidence_files",
            "analysis": "claims[] with claim_id/statement/status/evidence_ids",
            "review": "decision and claim_reviews[]",
        }[stage],
        "created_at": utc_now(),
    }


def expand_command(command: list[str], values: dict[str, str]) -> list[str]:
    return [item.format_map(values) for item in command]


def _usage(output: dict[str, Any]) -> dict[str, Any]:
    usage = output.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    model_calls = usage.get("model_calls", 0)
    if not isinstance(model_calls, int) or model_calls < 0:
        raise ValueError("usage.model_calls must be a non-negative integer")
    known_cost = usage.get("known_cost_usd")
    if known_cost is not None and (not isinstance(known_cost, (int, float)) or known_cost < 0):
        raise ValueError("usage.known_cost_usd must be null or non-negative")
    return {
        "model": usage.get("model"),
        "model_calls": model_calls,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "known_cost_usd": known_cost,
    }


def recompute_accounting(project: Path, plan: dict[str, Any], state: dict[str, Any]) -> None:
    attempts = [attempt for stage in state["stages"].values() for attempt in stage["attempts"]]
    state["stage_calls_started"] = len(attempts)
    state["stage_calls_completed"] = sum(
        attempt["status"] in TERMINAL_ATTEMPTS for attempt in attempts)
    model_calls = 0
    known_cost = 0.0
    unknown_cost_calls = 0
    for stage_name, stage_state in state["stages"].items():
        may_use_model = plan["stages"][stage_name].get("backend", {}).get("may_use_model", True)
        for attempt in stage_state["attempts"]:
            attempt_dir = _safe_attempt_path(project, attempt["path"])
            response_file = attempt_dir / "normalized-response.json"
            raw_file = attempt_dir / "response.json"
            usage = None
            for candidate in (response_file, raw_file):
                if not candidate.is_file():
                    continue
                try:
                    usage = _usage(read_json(candidate))
                    break
                except (ValueError, OSError, json.JSONDecodeError):
                    continue
            if usage is None:
                if may_use_model and attempt["status"] in TERMINAL_ATTEMPTS:
                    unknown_cost_calls += 1
                continue
            model_calls += usage["model_calls"]
            if usage["known_cost_usd"] is None:
                unknown_cost_calls += usage["model_calls"]
            else:
                known_cost += float(usage["known_cost_usd"])
    state["model_calls"] = model_calls
    state["known_cost_usd"] = known_cost
    state["unknown_cost_calls"] = unknown_cost_calls


def protected_snapshot(project: Path, state: dict[str, Any]) -> dict[str, str]:
    paths: list[Path] = [plan_path(project), project / "protocol-lock.json",
                         state_path(project), events_path(project)]
    lock = read_json(project / "protocol-lock.json")
    paths.extend(project / item["path"] for item in lock.get("frozen_inputs", []))
    for stage in state["stages"].values():
        if stage.get("output_path"):
            paths.append(project / stage["output_path"])
    for name in ("ideas.jsonl", "literature-ledger.jsonl", "novelty-assessment.json",
                 "experiment-ledger.jsonl", "analysis-results.json",
                 "claim-evidence-matrix.json"):
        path = project / name
        if path.is_file():
            paths.append(path)
    return {path.relative_to(project).as_posix(): sha256_file(path)
            for path in paths if path.is_file()}


def verify_protected_snapshot(project: Path, snapshot: dict[str, str]) -> None:
    for relative, digest in snapshot.items():
        path = project / relative
        if not path.is_file() or sha256_file(path) != digest:
            raise RuntimeError(f"stage command modified protected evidence: {relative}")


def _unique_rows(rows: Any, key: str, label: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be an array")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{label} items must be objects")
        identity = _require_string(row.get(key), f"{label}.{key}")
        if identity in seen:
            raise ValueError(f"duplicate {label}.{key}: {identity}")
        seen.add(identity)
    return rows


def validate_and_normalize_output(project: Path, attempt_dir: Path, stage: str,
                                  output: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(output, dict):
        raise ValueError("stage response must be an object")
    if output.get("stage") != stage:
        raise ValueError(f"response stage must equal {stage}")
    _usage(output)
    normalized = json.loads(json.dumps(output))
    adjustments: list[str] = []
    if stage == "ideation":
        ideas = _unique_rows(normalized.get("ideas"), "idea_id", "ideas")
        if not ideas:
            raise ValueError("ideation must return at least one idea")
        for row in ideas:
            for field in ("hypothesis", "falsifiable_prediction", "minimum_experiment"):
                _require_string(row.get(field), f"ideas.{field}")
    elif stage == "literature":
        sources = _unique_rows(normalized.get("sources"), "source_id", "sources")
        for row in sources:
            _require_string(row.get("title"), "sources.title")
            _require_string(row.get("locator"), "sources.locator")
        assessment = normalized.get("novelty_assessment")
        if not isinstance(assessment, dict):
            raise ValueError("novelty_assessment must be an object")
        status = assessment.get("status")
        if status not in {"supported_distinct", "not_novel", "unknown"}:
            raise ValueError("invalid novelty_assessment.status")
        if not sources and status != "unknown":
            assessment["status"] = "unknown"
            assessment["reason"] = "No sources were recorded; novelty cannot be established."
            adjustments.append("novelty downgraded to unknown because the source ledger is empty")
    elif stage == "experiment":
        experiments = _unique_rows(normalized.get("experiments"), "experiment_id", "experiments")
        if not experiments:
            raise ValueError("experiment stage must return at least one experiment")
        for row in experiments:
            if row.get("status") not in {"passed", "failed", "blocked"}:
                raise ValueError("invalid experiments.status")
            if not isinstance(row.get("metrics", {}), dict):
                raise ValueError("experiments.metrics must be an object")
            evidence = row.get("evidence_files", [])
            if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
                raise ValueError("experiments.evidence_files must be a string array")
            hashes = []
            for relative_text in evidence:
                relative = Path(relative_text)
                path = (attempt_dir / relative).resolve()
                if relative.is_absolute() or attempt_dir.resolve() not in path.parents or not path.is_file():
                    raise ValueError(f"missing or unsafe experiment evidence: {relative_text}")
                hashes.append({"path": relative.as_posix(), "sha256": sha256_file(path)})
            row["evidence_artifacts"] = hashes
            row["evidence_valid"] = row["status"] == "passed" and bool(hashes)
            if row["status"] == "passed" and not hashes:
                adjustments.append(
                    f"experiment {row['experiment_id']} passed but has no raw evidence; it cannot support claims")
    elif stage == "analysis":
        claims = _unique_rows(normalized.get("claims"), "claim_id", "claims")
        if not claims:
            raise ValueError("analysis must return at least one claim")
        sources = {row["source_id"] for row in load_jsonl(project / "literature-ledger.jsonl")}
        experiment_rows = load_jsonl(project / "experiment-ledger.jsonl")
        valid_experiments = {row["experiment_id"] for row in experiment_rows
                             if row.get("evidence_valid")}
        for row in claims:
            _require_string(row.get("statement"), "claims.statement")
            if row.get("status") not in {"supported", "refuted", "inconclusive"}:
                raise ValueError("invalid claims.status")
            evidence_ids = row.get("evidence_ids", [])
            if not isinstance(evidence_ids, list) or not all(isinstance(item, str) for item in evidence_ids):
                raise ValueError("claims.evidence_ids must be a string array")
            claim_type = row.get("claim_type", "empirical")
            allowed = sources if claim_type == "literature" else valid_experiments
            valid_ids = [item for item in evidence_ids if item in allowed]
            row["validated_evidence_ids"] = valid_ids
            if row["status"] == "supported" and not valid_ids:
                row["status"] = "inconclusive"
                row["runtime_note"] = "Downgraded: no validated evidence supports this claim."
                adjustments.append(f"claim {row['claim_id']} downgraded to inconclusive")
    elif stage == "review":
        if normalized.get("decision") not in {"accept", "revise", "reject"}:
            raise ValueError("review.decision must be accept, revise, or reject")
        reviews = _unique_rows(normalized.get("claim_reviews", []), "claim_id", "claim_reviews")
        analysis_rows = read_json(project / "analysis-results.json").get("claims", [])
        analysis_ids = {row["claim_id"] for row in analysis_rows}
        for row in reviews:
            if row["claim_id"] not in analysis_ids:
                raise ValueError(f"review references unknown claim: {row['claim_id']}")
            if row.get("decision") not in {"accept", "revise", "reject"}:
                raise ValueError("claim_reviews.decision must be accept, revise, or reject")
            _require_string(row.get("reason"), "claim_reviews.reason")
    return normalized, adjustments


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def write_stage_ledgers(project: Path, stage: str, output: dict[str, Any],
                        attempt_id: str, response_hash: str) -> None:
    if stage == "ideation":
        write_jsonl(project / "ideas.jsonl", [
            {**row, "attempt_id": attempt_id, "response_hash": response_hash,
             "recorded_at": utc_now()} for row in output["ideas"]
        ])
    elif stage == "literature":
        write_jsonl(project / "literature-ledger.jsonl", [
            {
                **row, "attempt_id": attempt_id, "response_hash": response_hash,
                "recorded_at": utc_now(),
            } for row in output["sources"]
        ])
        write_json(project / "novelty-assessment.json", output["novelty_assessment"])
    elif stage == "experiment":
        write_jsonl(project / "experiment-ledger.jsonl", [
            {
                **row, "attempt_id": attempt_id, "response_hash": response_hash,
                "recorded_at": utc_now(),
            } for row in output["experiments"]
        ])
    elif stage == "analysis":
        write_json(project / "analysis-results.json", output)
        write_json(project / "claim-evidence-matrix.json", {
            "schema_version": 1,
            "claims": output["claims"],
            "generated_from_attempt": attempt_id,
            "response_hash": response_hash,
        })
    elif stage == "review":
        reviews = project / "reviews"
        reviews.mkdir(parents=True, exist_ok=True)
        write_json(reviews / f"{attempt_id}.json", output)


def budget_stop_reason(plan: dict[str, Any], state: dict[str, Any]) -> str | None:
    budget = plan["budget"]
    if state["stage_calls_started"] >= budget["max_stage_calls"]:
        return "stage_call_budget_exhausted"
    max_cost = budget.get("max_known_cost_usd")
    if max_cost is not None and state["known_cost_usd"] >= max_cost:
        return "known_cost_budget_exhausted"
    if state["accumulated_active_sec"] >= budget["max_wall_time_sec"]:
        return "wall_time_budget_exhausted"
    return None


def stage_attempt(project: Path, plan: dict[str, Any], state: dict[str, Any], stage: str,
                  deadline: float) -> tuple[bool, str | None]:
    config = plan["stages"][stage]
    stage_state = state["stages"][stage]
    prior_attempts = stage_state["attempts"]
    if len(prior_attempts) >= config.get("max_attempts", 1):
        return False, "stage attempt budget exhausted"
    attempt_number = len(prior_attempts) + 1
    attempt_id = f"{stage}-a{attempt_number:03d}-{uuid.uuid4().hex[:8]}"
    attempt_dir = project / "scientist-runs" / stage / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    request = build_request(project, plan, state, stage, attempt_id)
    request_file = attempt_dir / "request.json"
    response_file = attempt_dir / "response.json"
    write_json(request_file, request)
    values = {
        "project": str(project), "stage": stage, "attempt_id": attempt_id,
        "attempt_dir": str(attempt_dir), "request": str(request_file),
        "response": str(response_file),
    }
    command = expand_command(config["backend"]["command"], values)
    attempt_record = {
        "id": attempt_id,
        "path": attempt_dir.relative_to(project).as_posix(),
        "status": "running",
        "started_at": utc_now(),
        "retry_of": prior_attempts[-1]["id"] if prior_attempts else None,
        "command": command,
        "request_hash": sha256_file(request_file),
    }
    prior_attempts.append(attempt_record)
    stage_state["status"] = "running"
    state["status"] = "running"
    state["stage_calls_started"] += 1
    state["updated_at"] = utc_now()
    write_json(state_path(project), state)
    append_event(project, "stage_attempt_started", stage=stage, attempt_id=attempt_id,
                 request_hash=attempt_record["request_hash"], retry_of=attempt_record["retry_of"])
    protected = protected_snapshot(project, state)
    remaining = max(0.0, deadline - time.perf_counter())
    timeout = min(float(config["backend"].get("timeout_sec", 900)), remaining)
    allowed = config["backend"].get("env_allowlist", [])
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"} or key in allowed}
    # Keep the subprocess working directory at the project root.  On Windows an
    # immutable attempt path can easily exceed the legacy MAX_PATH limit even
    # when the project itself is valid, causing CreateProcess to fail before
    # the command starts (WinError 267).  Request, response, and evidence paths
    # are absolute and the attempt directory remains the evidence boundary.
    # The protected snapshot below rejects mutations to protocol/state/prior
    # evidence regardless of the subprocess working directory.
    result = RUN_PROCESS(command, project, timeout, env=env) if timeout > 0 else {
        "return_code": 124, "stdout": "", "stderr": "wall time exhausted before call",
        "timed_out": True, "duration_sec": 0.0, "command": command,
        "process_tree_terminated": True,
    }
    (attempt_dir / "stdout.txt").write_text(result.get("stdout", ""), encoding="utf-8")
    (attempt_dir / "stderr.txt").write_text(result.get("stderr", ""), encoding="utf-8")
    error: str | None = None
    normalized: dict[str, Any] | None = None
    adjustments: list[str] = []
    try:
        verify_protected_snapshot(project, protected)
    except RuntimeError as exc:
        error = str(exc)
    if error is None and result["return_code"] != 0:
        error = "stage command timed out" if result.get("timed_out") else \
            f"stage command exited {result['return_code']}"
    elif error is None and not response_file.is_file():
        error = "stage command did not create response.json"
    elif error is None:
        try:
            response = read_json(response_file)
            normalized, adjustments = validate_and_normalize_output(
                project, attempt_dir, stage, response)
            write_json(attempt_dir / "normalized-response.json", normalized)
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            error = f"invalid stage response: {exc}"
    finished_at = utc_now()
    result_record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "stage": stage,
        "status": "completed" if error is None else "failed",
        "started_at": attempt_record["started_at"],
        "finished_at": finished_at,
        "duration_sec": result.get("duration_sec", 0.0),
        "return_code": result.get("return_code"),
        "timed_out": result.get("timed_out", False),
        "process_tree_terminated": result.get("process_tree_terminated", False),
        "error": error,
        "adjustments": adjustments,
        "raw_response_hash": sha256_file(response_file) if response_file.is_file() else None,
        "normalized_response_hash": sha256_file(attempt_dir / "normalized-response.json")
        if normalized is not None else None,
    }
    write_json(attempt_dir / "attempt-result.json", result_record)
    attempt_record.update({
        "status": result_record["status"], "finished_at": finished_at,
        "result_hash": sha256_file(attempt_dir / "attempt-result.json"),
    })
    if normalized is not None:
        output_path = attempt_dir / "normalized-response.json"
        output_hash = sha256_file(output_path)
        write_stage_ledgers(project, stage, normalized, attempt_id, output_hash)
        stage_state.update({
            "status": "completed", "output_path": output_path.relative_to(project).as_posix(),
            "output_hash": output_hash, "last_error": None,
        })
    else:
        stage_state.update({"status": "failed", "last_error": error})
    recompute_accounting(project, plan, state)
    state["updated_at"] = utc_now()
    write_json(state_path(project), state)
    append_event(project, "stage_attempt_finished", stage=stage, attempt_id=attempt_id,
                 status=result_record["status"], error=error,
                 adjustments=adjustments, result_hash=attempt_record["result_hash"])
    return error is None, error


def derive_acceptance(project: Path, plan: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    analysis_path = state["stages"]["analysis"].get("output_path")
    analysis = read_json(project / analysis_path) if analysis_path else {}
    claims = analysis.get("claims", [])
    claim_by_id = {row["claim_id"]: row for row in claims}
    required = plan.get("acceptance", {}).get("required_claim_ids", [])
    required_status = {
        claim_id: claim_by_id.get(claim_id, {}).get("status", "missing") for claim_id in required
    }
    supported = [row["claim_id"] for row in claims if row.get("status") == "supported"]
    review_output = None
    review_path = state["stages"]["review"].get("output_path")
    if review_path:
        review_output = read_json(project / review_path)
    review_decision = review_output.get("decision") if review_output else None
    claim_review_by_id = {
        row.get("claim_id"): row.get("decision")
        for row in (review_output or {}).get("claim_reviews", [])
    }
    all_required = all(status == "supported" for status in required_status.values())
    claims_requiring_review = required or supported
    review_accepts_required = bool(claims_requiring_review) and all(
        claim_review_by_id.get(claim_id) == "accept" for claim_id in claims_requiring_review)
    accepted = bool(review_decision == "accept" and supported and all_required and
                    review_accepts_required)
    literature_path = state["stages"]["literature"].get("output_path")
    literature = read_json(project / literature_path) if literature_path else {}
    novelty = literature.get("novelty_assessment", {
        "status": "unknown", "reason": "literature stage did not provide an assessment"
    })
    return {
        "accepted": accepted,
        "review_decision": review_decision,
        "supported_claim_ids": supported,
        "required_claim_status": required_status,
        "required_claim_review_status": {
            claim_id: claim_review_by_id.get(claim_id, "missing")
            for claim_id in claims_requiring_review
        },
        "novelty_status": novelty.get("status", "unknown"),
        "novelty_claim_allowed": novelty.get("status") == "supported_distinct",
        "reason": "review accepted every required supported claim with validated evidence"
        if accepted else
        "review did not accept every required supported claim, no supported claim exists, or evidence is missing",
    }


def export_report(project: Path, plan: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    acceptance = derive_acceptance(project, plan, state)
    report = {
        "schema_version": 1,
        "plan_version": plan["plan_version"],
        "plan_hash": state["plan_hash"],
        "execution_status": state["status"],
        "research_question": plan["research_question"],
        "claim_boundary": plan["claim_boundary"],
        "stages": state["stages"],
        "usage": {
            "stage_calls_started": state["stage_calls_started"],
            "stage_calls_completed": state["stage_calls_completed"],
            "model_calls": state["model_calls"],
            "known_cost_usd": state["known_cost_usd"],
            "unknown_cost_calls": state["unknown_cost_calls"],
            "active_duration_sec": state["accumulated_active_sec"],
        },
        "acceptance": acceptance,
        "evidence": {
            "ideas": "ideas.jsonl", "literature": "literature-ledger.jsonl",
            "experiments": "experiment-ledger.jsonl", "analysis": "analysis-results.json",
            "claim_evidence_matrix": "claim-evidence-matrix.json", "reviews": "reviews/",
            "events": "scientist-events.jsonl", "runs": "scientist-runs/",
        },
        "limitations": [
            "Machine execution validates only the configured contracts and evidence.",
            "Novelty may be claimed only when novelty_status is supported_distinct.",
            "Completion does not imply acceptance; inspect acceptance.accepted.",
        ],
        "generated_at": utc_now(),
    }
    write_json(project / "final-scientist-report.json", report)
    return report


def _execute_locked(project: Path) -> dict[str, Any]:
    project = project.resolve()
    plan, state = initialize(project)
    verify_identity(project, state)
    reconcile_attempts(project, plan, state)
    state = read_json(state_path(project))
    if state["status"] == "completed":
        return export_report(project, plan, state)
    session_started = time.perf_counter()
    previous_elapsed = float(state.get("accumulated_active_sec", 0.0))
    remaining = max(0.0, float(plan["budget"]["max_wall_time_sec"]) - previous_elapsed)
    deadline = session_started + remaining

    def persist_elapsed() -> None:
        state["accumulated_active_sec"] = previous_elapsed + (time.perf_counter() - session_started)
        state["updated_at"] = utc_now()
        write_json(state_path(project), state)

    try:
        for stage in STAGES:
            state = read_json(state_path(project))
            persist_elapsed()
            stage_state = state["stages"][stage]
            if stage_state["status"] in {"completed", "skipped"}:
                continue
            state["current_stage"] = stage
            config = plan["stages"][stage]
            if not config["enabled"]:
                stage_state["status"] = "skipped"
                state["updated_at"] = utc_now()
                write_json(state_path(project), state)
                append_event(project, "stage_skipped", stage=stage)
                continue
            stop_reason = budget_stop_reason(plan, state)
            if stop_reason:
                state["status"] = "budget_exhausted"
                state["stop_reason"] = stop_reason
                write_json(state_path(project), state)
                append_event(project, "scientist_stopped", reason=stop_reason, stage=stage)
                return export_report(project, plan, state)
            ok, error = stage_attempt(project, plan, state, stage, deadline)
            state = read_json(state_path(project))
            persist_elapsed()
            if not ok:
                attempts = state["stages"][stage]["attempts"]
                if len(attempts) >= config.get("max_attempts", 1):
                    state["status"] = "blocked"
                else:
                    state["status"] = "paused_on_failure"
                state["last_error"] = error
                write_json(state_path(project), state)
                append_event(project, "scientist_paused", stage=stage, status=state["status"],
                             error=error)
                return export_report(project, plan, state)
        state = read_json(state_path(project))
        state["status"] = "completed"
        state["current_stage"] = None
        state["updated_at"] = utc_now()
        write_json(state_path(project), state)
        append_event(project, "scientist_completed")
        return export_report(project, plan, state)
    finally:
        state = read_json(state_path(project))
        state["accumulated_active_sec"] = previous_elapsed + (time.perf_counter() - session_started)
        state["updated_at"] = utc_now()
        write_json(state_path(project), state)


def execute(project: Path) -> dict[str, Any]:
    """Run or resume one Scientist project with a single project writer."""
    project = project.resolve()
    with project_lock(project):
        return _execute_locked(project)


def audit(project: Path) -> dict[str, Any]:
    project = project.resolve()
    errors: list[str] = []
    try:
        plan = read_json(plan_path(project))
        validate_plan(plan, project)
        state = read_json(state_path(project))
        verify_identity(project, state)
        events = load_events(project)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return {"valid": False, "accepted": False, "errors": [str(exc)]}
    started_events: dict[str, list[dict[str, Any]]] = {}
    finished_events: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        attempt_id = event.get("attempt_id")
        if event.get("type") == "stage_attempt_started" and attempt_id:
            started_events.setdefault(attempt_id, []).append(event)
        elif event.get("type") == "stage_attempt_finished" and attempt_id:
            finished_events.setdefault(attempt_id, []).append(event)

    seen_attempt_ids: set[str] = set()
    accepted_attempts: dict[str, dict[str, Any]] = {}
    for stage_name, stage_state in state["stages"].items():
        completed_attempts: list[dict[str, Any]] = []
        for attempt in stage_state["attempts"]:
            attempt_id = attempt.get("id")
            if not isinstance(attempt_id, str) or not attempt_id:
                errors.append(f"{stage_name} has an attempt without a valid id")
                continue
            if attempt_id in seen_attempt_ids:
                errors.append(f"duplicate attempt id in state: {attempt_id}")
            seen_attempt_ids.add(attempt_id)
            try:
                attempt_dir = _safe_attempt_path(project, attempt["path"])
            except (KeyError, RuntimeError) as exc:
                errors.append(f"unsafe attempt path {attempt_id}: {exc}")
                continue
            request_file = attempt_dir / "request.json"
            if not request_file.is_file() or attempt.get("request_hash") != sha256_file(request_file):
                errors.append(f"attempt request missing or hash mismatch: {attempt_id}")
            starts = started_events.get(attempt_id, [])
            if len(starts) != 1 or (starts and (
                    starts[0].get("stage") != stage_name or
                    starts[0].get("request_hash") != attempt.get("request_hash"))):
                errors.append(f"attempt start event mismatch: {attempt_id}")

            result_file = attempt_dir / "attempt-result.json"
            result: dict[str, Any] | None = None
            if attempt.get("status") in TERMINAL_ATTEMPTS and not result_file.is_file():
                # An unknown-outcome interruption intentionally has no result.
                if attempt.get("status") != "abandoned_unknown_outcome":
                    errors.append(f"attempt result missing: {attempt_id}")
            elif result_file.is_file():
                result_hash = sha256_file(result_file)
                if attempt.get("result_hash") != result_hash:
                    errors.append(f"attempt result hash mismatch: {attempt_id}")
                try:
                    result = read_json(result_file)
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"attempt result is invalid JSON {attempt_id}: {exc}")
                if result is not None:
                    if result.get("attempt_id") != attempt_id or result.get("stage") != stage_name or \
                            result.get("status") != attempt.get("status"):
                        errors.append(f"attempt result identity/status mismatch: {attempt_id}")
                    response_file = attempt_dir / "response.json"
                    raw_hash = sha256_file(response_file) if response_file.is_file() else None
                    if result.get("raw_response_hash") != raw_hash:
                        errors.append(f"raw response hash mismatch: {attempt_id}")
                    normalized_file = attempt_dir / "normalized-response.json"
                    normalized_hash = sha256_file(normalized_file) \
                        if normalized_file.is_file() else None
                    if result.get("normalized_response_hash") != normalized_hash:
                        errors.append(f"normalized response hash mismatch: {attempt_id}")
                    finishes = finished_events.get(attempt_id, [])
                    if len(finishes) != 1 or (finishes and (
                            finishes[0].get("stage") != stage_name or
                            finishes[0].get("status") != result.get("status") or
                            finishes[0].get("result_hash") != result_hash)):
                        errors.append(f"attempt finished event mismatch: {attempt_id}")
                    if result.get("status") == "completed":
                        completed_attempts.append({
                            "attempt": attempt, "dir": attempt_dir,
                            "normalized_path": normalized_file,
                            "normalized_hash": normalized_hash,
                        })
                        if normalized_file.is_file() and stage_name == "experiment":
                            try:
                                experiment_output = read_json(normalized_file)
                                for row in experiment_output.get("experiments", []):
                                    for artifact in row.get("evidence_artifacts", []):
                                        evidence_path = (attempt_dir / artifact["path"]).resolve()
                                        if attempt_dir.resolve() not in evidence_path.parents or \
                                                not evidence_path.is_file() or \
                                                sha256_file(evidence_path) != artifact.get("sha256"):
                                            errors.append(
                                                f"experiment evidence missing or hash mismatch: "
                                                f"{attempt_id}:{artifact.get('path')}")
                            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                                errors.append(f"invalid experiment evidence ledger {attempt_id}: {exc}")

        if stage_state["status"] == "completed":
            output_text = stage_state.get("output_path")
            output = project / output_text if isinstance(output_text, str) else None
            matching = [row for row in completed_attempts
                        if output is not None and row["normalized_path"].resolve() == output.resolve()
                        and row["normalized_hash"] == stage_state.get("output_hash")]
            if output is None or not output.is_file() or \
                    sha256_file(output) != stage_state.get("output_hash") or len(matching) != 1:
                errors.append(f"{stage_name} output is not bound to one completed attempt")
            elif matching:
                accepted_attempts[stage_name] = matching[0]
        elif completed_attempts:
            errors.append(f"{stage_name} has a completed attempt but stage is {stage_state['status']}")

    terminal_attempts = [attempt for stage in state["stages"].values()
                         for attempt in stage["attempts"]
                         if attempt.get("status") in TERMINAL_ATTEMPTS]
    all_attempts = [attempt for stage in state["stages"].values()
                    for attempt in stage["attempts"]]
    if state.get("stage_calls_started") != len(all_attempts) or \
            state.get("stage_calls_completed") != len(terminal_attempts):
        errors.append("stage call accounting does not match attempts")
    recalculated = json.loads(json.dumps(state))
    recompute_accounting(project, plan, recalculated)
    for field in ("model_calls", "known_cost_usd", "unknown_cost_calls"):
        if state.get(field) != recalculated.get(field):
            errors.append(f"{field} accounting does not match recorded attempt usage")
    if state["status"] == "completed" and any(
            row["status"] not in {"completed", "skipped"} for row in state["stages"].values()):
        errors.append("completed state has unfinished stages")
    completion_events = [event for event in events if event.get("type") == "scientist_completed"]
    if state["status"] == "completed" and len(completion_events) != 1:
        errors.append("completed state must have exactly one scientist_completed event")
    def strip_ledger_metadata(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items()
                if key not in {"attempt_id", "response_hash", "recorded_at"}}

    stage_ledger_pairs = (
        ("ideation", "ideas", "ideas.jsonl"),
        ("literature", "sources", "literature-ledger.jsonl"),
        ("experiment", "experiments", "experiment-ledger.jsonl"),
    )
    for stage_name, field, ledger_name in stage_ledger_pairs:
        output_path = state["stages"][stage_name].get("output_path")
        if not output_path:
            continue
        expected = read_json(project / output_path).get(field, [])
        ledger_rows = load_jsonl(project / ledger_name)
        actual = [strip_ledger_metadata(row) for row in ledger_rows]
        if actual != expected:
            errors.append(f"{ledger_name} does not match the accepted {stage_name} output")
        binding = accepted_attempts.get(stage_name)
        if binding and any(
                row.get("attempt_id") != binding["attempt"]["id"] or
                row.get("response_hash") != binding["normalized_hash"]
                for row in ledger_rows):
            errors.append(f"{ledger_name} provenance does not match the accepted attempt")
    analysis_path = state["stages"]["analysis"].get("output_path")
    matrix_path = project / "claim-evidence-matrix.json"
    if analysis_path:
        analysis_output = read_json(project / analysis_path)
        expected_claims = analysis_output.get("claims", [])
        analysis_copy = project / "analysis-results.json"
        if not analysis_copy.is_file() or read_json(analysis_copy) != analysis_output:
            errors.append("analysis-results.json does not match the accepted analysis output")
        matrix = read_json(matrix_path) if matrix_path.is_file() else {}
        binding = accepted_attempts.get("analysis")
        if matrix.get("claims") != expected_claims or not binding or \
                matrix.get("generated_from_attempt") != binding["attempt"]["id"] or \
                matrix.get("response_hash") != binding["normalized_hash"]:
            errors.append("claim-evidence-matrix.json does not match the accepted analysis output")
    literature_path = state["stages"]["literature"].get("output_path")
    if literature_path:
        expected_novelty = read_json(project / literature_path).get("novelty_assessment")
        novelty_path = project / "novelty-assessment.json"
        if not novelty_path.is_file() or read_json(novelty_path) != expected_novelty:
            errors.append("novelty-assessment.json does not match accepted literature output")
    review_path = state["stages"]["review"].get("output_path")
    binding = accepted_attempts.get("review")
    if review_path and binding:
        derived_review = project / "reviews" / f"{binding['attempt']['id']}.json"
        if not derived_review.is_file() or read_json(derived_review) != read_json(project / review_path):
            errors.append("review record does not match the accepted review output")
    acceptance = derive_acceptance(project, plan, state)
    result = {
        "valid": not errors,
        "accepted": not errors and state["status"] == "completed" and acceptance["accepted"],
        "execution_status": state["status"],
        "acceptance": acceptance,
        "errors": errors,
        "event_count": len(events),
    }
    write_json(project / "scientist-audit.json", result)
    return result


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "start", "resume", "status", "audit", "export"):
        child = sub.add_parser(name)
        child.add_argument("project")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    if args.command == "validate":
        plan = read_json(plan_path(project))
        validate_plan(plan, project)
        print(json.dumps({"valid": True, "plan_hash": plan_digest(project)}, indent=2))
    elif args.command in {"start", "resume"}:
        print(json.dumps(execute(project), indent=2, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(read_json(state_path(project)), indent=2, ensure_ascii=False))
    elif args.command == "audit":
        print(json.dumps(audit(project), indent=2, ensure_ascii=False))
    elif args.command == "export":
        plan, state = initialize(project)
        print(json.dumps(export_report(project, plan, state), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    cli()
