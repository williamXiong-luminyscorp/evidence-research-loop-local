#!/usr/bin/env python3
"""Evidence Research Loop: versioned, tamper-evident solution evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
SHARED_VOLUME_PERMISSIONS = os.getenv("ERL_SHARED_VOLUME_PERMISSIONS", "").lower() in {"1", "true", "yes"}


def copy_file(source: Path, destination: Path) -> None:
    """Copy contents without metadata on filesystems that reject copystat."""
    if SHARED_VOLUME_PERMISSIONS:
        shutil.copyfile(source, destination)
    else:
        shutil.copy2(source, destination)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
    ) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def windows_kill_job(proc: subprocess.Popen[str]) -> tuple[int | None, str | None]:
    if os.name != "nt":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                      ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, f"CreateJobObjectW failed: {ctypes.get_last_error()}"
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return None, f"SetInformationJobObject failed: {error}"
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(proc._handle)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return None, f"AssignProcessToJobObject failed: {error}"
        return int(job), None
    except Exception as exc:
        return None, repr(exc)


def close_windows_job(job: int | None, *, terminate: bool) -> str | None:
    if os.name != "nt" or job is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE(job)
        termination_error = None
        if terminate and not kernel32.TerminateJobObject(handle, 1):
            termination_error = f"TerminateJobObject failed: {ctypes.get_last_error()}"
        close_error = None if kernel32.CloseHandle(handle) else f"CloseHandle(job) failed: {ctypes.get_last_error()}"
        return termination_error or close_error
    except Exception as exc:
        return repr(exc)


def run_process_tree(command: list[str], cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    popen_args: dict[str, Any] = {
        "cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
    }
    if os.name == "nt":
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        popen_args["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **popen_args)
    except OSError as exc:
        return {"return_code": None, "stdout": "", "stderr": str(exc), "timed_out": False,
                "invocation_error": str(exc), "duration_sec": time.perf_counter() - started,
                "process_tree_terminated": False, "process_id": None}
    job, job_error = windows_kill_job(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        close_error = close_windows_job(job, terminate=False)
        return {"return_code": proc.returncode, "stdout": stdout or "", "stderr": stderr or "",
                "timed_out": False, "invocation_error": None,
                "duration_sec": time.perf_counter() - started,
                "process_tree_terminated": False, "process_id": proc.pid,
                "process_tree_setup_error": job_error or close_error}
    except subprocess.TimeoutExpired:
        kill_error = close_windows_job(job, terminate=True) if os.name == "nt" and job is not None else None
        try:
            if os.name == "nt":
                if job is None:
                    killed = subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                        text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                    if killed.returncode not in {0, 128} and proc.poll() is None:
                        kill_error = (killed.stderr or killed.stdout or f"taskkill rc={killed.returncode}").strip()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError) as exc:
            kill_error = repr(exc)
        if proc.poll() is None:
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            kill_error = kill_error or "process tree did not close its pipes after termination"
        stderr_text = stderr or "timeout"
        if kill_error:
            stderr_text += f"\nprocess-tree termination warning: {kill_error}"
        return {"return_code": 124, "stdout": stdout or "", "stderr": stderr_text,
                "timed_out": True, "invocation_error": None,
                "duration_sec": time.perf_counter() - started,
                "process_tree_terminated": kill_error is None,
                "process_tree_termination_error": kill_error, "process_id": proc.pid}


def project_paths(root: Path) -> dict[str, Path]:
    return {
        "contract": root / "contract.json",
        "archive": root / "archive.json",
        "artifacts": root / "artifacts",
        "runs": root / "runs-v2",
        "locks": root / "protocol-locks",
        "lock_current": root / "protocol-current.json",
        "freezes": root / "freezes.json",
        "freeze_current": root / "freeze-current.json",
        "holdout_log": root / "holdout-access.jsonl",
    }


def require_project(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    paths = project_paths(root)
    if not paths["contract"].is_file() or not paths["archive"].is_file():
        raise SystemExit(f"Not an initialized evolution project: {root}")
    contract = load_json(paths["contract"])
    archive = load_json(paths["archive"])
    if contract.get("schema_version") != SCHEMA_VERSION or archive.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("Project schema is not version 2; create a new project and preserve the old evidence")
    return contract, archive, paths


def find_candidate(archive: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for candidate in archive["candidates"]:
        if candidate["id"] == candidate_id:
            return candidate
    raise SystemExit(f"Unknown candidate: {candidate_id}")


def nested_field(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def checked_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise SystemExit(f"Frozen input escapes project root: {relative}") from exc
    return path


def run_checked(command: list[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Environment check could not run {command!r}: {exc}") from exc
    if proc.returncode != 0:
        raise SystemExit(
            f"Environment check failed ({proc.returncode}) for {command!r}: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout.strip()


def verify_environment(root: Path, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actuals: list[dict[str, Any]] = []
    for check in checks:
        kind = check.get("type")
        if kind == "docker_image":
            reference = str(check["reference"])
            inspected = json.loads(run_checked(["docker", "image", "inspect", reference]))
            if not isinstance(inspected, list) or len(inspected) != 1:
                raise SystemExit(f"Expected exactly one Docker image for {reference}")
            actual_id = inspected[0].get("Id")
            expected_id = check.get("expected_id")
            if not expected_id or actual_id != expected_id:
                raise SystemExit(f"Docker image mismatch for {reference}: {actual_id} != {expected_id}")
            repo_digests = sorted(inspected[0].get("RepoDigests") or [])
            expected_digest = check.get("expected_repo_digest")
            if expected_digest and expected_digest not in repo_digests:
                raise SystemExit(f"Docker repo digest missing for {reference}: {expected_digest}")
            actuals.append({"type": kind, "reference": reference, "id": actual_id, "repo_digests": repo_digests})
        elif kind == "git_repo":
            configured = Path(str(check["path"]))
            repo = configured.resolve() if configured.is_absolute() else checked_path(root, str(configured))
            actual_commit = run_checked(["git", "rev-parse", "HEAD"], cwd=repo)
            expected_commit = check.get("expected_commit")
            if not expected_commit or actual_commit != expected_commit:
                raise SystemExit(f"Git commit mismatch at {repo}: {actual_commit} != {expected_commit}")
            status = run_checked(["git", "status", "--porcelain"], cwd=repo)
            if bool(check.get("require_clean", True)) and status:
                raise SystemExit(f"Git repository is dirty: {repo}\n{status}")
            actuals.append({"type": kind, "path": str(repo), "commit": actual_commit, "clean": not bool(status)})
        elif kind == "command":
            command = [str(v) for v in check.get("command", [])]
            if not command or "expected_stdout" not in check:
                raise SystemExit("command check requires command and expected_stdout")
            stdout = run_checked(command, cwd=root)
            if stdout != str(check["expected_stdout"]):
                raise SystemExit(f"Command identity mismatch for {command!r}: {stdout!r}")
            actuals.append({"type": kind, "command": command, "stdout": stdout})
        else:
            raise SystemExit(f"Unknown environment check type: {kind!r}")
    return actuals


def frozen_hashes(root: Path, contract: dict[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in contract.get("frozen_input_files", []):
        path = checked_path(root, str(relative))
        if not path.is_file():
            raise SystemExit(f"Frozen input is missing or not a file: {relative}")
        hashes[str(relative)] = sha256_file(path)
    return hashes


def signed_payload(payload: dict[str, Any], field: str) -> dict[str, Any]:
    result = dict(payload)
    result[field] = sha256_json(result)
    return result


def verify_signed_payload(payload: dict[str, Any], field: str) -> bool:
    expected = payload.get(field)
    unsigned = dict(payload)
    unsigned.pop(field, None)
    return isinstance(expected, str) and expected == sha256_json(unsigned)


def load_current_lock(root: Path, contract: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    if not paths["lock_current"].is_file():
        raise SystemExit("Protocol is not locked; run the lock command before evaluation")
    pointer = load_json(paths["lock_current"])
    lock_path = paths["locks"] / f"{pointer['lock_id']}.json"
    if not lock_path.is_file():
        raise SystemExit(f"Protocol lock file is missing: {lock_path}")
    lock = load_json(lock_path)
    if not verify_signed_payload(lock, "lock_hash"):
        raise SystemExit("Protocol lock hash is invalid")
    if pointer.get("lock_hash") != lock["lock_hash"]:
        raise SystemExit("Current protocol pointer does not match the lock")
    actual_engine_hash = sha256_file(Path(__file__).resolve())
    if actual_engine_hash != lock.get("engine_hash"):
        raise SystemExit(f"Research engine hash mismatch: {actual_engine_hash} != {lock.get('engine_hash')}")
    current_contract_hash = sha256_json(contract)
    if current_contract_hash != lock["contract_hash"]:
        raise SystemExit(f"Contract hash mismatch: {current_contract_hash} != {lock['contract_hash']}")
    if frozen_hashes(root, contract) != lock["frozen_input_hashes"]:
        raise SystemExit("Frozen input hash mismatch")
    actual_environment = verify_environment(root, contract.get("environment_checks", []))
    if actual_environment != lock["environment"]:
        raise SystemExit("Environment identity differs from the locked identity")
    return lock


def candidate_artifact(root: Path, candidate: dict[str, Any]) -> Path:
    artifact = checked_path(root, candidate["artifact_path"])
    if not artifact.is_file():
        raise SystemExit(f"Candidate artifact is missing: {artifact}")
    actual = sha256_file(artifact)
    if actual != candidate["artifact_hash"]:
        raise SystemExit(f"Candidate artifact hash mismatch for {candidate['id']}: {actual} != {candidate['artifact_hash']}")
    return artifact


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    paths = project_paths(root)
    if paths["contract"].exists() or paths["archive"].exists():
        raise SystemExit(f"Project already initialized: {root}")
    paths["artifacts"].mkdir(parents=True, exist_ok=True)
    paths["runs"].mkdir(parents=True, exist_ok=True)
    paths["locks"].mkdir(parents=True, exist_ok=True)
    contract = {
        "schema_version": SCHEMA_VERSION,
        "name": args.name,
        "artifact": {"kind": args.artifact_kind, "stable_interface": "REPLACE_ME", "mutable_boundary": "REPLACE_ME"},
        "workload": "REPLACE_ME",
        "hard_gate": {"file": "correct.json", "field": "correct", "equals": True},
        "objectives": [{"metric": "combined_score", "direction": "maximize"}],
        "behavior_signature_metrics": [],
        "evaluator": {"command": [], "metrics_file": "metrics.json", "timeout_sec": 3600, "env": {}},
        "splits": {"dev": {"env": {}}, "holdout": {"env": {}}},
        "resource_envelope": "REPLACE_ME",
        "budget_and_stop_rule": "REPLACE_ME",
        "frozen_input_files": [],
        "environment_checks": [],
    }
    archive = {
        "schema_version": SCHEMA_VERSION, "project": args.name, "created_at": utc_now(), "updated_at": utc_now(),
        "candidates": [], "events": [{"at": utc_now(), "type": "project_initialized"}],
    }
    save_json(paths["contract"], contract)
    save_json(paths["archive"], archive)
    save_json(paths["freezes"], {"schema_version": SCHEMA_VERSION, "freezes": []})
    print(root)


def cmd_lock(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    contract, _, paths = require_project(root)
    lock_id = f"p-{len(list(paths['locks'].glob('p-*.json'))) + 1:04d}"
    record = signed_payload({
        "schema_version": SCHEMA_VERSION, "lock_id": lock_id, "locked_at": utc_now(), "reason": args.reason,
        "contract_hash": sha256_json(contract), "frozen_input_hashes": frozen_hashes(root, contract),
        "engine_hash": sha256_file(Path(__file__).resolve()),
        "environment": verify_environment(root, contract.get("environment_checks", [])),
    }, "lock_hash")
    save_json(paths["locks"] / f"{lock_id}.json", record)
    save_json(paths["lock_current"], {"lock_id": lock_id, "lock_hash": record["lock_hash"]})
    print(json.dumps(record, indent=2, ensure_ascii=False))


def cmd_register(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    _, archive, paths = require_project(root)
    source = Path(args.artifact).resolve()
    if not source.is_file():
        raise SystemExit(f"Artifact is not a file: {source}")
    digest = sha256_file(source)
    for existing in archive["candidates"]:
        if existing["artifact_hash"] == digest:
            print(json.dumps({"duplicate_of": existing["id"], "hash": digest}))
            return
    for parent in args.parent:
        find_candidate(archive, parent)
    candidate_id = f"c-{len(archive['candidates']) + 1:04d}-{digest[:8]}"
    stored = paths["artifacts"] / f"{candidate_id}{source.suffix or '.artifact'}"
    copy_file(source, stored)
    candidate = {
        "id": candidate_id, "parents": args.parent, "generation": args.generation, "role": args.role,
        "hypothesis": args.hypothesis, "mechanism": args.mechanism, "source_label": args.source_label,
        "source_type": args.source_type, "author_or_model": args.author_or_model, "model_calls": args.model_calls,
        "generation_cost_usd": args.generation_cost_usd, "generation_duration_sec": args.generation_duration_sec,
        "generation_notes": args.generation_notes, "artifact_path": stored.relative_to(root).as_posix(),
        "artifact_hash": digest, "registered_at": utc_now(), "behavior_signature": None, "evaluations": {},
        "archive_status": "pending", "decision_reason": None,
    }
    archive["candidates"].append(candidate)
    archive["events"].append({"at": utc_now(), "type": "candidate_registered", "candidate_id": candidate_id})
    archive["updated_at"] = utc_now()
    save_json(paths["archive"], archive)
    print(json.dumps(candidate, indent=2, ensure_ascii=False))


def format_command(parts: list[str], values: dict[str, str]) -> list[str]:
    return [sys.executable if part == "{python}" else part.format(**values) for part in parts]


def latest_evaluation(candidate: dict[str, Any], split: str) -> dict[str, Any] | None:
    values = candidate.get("evaluations", {}).get(split, [])
    if isinstance(values, dict):
        return values
    return values[-1] if values else None


def current_freeze(paths: dict[str, Path]) -> dict[str, Any]:
    if not paths["freeze_current"].is_file():
        raise SystemExit("Holdout is locked until a candidate set is frozen")
    pointer = load_json(paths["freeze_current"])
    for record in load_json(paths["freezes"]).get("freezes", []):
        if record["freeze_id"] == pointer.get("freeze_id"):
            if record.get("freeze_hash") != pointer.get("freeze_hash") or not verify_signed_payload(record, "freeze_hash"):
                raise SystemExit("Current freeze record hash mismatch")
            return record
    raise SystemExit("Current freeze record is missing")


def authorize_holdout(paths: dict[str, Path], candidate: dict[str, Any], lock: dict[str, Any]) -> dict[str, Any]:
    freeze = current_freeze(paths)
    if freeze["protocol_lock_id"] != lock["lock_id"] or freeze["protocol_lock_hash"] != lock["lock_hash"]:
        raise SystemExit("Current freeze belongs to a different protocol lock")
    if candidate["id"] not in freeze["candidate_hashes"]:
        raise SystemExit("Holdout may only evaluate candidates named in the frozen comparison set")
    if freeze["candidate_hashes"][candidate["id"]] != candidate["artifact_hash"]:
        raise SystemExit("Frozen candidate hash mismatch")
    return freeze


def safe_json(path: Path) -> tuple[Any, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        return load_json(path), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"invalid_json: {exc}"


def output_evaluation(
    root: Path, contract: dict[str, Any], result_dir: Path, exit_code: int | None, duration_sec: float,
    timed_out: bool, invocation_error: str | None, postflight_error: str | None,
) -> dict[str, Any]:
    gate_cfg = contract["hard_gate"]
    gate_payload, gate_error = safe_json(result_dir / gate_cfg["file"])
    metrics, metrics_error = safe_json(result_dir / contract["evaluator"]["metrics_file"])
    gate_actual = nested_field(gate_payload, gate_cfg["field"]) if isinstance(gate_payload, dict) else None
    failure_class: str | None = None
    if invocation_error:
        failure_class = "invocation_error"
    elif timed_out:
        failure_class = "timeout"
    elif exit_code != 0:
        failure_class = "nonzero_exit"
    elif gate_error or metrics_error:
        failure_class = "missing_or_invalid_output"
    elif gate_actual != gate_cfg.get("equals", True):
        failure_class = "hard_gate_failed"
    else:
        for objective in contract.get("objectives", []):
            value = nested_field(metrics, objective["metric"])
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                failure_class = "illegal_metric"
                break
        if failure_class is None:
            try:
                json.dumps(metrics, allow_nan=False)
            except (TypeError, ValueError):
                failure_class = "illegal_metric"
    if postflight_error:
        failure_class = "postflight_integrity_failure"
    signature = {
        key: nested_field(metrics, key) if isinstance(metrics, dict) else None
        for key in contract.get("behavior_signature_metrics", [])
    }
    recorded_metrics = metrics if isinstance(metrics, dict) and failure_class != "illegal_metric" else {}
    return {
        "hard_pass": failure_class is None, "failure_class": failure_class, "gate_actual": gate_actual,
        "gate_error": gate_error, "metrics_error": metrics_error, "metrics": recorded_metrics,
        "behavior_signature": signature, "exit_code": exit_code, "timed_out": timed_out,
        "duration_sec": duration_sec, "invocation_error": invocation_error, "postflight_error": postflight_error,
        "evidence_path": result_dir.relative_to(root).as_posix(),
    }


def cmd_run(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    contract, archive, paths = require_project(root)
    if args.split not in contract["splits"]:
        raise SystemExit(f"Unknown split: {args.split}")
    candidate = find_candidate(archive, args.candidate_id)
    lock = load_current_lock(root, contract, paths)
    artifact = candidate_artifact(root, candidate)
    freeze: dict[str, Any] | None = None
    if args.split == "holdout":
        freeze = authorize_holdout(paths, candidate, lock)
    template = contract["evaluator"].get("command", [])
    if not template:
        raise SystemExit("Set evaluator.command in contract.json before locking")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = f"run-{timestamp}-{uuid.uuid4().hex[:8]}"
    result_dir = paths["runs"] / run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    values = {"candidate": str(artifact), "candidate_id": candidate["id"], "project": str(root),
              "result_dir": str(result_dir), "split": args.split, "run_id": run_id}
    command = format_command(template, values)
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in contract["evaluator"].get("env", {}).items()})
    env.update({str(k): str(v) for k, v in contract["splits"][args.split].get("env", {}).items()})
    env["ERL_RUN_ID"] = run_id
    env["ERL_REPEAT_INDEX"] = "" if args.repeat_index is None else str(args.repeat_index)
    env["ERL_RUN_LABEL"] = args.run_label or ""
    run_meta = {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "run_label": args.run_label,
        "repeat_index": args.repeat_index, "candidate_id": candidate["id"], "artifact_hash": candidate["artifact_hash"],
        "split": args.split, "command": command, "contract_hash": lock["contract_hash"],
        "protocol_lock_id": lock["lock_id"], "protocol_lock_hash": lock["lock_hash"],
        "freeze_id": freeze["freeze_id"] if freeze else None, "started_at": utc_now(),
        "host": {"python": sys.version, "platform": platform.platform(), "cpu_count": os.cpu_count()},
    }
    save_json(result_dir / "run.json", run_meta)
    if args.dry_run:
        print(json.dumps(run_meta, indent=2, ensure_ascii=False))
        return
    if args.split == "holdout":
        append_jsonl(paths["holdout_log"], {
            "at": utc_now(), "event": "started", "run_id": run_id, "freeze_id": freeze["freeze_id"],
            "candidate_id": candidate["id"], "protocol_lock_id": lock["lock_id"],
        })
    evaluator_timeout = float(contract["evaluator"].get("timeout_sec", 3600))
    controller_remaining = os.environ.get("ERL_CONTROLLER_REMAINING_SEC")
    if controller_remaining is not None:
        evaluator_timeout = min(evaluator_timeout, max(0.001, float(controller_remaining)))
    process_result = run_process_tree(command, root, env, evaluator_timeout)
    exit_code = process_result["return_code"]
    stdout, stderr = process_result["stdout"], process_result["stderr"]
    timed_out = process_result["timed_out"]
    invocation_error = process_result["invocation_error"]
    duration = process_result["duration_sec"]
    (result_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (result_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    postflight_error: str | None = None
    try:
        load_current_lock(root, load_json(paths["contract"]), paths)
        candidate_artifact(root, candidate)
    except SystemExit as exc:
        postflight_error = str(exc)
    evaluation = output_evaluation(root, contract, result_dir, exit_code, duration, timed_out,
                                   invocation_error, postflight_error)
    evaluation.update({"run_id": run_id, "split": args.split, "recorded_at": utc_now(),
                       "protocol_lock_id": lock["lock_id"]})
    candidate.setdefault("evaluations", {}).setdefault(args.split, []).append(evaluation)
    if args.split != "holdout":
        candidate["behavior_signature"] = evaluation["behavior_signature"]
        candidate["archive_status"] = "eligible" if evaluation["hard_pass"] else "failed"
    archive["events"].append({"at": utc_now(), "type": "candidate_evaluated", "candidate_id": candidate["id"],
                              "split": args.split, "run_id": run_id, "hard_pass": evaluation["hard_pass"]})
    archive["updated_at"] = utc_now()
    save_json(paths["archive"], archive)
    run_meta.update({"finished_at": utc_now(), "evaluation": evaluation,
                     "process_tree_terminated": process_result["process_tree_terminated"],
                     "process_id": process_result["process_id"],
                     "process_tree_termination_error": process_result.get("process_tree_termination_error")})
    save_json(result_dir / "run.json", run_meta)
    if args.split == "holdout":
        append_jsonl(paths["holdout_log"], {
            "at": utc_now(), "event": "finished", "run_id": run_id, "freeze_id": freeze["freeze_id"],
            "candidate_id": candidate["id"], "hard_pass": evaluation["hard_pass"],
            "failure_class": evaluation["failure_class"], "exit_code": exit_code,
        })
    print(json.dumps(evaluation, indent=2, ensure_ascii=False))


def objective_value(contract: dict[str, Any], candidate: dict[str, Any], split: str) -> Any:
    evaluation = latest_evaluation(candidate, split)
    if not evaluation or not evaluation.get("hard_pass"):
        return None
    return nested_field(evaluation["metrics"], contract["objectives"][0]["metric"])


def cmd_rank(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    contract, archive, _ = require_project(root)
    reverse = contract["objectives"][0].get("direction", "maximize") == "maximize"
    rows = []
    for candidate in archive["candidates"]:
        value = objective_value(contract, candidate, args.split)
        if value is not None:
            rows.append((value, candidate["id"], candidate["role"], candidate["mechanism"]))
    rows.sort(key=lambda row: row[0], reverse=reverse)
    print(json.dumps([{"rank": i + 1, "candidate_id": row[1], "value": row[0], "role": row[2],
                       "mechanism": row[3]} for i, row in enumerate(rows)], indent=2, ensure_ascii=False))


def cmd_freeze(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    contract, archive, paths = require_project(root)
    lock = load_current_lock(root, contract, paths)
    ids = [args.candidate_id, *args.comparator]
    if len(set(ids)) != len(ids):
        raise SystemExit("Frozen comparison candidate IDs must be unique")
    hashes: dict[str, str] = {}
    selection_runs: dict[str, str] = {}
    for candidate_id in ids:
        candidate = find_candidate(archive, candidate_id)
        candidate_artifact(root, candidate)
        evaluations = candidate.get("evaluations", {}).get(args.split, [])
        if isinstance(evaluations, dict):
            evaluations = [evaluations]
        passing = [e for e in evaluations if e.get("hard_pass") and e.get("protocol_lock_id") == lock["lock_id"]]
        evaluation = passing[-1] if passing else None
        if not evaluation:
            raise SystemExit(f"Candidate {candidate_id} lacks a hard-pass {args.split} evaluation")
        hashes[candidate_id], selection_runs[candidate_id] = candidate["artifact_hash"], evaluation["run_id"]
    history = load_json(paths["freezes"])
    freeze_id = f"f-{len(history.get('freezes', [])) + 1:04d}"
    record = signed_payload({
        "schema_version": SCHEMA_VERSION, "freeze_id": freeze_id, "frozen_at": utc_now(),
        "selected_candidate_id": args.candidate_id, "comparison_candidate_ids": ids,
        "candidate_hashes": hashes, "selection_split": args.split, "selection_run_ids": selection_runs,
        "protocol_lock_id": lock["lock_id"], "protocol_lock_hash": lock["lock_hash"], "reason": args.reason,
    }, "freeze_hash")
    history.setdefault("freezes", []).append(record)
    save_json(paths["freezes"], history)
    save_json(paths["freeze_current"], {"freeze_id": freeze_id, "freeze_hash": record["freeze_hash"]})
    for candidate_id in ids:
        candidate = find_candidate(archive, candidate_id)
        candidate["archive_status"] = "frozen_selected" if candidate_id == args.candidate_id else "frozen_comparator"
        candidate["decision_reason"] = args.reason
    archive["events"].append({"at": utc_now(), "type": "candidate_set_frozen", "freeze_id": freeze_id,
                              "candidate_ids": ids})
    archive["updated_at"] = utc_now()
    save_json(paths["archive"], archive)
    print(json.dumps(record, indent=2, ensure_ascii=False))


def cmd_status(args: argparse.Namespace) -> None:
    root = Path(args.project).resolve()
    contract, archive, paths = require_project(root)
    accesses = []
    if paths["holdout_log"].is_file():
        accesses = [json.loads(line) for line in paths["holdout_log"].read_text(encoding="utf-8").splitlines() if line]
    print(json.dumps({
        "project": contract["name"], "contract_hash": sha256_json(contract),
        "protocol_current": load_json(paths["lock_current"]) if paths["lock_current"].is_file() else None,
        "freeze_current": load_json(paths["freeze_current"]) if paths["freeze_current"].is_file() else None,
        "freeze_count": len(load_json(paths["freezes"]).get("freezes", [])), "holdout_access_events": accesses,
        "candidate_count": len(archive["candidates"]),
        "candidates": [{"id": c["id"], "role": c["role"], "parents": c["parents"], "hash": c["artifact_hash"],
                        "status": c["archive_status"], "evaluation_counts": {
                            k: len(v) if isinstance(v, list) else 1 for k, v in c["evaluations"].items()
                        }} for c in archive["candidates"]],
    }, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive and evaluate evolving solutions")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("project"); init.add_argument("--name", required=True)
    init.add_argument("--artifact-kind", default="code"); init.set_defaults(func=cmd_init)
    lock = sub.add_parser("lock"); lock.add_argument("project"); lock.add_argument("--reason", required=True)
    lock.set_defaults(func=cmd_lock)
    register = sub.add_parser("register"); register.add_argument("project"); register.add_argument("artifact")
    register.add_argument("--parent", action="append", default=[]); register.add_argument("--generation", type=int, default=0)
    register.add_argument("--role", default="baseline"); register.add_argument("--hypothesis", required=True)
    register.add_argument("--mechanism", required=True); register.add_argument("--source-label", default="original")
    register.add_argument("--source-type", default="unknown"); register.add_argument("--author-or-model", default="unknown")
    register.add_argument("--model-calls", type=int); register.add_argument("--generation-cost-usd", type=float)
    register.add_argument("--generation-duration-sec", type=float); register.add_argument("--generation-notes")
    register.set_defaults(func=cmd_register)
    run = sub.add_parser("run"); run.add_argument("project"); run.add_argument("candidate_id")
    run.add_argument("--split", default="dev"); run.add_argument("--run-label"); run.add_argument("--repeat-index", type=int)
    run.add_argument("--dry-run", action="store_true"); run.set_defaults(func=cmd_run)
    rank = sub.add_parser("rank"); rank.add_argument("project"); rank.add_argument("--split", default="dev")
    rank.set_defaults(func=cmd_rank)
    freeze = sub.add_parser("freeze"); freeze.add_argument("project"); freeze.add_argument("candidate_id")
    freeze.add_argument("--comparator", action="append", default=[]); freeze.add_argument("--split", default="dev")
    freeze.add_argument("--reason", required=True); freeze.set_defaults(func=cmd_freeze)
    status = sub.add_parser("status"); status.add_argument("project"); status.set_defaults(func=cmd_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
