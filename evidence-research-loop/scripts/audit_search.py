#!/usr/bin/env python3
"""Independently verify a completed evolution search from its raw files."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any


EXCLUDED_MANIFEST = "evidence-manifest.json"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def same_number(left: Any, right: Any) -> bool:
    return isinstance(left, (int, float)) and isinstance(right, (int, float)) and math.isclose(
        float(left), float(right), rel_tol=1e-12, abs_tol=1e-9)


def verify_events(project: Path) -> tuple[list[dict[str, Any]], list[str]]:
    errors = []
    events = [json.loads(line) for line in (project / "search-events.jsonl").read_text(
        encoding="utf-8").splitlines() if line]
    previous = None
    for sequence, event in enumerate(events, 1):
        stored = event.get("event_hash")
        unsigned = dict(event)
        unsigned.pop("event_hash", None)
        if event.get("sequence") != sequence:
            errors.append(f"event sequence mismatch at {sequence}")
        if event.get("previous_hash") != previous:
            errors.append(f"event previous_hash mismatch at {sequence}")
        if stored != sha256_bytes(canonical(unsigned)):
            errors.append(f"event hash mismatch at {sequence}")
        previous = stored
    return events, errors


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def recalculate_raw(result_dir: Path, metrics: dict[str, Any]) -> list[str]:
    errors = []
    raw_path = result_dir / "raw-results.json"
    if not raw_path.is_file():
        return [f"missing raw-results.json in {result_dir.name}"]
    rows = load(raw_path)
    if "median_elapsed_ns" in metrics:
        durations = [float(row["actual_elapsed_ns"]) for row in rows]
        expected = {
            "median_elapsed_ns": float(statistics.median(durations)),
            "p95_elapsed_ns": float(percentile(durations, 0.95)),
            "correct_case_count": sum(bool(row["correct"]) for row in rows),
            "case_count": len(rows),
        }
    elif "mean_total_cost" in metrics:
        expected = {
            "minimum_service_level": min(float(row["service_level"]) for row in rows),
            "mean_total_cost": sum(float(row["total_cost"]) for row in rows) / len(rows),
            "mean_committed_capacity": sum(float(row["committed_capacity"]) for row in rows) / len(rows),
            "scenario_count": len(rows),
        }
    else:
        return []
    for key, value in expected.items():
        actual = metrics.get(key)
        if isinstance(value, float):
            if not same_number(actual, value):
                errors.append(f"{result_dir.name} raw recomputation mismatch for {key}: {actual} != {value}")
        elif actual != value:
            errors.append(f"{result_dir.name} raw recomputation mismatch for {key}: {actual} != {value}")
    return errors


def verify_runs(project: Path, archive: dict[str, Any],
                events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    errors = []
    rows = []
    terminal_attempts = []
    candidates = {row["id"]: row for row in archive["candidates"]}
    access_path = project / "holdout-access.jsonl"
    accesses = []
    if access_path.is_file():
        accesses = [json.loads(line) for line in access_path.read_text(encoding="utf-8").splitlines() if line]
    for run_file in sorted((project / "runs-v2").glob("*/run.json")):
        result_dir = run_file.parent
        run = load(run_file)
        candidate = candidates.get(run.get("candidate_id"))
        if candidate is None or candidate.get("artifact_hash") != run.get("artifact_hash"):
            errors.append(f"candidate hash reference mismatch: {result_dir.name}")
        evaluation = run.get("evaluation")
        if not evaluation:
            terminal = run.get("attempt_terminal")
            if (run.get("split") == "holdout" and isinstance(terminal, dict)
                    and terminal.get("status") == "interrupted"):
                run_id = run.get("run_id")
                if not any(row["type"] == "holdout_attempt_interrupted" and row.get("run_id") == run_id
                           for row in events):
                    errors.append(f"interrupted attempt lacks controller event: {result_dir.name}")
                if not any(row.get("event") == "interrupted" and row.get("run_id") == run_id
                           for row in accesses):
                    errors.append(f"interrupted attempt lacks access event: {result_dir.name}")
                terminal_attempts.append({
                    "run_id": run_id, "candidate_id": run.get("candidate_id"),
                    "split": run.get("split"), "repeat_index": run.get("repeat_index"),
                    "freeze_id": run.get("freeze_id"), "status": terminal.get("status"),
                    "reason": terminal.get("reason"),
                    "retry_key": terminal.get("retry_key"),
                    "retried_by_run_id": terminal.get("retried_by_run_id"),
                })
                continue
            errors.append(f"unfinished run without terminal attempt record: {result_dir.name}")
            continue
        metrics_path = result_dir / "metrics.json"
        gate_path = result_dir / "correct.json"
        if metrics_path.is_file():
            metrics = load(metrics_path)
            if metrics != evaluation.get("metrics"):
                errors.append(f"metrics.json mismatch: {result_dir.name}")
            errors.extend(recalculate_raw(result_dir, metrics))
        else:
            metrics = {}
        if gate_path.is_file():
            gate = load(gate_path)
            if gate.get("correct") != evaluation.get("gate_actual"):
                errors.append(f"correct.json mismatch: {result_dir.name}")
        rows.append({"run_id": run["run_id"], "candidate_id": run["candidate_id"],
                     "split": run["split"], "repeat_index": run.get("repeat_index"),
                     "freeze_id": run.get("freeze_id"),
                     "hard_pass": evaluation["hard_pass"], "failure_class": evaluation["failure_class"],
                     "exit_code": evaluation["exit_code"], "timed_out": evaluation["timed_out"],
                     "duration_sec": evaluation["duration_sec"], "metrics": metrics})
    return rows, terminal_attempts, errors


def verify_candidate_hashes(project: Path, archive: dict[str, Any]) -> list[str]:
    errors = []
    for candidate in archive["candidates"]:
        path = project / candidate["artifact_path"]
        if not path.is_file() or sha256_file(path) != candidate["artifact_hash"]:
            errors.append(f"candidate artifact mismatch: {candidate['id']}")
    return errors


def verify_counters(state: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    errors = []
    expected = {
        "generator_calls_started": sum(row["type"] == "generation_started" for row in events),
        "generator_calls_completed": sum(row["type"] == "generation_call_finished" for row in events),
        "model_calls_started": sum(row["type"] == "generation_started" and row.get("counts_as_model_call")
                                   for row in events),
        "model_calls_completed": sum(row["type"] == "generation_call_finished" and row.get("model_call")
                                     for row in events),
    }
    for key, value in expected.items():
        if state.get(key) != value:
            errors.append(f"state counter mismatch {key}: {state.get(key)} != {value}")
    terminal = {"generation_failed", "generation_duplicate", "generation_evaluated",
                "generation_abandoned_unknown_outcome"}
    started_ids = {row["request_id"] for row in events if row["type"] == "generation_started"}
    terminal_ids = {row["request_id"] for row in events if row["type"] in terminal}
    if started_ids != terminal_ids:
        errors.append(f"nonterminal request IDs: {sorted(started_ids - terminal_ids)}")
    for request_id in started_ids:
        started_count = sum(row["type"] == "generation_started" and row.get("request_id") == request_id
                            for row in events)
        finished_count = sum(row["type"] == "generation_call_finished" and row.get("request_id") == request_id
                             for row in events)
        if started_count != 1:
            errors.append(f"request {request_id} has {started_count} generation_started events")
        if finished_count > 1:
            errors.append(f"request {request_id} has {finished_count} generation_call_finished events")
    return errors


def verify_holdout(project: Path, state: dict[str, Any], runs: list[dict[str, Any]],
                   terminal_attempts: list[dict[str, Any]],
                   events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    freeze_id = state.get("freeze_id")
    if state.get("status") == "no_eligible_candidate":
        if freeze_id is not None:
            errors.append("no_eligible_candidate search unexpectedly has a freeze")
        if state.get("selected_candidate_id") is not None:
            errors.append("no_eligible_candidate search unexpectedly selected a candidate")
        holdout_runs = [row for row in runs if row["split"] == "holdout"]
        if holdout_runs:
            errors.append("no_eligible_candidate search unexpectedly ran holdout")
        terminal_events = [row for row in events if row["type"] == "search_no_eligible_candidate"]
        if len(terminal_events) != 1:
            errors.append(
                f"search_no_eligible_candidate event count: {len(terminal_events)}")
        summary = {
            "freeze_id": None,
            "expected_count": 0,
            "completed_count": 0,
            "missing": [],
            "all_hard_pass": False,
            "complete": False,
            "not_run_reason": "no candidate passed the development hard gate",
        }
        return summary, errors
    if not freeze_id:
        summary = {"freeze_id": None, "expected_count": 0, "completed_count": 0,
                   "missing": [{"stage": "freeze"}], "all_hard_pass": False, "complete": False}
        return summary, ["search has no frozen comparison set"]
    freeze_history = load(project / "freezes.json").get("freezes", [])
    freeze = next((row for row in freeze_history if row.get("freeze_id") == freeze_id), None)
    if freeze is None:
        summary = {"freeze_id": freeze_id, "expected_count": 0, "completed_count": 0,
                   "missing": [{"stage": "freeze_record"}], "all_hard_pass": False, "complete": False}
        return summary, [f"state freeze record is missing: {freeze_id}"]
    task = load(project / "task.json")
    repeats = int(task["selection"].get("holdout_repeats", 1))
    expected = [(candidate_id, repeat) for repeat in range(1, repeats + 1)
                for candidate_id in freeze["comparison_candidate_ids"]]
    holdout_runs = [row for row in runs if row["split"] == "holdout" and row.get("freeze_id") == freeze_id]
    completed = []
    missing = []
    for candidate_id, repeat in expected:
        matches = [row for row in holdout_runs
                   if row["candidate_id"] == candidate_id and row.get("repeat_index") == repeat]
        if not matches:
            missing.append({"candidate_id": candidate_id, "repeat_index": repeat})
            continue
        if len(matches) != 1:
            errors.append(f"duplicate terminal holdout runs for {candidate_id} repeat {repeat}: {len(matches)}")
        completed.append(matches[-1])
        finished_events = [row for row in events if row["type"] == "holdout_finished"
                           and row.get("freeze_id") == freeze_id
                           and row.get("candidate_id") == candidate_id
                           and row.get("repeat_index") == repeat]
        if len(finished_events) != 1:
            errors.append(f"holdout_finished event count for {candidate_id} repeat {repeat}: "
                          f"{len(finished_events)}")
        elif finished_events[0].get("run_id") not in {None, matches[-1]["run_id"]}:
            errors.append(f"holdout event/run mismatch for {candidate_id} repeat {repeat}")
    unexpected = [row["run_id"] for row in holdout_runs
                  if (row["candidate_id"], row.get("repeat_index")) not in set(expected)]
    if unexpected:
        errors.append(f"unexpected holdout runs for current freeze: {unexpected}")
    complete = not missing and len(completed) == len(expected)
    all_hard_pass = bool(completed) and all(bool(row["hard_pass"]) for row in completed)
    completed_run_ids = {row["run_id"] for row in completed}
    for attempt in terminal_attempts:
        if attempt.get("freeze_id") != freeze_id:
            continue
        key = (attempt.get("candidate_id"), attempt.get("repeat_index"))
        if key not in set(expected):
            errors.append(f"interrupted attempt is outside current holdout matrix: {attempt['run_id']}")
            continue
        retry_run_id = attempt.get("retried_by_run_id")
        if retry_run_id is not None and retry_run_id not in completed_run_ids:
            errors.append(f"interrupted attempt links to non-completed retry: {attempt['run_id']}")
        if complete and retry_run_id is None:
            errors.append(f"completed matrix has unlinked interrupted attempt: {attempt['run_id']}")
        if retry_run_id is not None and not any(
                row["type"] == "holdout_retry_linked"
                and row.get("interrupted_run_id") == attempt["run_id"]
                and row.get("retry_run_id") == retry_run_id for row in events):
            errors.append(f"interrupted attempt lacks retry link event: {attempt['run_id']}")
    if missing:
        errors.append(f"missing terminal holdout runs: {missing}")
    terminal_status = state.get("status") in {"completed", "completed_with_holdout_failures"}
    if terminal_status and not complete:
        errors.append(f"terminal search status {state.get('status')} with incomplete holdout matrix")
    if state.get("status") == "completed" and complete and not all_hard_pass:
        errors.append("status completed but at least one holdout run failed the hard gate")
    if state.get("status") == "completed_with_holdout_failures" and complete and all_hard_pass:
        errors.append("status completed_with_holdout_failures but every holdout run hard-passed")
    return {"freeze_id": freeze_id, "expected_count": len(expected),
            "completed_count": len(completed), "missing": missing,
            "all_hard_pass": all_hard_pass, "complete": complete}, errors


def build_manifest(project: Path) -> dict[str, Any]:
    files = {}
    for path in sorted(item for item in project.rglob("*") if item.is_file()):
        relative = path.relative_to(project).as_posix()
        if relative == EXCLUDED_MANIFEST:
            continue
        files[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {"schema_version": 1, "algorithm": "sha256", "file_count": len(files), "files": files}


def verify_manifest(project: Path) -> list[str]:
    path = project / EXCLUDED_MANIFEST
    if not path.is_file():
        return ["evidence-manifest.json is missing"]
    expected = load(path)
    current = build_manifest(project)
    if expected != current:
        expected_files = expected.get("files", {})
        current_files = current["files"]
        changed = sorted(key for key in set(expected_files) | set(current_files)
                         if expected_files.get(key) != current_files.get(key))
        return [f"manifest mismatch: {changed}"]
    return []


def audit(project: Path, check_manifest: bool) -> dict[str, Any]:
    state = load(project / "search-state.json")
    archive = load(project / "archive.json")
    search_archive = load(project / "search-archive.json")
    events, errors = verify_events(project)
    errors.extend(verify_candidate_hashes(project, archive))
    runs, terminal_attempts, run_errors = verify_runs(project, archive, events)
    errors.extend(run_errors)
    errors.extend(verify_counters(state, events))
    if state.get("status") == "no_eligible_candidate" and search_archive.get("eligible_ids"):
        errors.append("no_eligible_candidate search has eligible archive entries")
    holdout_summary, holdout_errors = verify_holdout(project, state, runs, terminal_attempts, events)
    errors.extend(holdout_errors)
    if check_manifest:
        errors.extend(verify_manifest(project))
    event_counts = {kind: sum(row["type"] == kind for row in events)
                    for kind in sorted({row["type"] for row in events})}
    model_usage = {"input_tokens": 0, "output_tokens": 0, "calls_with_usage": 0}
    for path in sorted((project / "generator-workspaces").glob("*/call-result.json")):
        result = load(path)
        usage = result.get("usage")
        if result.get("model_call") and usage:
            model_usage["calls_with_usage"] += 1
            model_usage["input_tokens"] += int(usage.get("input_tokens", 0))
            model_usage["output_tokens"] += int(usage.get("output_tokens", 0))
    holdout = [row for row in runs if row["split"] == "holdout"]
    return {
        "valid": not errors,
        "accepted": not errors and holdout_summary["complete"] and holdout_summary["all_hard_pass"]
                    and state["status"] == "completed",
        "errors": errors,
        "task": load(project / "task.json")["name"],
        "status": state["status"],
        "stop_reason": state.get("stop_reason"),
        "selected_candidate_id": state.get("selected_candidate_id"),
        "candidate_count": len(archive["candidates"]),
        "eligible_count": len(search_archive["eligible_ids"]),
        "failure_count": len(search_archive["failures"]),
        "event_count": len(events),
        "event_counts": event_counts,
        "run_count": len(runs),
        "terminal_attempt_count": len(terminal_attempts),
        "holdout_run_count": len(holdout),
        "holdout_hard_pass_count": sum(row["hard_pass"] for row in holdout),
        "holdout_completion": holdout_summary,
        "model_usage": model_usage,
        "terminal_attempts": terminal_attempts,
        "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--write-summary", action="store_true")
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--verify-manifest", action="store_true")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    summary = audit(project, check_manifest=args.verify_manifest)
    if args.write_summary:
        (project / "audit-summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    if args.write_manifest:
        manifest = build_manifest(project)
        (project / EXCLUDED_MANIFEST).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False))
    if not summary["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
