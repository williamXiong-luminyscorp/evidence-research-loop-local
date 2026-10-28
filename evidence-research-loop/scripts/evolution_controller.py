#!/usr/bin/env python3
"""Serializable, resumable multi-generation controller for evidence-research-loop.

The controller deliberately delegates all candidate execution and holdout access
to solution_evolver.py. Generator workspaces receive only the parent artifact,
declared public context, and development feedback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONTROLLER = Path(__file__).resolve()
ENGINE = HERE / "solution_evolver.py"
SKILL_ROOT = HERE.parent
SCHEMA_VERSION = 1
WALL_TIME_GUARD_SEC = 0.25
SHARED_VOLUME_PERMISSIONS = os.getenv("ERL_SHARED_VOLUME_PERMISSIONS", "").lower() in {"1", "true", "yes"}
DETERMINISTIC_TEST_ENV = "ERL_ALLOW_DETERMINISTIC_TEST_MODE"
TERMINAL_GENERATION_EVENTS = {
    "generation_failed",
    "generation_duplicate",
    "generation_evaluated",
    "generation_abandoned_unknown_outcome",
}


def copy_file(source: Path, destination: Path) -> None:
    """Copy contents without metadata on filesystems that reject copystat."""
    if SHARED_VOLUME_PERMISSIONS:
        shutil.copyfile(source, destination)
    else:
        shutil.copy2(source, destination)


def copy_tree(source: Path, destination: Path) -> None:
    if not SHARED_VOLUME_PERMISSIONS:
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.025 * (attempt + 1))


def safe_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError(f"path must be project-relative: {relative}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes project: {relative}")
    return resolved


def append_event(project: Path, event_type: str, **fields: Any) -> dict[str, Any]:
    path = project / "search-events.jsonl"
    previous = load_events(project)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(previous) + 1,
        "at": utc_now(),
        "type": event_type,
        "previous_hash": previous[-1]["event_hash"] if previous else None,
        **fields,
    }
    record["event_hash"] = sha256_bytes(canonical(record))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_events(project: Path) -> list[dict[str, Any]]:
    path = project / "search-events.jsonl"
    if not path.is_file():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    previous_hash = None
    for index, event in enumerate(events, start=1):
        stored_hash = event.get("event_hash")
        unsigned = dict(event)
        unsigned.pop("event_hash", None)
        if event.get("sequence") != index or event.get("previous_hash") != previous_hash:
            raise SystemExit("search event chain sequence/hash link mismatch")
        if stored_hash != sha256_bytes(canonical(unsigned)):
            raise SystemExit("search event hash mismatch")
        previous_hash = stored_hash
    return events


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
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
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


def run_process(command: list[str], cwd: Path, timeout: float,
                env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        popen_args: dict[str, Any] = {
            "cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            popen_args["start_new_session"] = True
        proc = subprocess.Popen(command, **popen_args)
        job, job_error = windows_kill_job(proc)
        stdout, stderr = proc.communicate(timeout=timeout)
        close_error = close_windows_job(job, terminate=False)
        return {"return_code": proc.returncode, "stdout": stdout or "", "stderr": stderr or "",
                "timed_out": False, "duration_sec": time.perf_counter() - started,
                "command": command, "process_tree_terminated": False, "process_id": proc.pid,
                "process_tree_setup_error": job_error or close_error}
    except subprocess.TimeoutExpired as exc:
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
        except (OSError, subprocess.SubprocessError) as kill_exc:
            kill_error = repr(kill_exc)
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
                "timed_out": True, "duration_sec": time.perf_counter() - started,
                "command": command, "process_tree_terminated": kill_error is None,
                "process_tree_termination_error": kill_error, "process_id": proc.pid}
    except OSError as exc:
        return {"return_code": 127, "stdout": "", "stderr": str(exc), "timed_out": False,
                "duration_sec": time.perf_counter() - started, "command": command,
                "process_tree_terminated": False, "process_id": None}


def engine_call(project: Path, *args: str, timeout: float | None = None) -> Any:
    if timeout is not None and timeout <= 0:
        raise TimeoutError("controller wall-time budget exhausted before engine call")
    env = os.environ.copy()
    if timeout is not None:
        env["ERL_CONTROLLER_REMAINING_SEC"] = str(timeout)
    result = run_process([sys.executable, str(ENGINE), *args], project,
                         timeout=min(7200.0, timeout) if timeout is not None else 7200.0,
                         env=env)
    if result["return_code"] != 0:
        if result["timed_out"]:
            raise TimeoutError("evaluation engine exceeded remaining controller wall-time budget")
        raise RuntimeError(result["stderr"] or result["stdout"])
    output = result["stdout"].strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def config_path(project: Path) -> Path:
    return project / "task.json"


def state_path(project: Path) -> Path:
    return project / "search-state.json"


def archive_path(project: Path) -> Path:
    return project / "search-archive.json"


def remaining_sec(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.perf_counter())


def operation_timeout(deadline: float | None) -> float | None:
    remaining = remaining_sec(deadline)
    if remaining is None:
        return None
    return max(0.0, remaining - WALL_TIME_GUARD_SEC)


def load_config(project: Path) -> dict[str, Any]:
    path = config_path(project)
    if not path.is_file():
        raise SystemExit(f"missing task adapter: {path}")
    config = load_json(path)
    validate_config(project, config)
    return config


def validate_config(project: Path, config: dict[str, Any]) -> None:
    required = ["name", "artifact", "contract", "public_context_files", "generator", "budget", "selection"]
    missing = [key for key in required if key not in config]
    if missing:
        raise SystemExit(f"task.json missing fields: {missing}")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(f"unsupported task schema: {config.get('schema_version')}")
    baseline = safe_path(project, config["artifact"]["baseline"])
    if not baseline.is_file():
        raise SystemExit(f"baseline artifact missing: {baseline}")
    filename = config["artifact"].get("candidate_filename")
    if not filename or Path(filename).name != filename:
        raise SystemExit("artifact.candidate_filename must be a simple filename")
    for item in config["public_context_files"]:
        if not safe_path(project, item).is_file():
            raise SystemExit(f"public context missing: {item}")
    generator = config["generator"]
    if generator.get("backend") not in {"deterministic", "command", "codex", "docker-command"}:
        raise SystemExit("generator.backend must be deterministic, command, codex, or docker-command")
    roles = generator.get("roles", [])
    if not roles:
        raise SystemExit("generator.roles cannot be empty")
    allowed_roles = {"conservative", "mechanism", "exploratory", "repair"}
    if any(role not in allowed_roles for role in roles):
        raise SystemExit(f"generator.roles must use {sorted(allowed_roles)}")
    if float(generator.get("timeout_sec", 900)) <= 0:
        raise SystemExit("generator.timeout_sec must be positive")
    budget = config["budget"]
    for key in ("max_generations", "candidates_per_generation", "max_generator_calls", "max_wall_time_sec"):
        if not isinstance(budget.get(key), int) or budget[key] <= 0:
            raise SystemExit(f"budget.{key} must be a positive integer")
    if budget["candidates_per_generation"] > len(roles):
        raise SystemExit("generator.roles must cover candidates_per_generation")
    if budget.get("max_known_cost_usd") is not None and float(budget["max_known_cost_usd"]) <= 0:
        raise SystemExit("budget.max_known_cost_usd must be positive when set")
    if budget.get("no_improvement_generations") is not None and int(budget["no_improvement_generations"]) <= 0:
        raise SystemExit("budget.no_improvement_generations must be positive when set")
    allow_failing_baseline = config["selection"].get("allow_failing_baseline", False)
    if type(allow_failing_baseline) is not bool:
        raise SystemExit("selection.allow_failing_baseline must be boolean when set")
    contract = config["contract"]
    if not contract.get("evaluator", {}).get("command"):
        raise SystemExit("contract.evaluator.command is required")
    if not contract.get("objectives"):
        raise SystemExit("contract.objectives cannot be empty")
    for frozen in contract.get("frozen_input_files", []):
        if not safe_path(project, frozen).is_file():
            raise SystemExit(f"frozen input missing: {frozen}")
    if config["selection"].get("independent_holdout_claim"):
        seed_file_name = contract.get("seed_ids_file")
        novelty_file_name = contract.get("seed_novelty_file")
        if not seed_file_name or not novelty_file_name:
            raise SystemExit("independent holdout requires contract.seed_ids_file and seed_novelty_file")
        if seed_file_name not in contract.get("frozen_input_files", []):
            raise SystemExit("independent holdout seed_ids_file must be a frozen input")
        if novelty_file_name not in contract.get("frozen_input_files", []):
            raise SystemExit("independent holdout seed_novelty_file must be a frozen input")
        seed_path = safe_path(project, seed_file_name)
        novelty = load_json(safe_path(project, novelty_file_name))
        seed_sets = load_json(seed_path)
        if not novelty.get("valid"):
            raise SystemExit("independent holdout seed novelty check is not valid")
        if novelty.get("planned_seed_file_sha256") != sha256_file(seed_path):
            raise SystemExit("seed novelty evidence belongs to a different seed file")
        if novelty.get("planned_holdout") != seed_sets.get("holdout"):
            raise SystemExit("seed novelty evidence holdout set differs from seeds.json")
        if novelty.get("holdout_history_overlap"):
            raise SystemExit("independent holdout overlaps recorded historical seeds")
    context_names = [safe_path(project, item).name for item in config["public_context_files"]]
    if len(context_names) != len(set(context_names)):
        raise SystemExit("public context basenames must be unique")
    backend = generator["backend"]
    if backend == "deterministic":
        test_mode_enabled = os.getenv(DETERMINISTIC_TEST_ENV, "").lower() in {"1", "true", "yes"}
        if generator.get("test_only") is not True or not test_mode_enabled:
            raise SystemExit(
                "deterministic generator is test-only and disabled for normal searches; "
                "configure generator.backend=codex (or an authenticated AI command backend). "
                f"Infrastructure tests must set generator.test_only=true and {DETERMINISTIC_TEST_ENV}=1"
            )
        expected = {(generation, slot) for generation in range(1, budget["max_generations"] + 1)
                    for slot in range(1, budget["candidates_per_generation"] + 1)}
        entries = generator.get("deterministic_plan", [])
        actual = {(item.get("generation"), item.get("slot")) for item in entries}
        if not expected.issubset(actual):
            raise SystemExit(f"deterministic_plan is missing slots: {sorted(expected - actual)}")
        for item in entries:
            if not safe_path(project, item["artifact"]).is_file():
                raise SystemExit(f"deterministic artifact missing: {item['artifact']}")
    elif backend == "command" and not generator.get("command"):
        raise SystemExit("command backend requires generator.command")
    elif backend == "codex":
        if not generator.get("model"):
            raise SystemExit("codex backend requires generator.model")
        executable = generator.get("executable", "codex")
        auth = run_process([executable, "login", "status"], project, 30)
        auth_text = f"{auth.get('stdout', '')}\n{auth.get('stderr', '')}".lower()
        if auth["return_code"] != 0 or auth["timed_out"] or "logged in" not in auth_text:
            raise SystemExit(
                "Codex authentication is required before AI search. Do not put a token in task.json. "
                "Run `codex login --with-access-token` (or `codex login --with-api-key`) in a private "
                "terminal, then retry validation"
            )
    elif backend == "docker-command":
        for key in ("image", "expected_image_id", "command"):
            if not generator.get(key):
                raise SystemExit(f"docker-command backend requires generator.{key}")


def initialize_project(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    if state_path(project).is_file():
        return load_json(state_path(project))
    if (project / "contract.json").exists() or (project / "archive.json").exists():
        raise SystemExit("engine project exists without search-state.json; reconcile manually before starting")
    engine_call(project, "init", str(project), "--name", config["name"],
                "--artifact-kind", config["artifact"].get("kind", "code"))
    save_json(project / "contract.json", config["contract"])
    baseline = engine_call(
        project, "register", str(project), str(safe_path(project, config["artifact"]["baseline"])),
        "--role", "baseline", "--hypothesis", "declared task baseline",
        "--mechanism", "baseline implementation", "--source-label", "task-baseline",
        "--source-type", "provided_baseline", "--author-or-model", "task owner",
    )
    lock = engine_call(project, "lock", str(project), "--reason", "automatic search protocol frozen before baseline evaluation")
    baseline_eval = engine_call(project, "run", str(project), baseline["id"], "--split", "dev",
                                "--repeat-index", "1", "--run-label", "automatic-search-baseline")
    primary_metric = config["contract"]["objectives"][0]["metric"]
    baseline_primary = nested(baseline_eval.get("metrics", {}), primary_metric)
    allow_failing_baseline = bool(config["selection"].get("allow_failing_baseline", False))
    primary_is_finite = (not isinstance(baseline_primary, bool)
                         and isinstance(baseline_primary, (int, float))
                         and math.isfinite(float(baseline_primary)))
    if not primary_is_finite:
        raise SystemExit("baseline must expose a finite primary objective before search")
    if not baseline_eval.get("hard_pass") and not allow_failing_baseline:
        raise SystemExit(
            "baseline must hard-pass unless selection.allow_failing_baseline is true")
    state = {
        "schema_version": SCHEMA_VERSION,
        "search_id": f"search-{uuid.uuid4().hex[:12]}",
        "task_config_hash": sha256_file(config_path(project)),
        "controller_hash": sha256_file(CONTROLLER),
        "evaluation_engine_hash": sha256_file(ENGINE),
        "status": "running",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "current_generation": 1,
        "generator_calls_started": 0,
        "generator_calls_completed": 0,
        "model_calls_started": 0,
        "model_calls_completed": 0,
        "known_cost_usd": 0.0,
        "unknown_cost_calls": 0,
        "accumulated_active_sec": 0.0,
        "no_improvement_generations": 0,
        "best_primary_value": float(baseline_primary) if baseline_eval["hard_pass"] else None,
        "baseline_hard_pass": bool(baseline_eval["hard_pass"]),
        "allow_failing_baseline": allow_failing_baseline,
        "baseline_id": baseline["id"],
        "protocol_lock_id": lock["lock_id"],
        "freeze_id": None,
        "selected_candidate_id": None,
        "stop_reason": None,
        "last_error": None,
    }
    save_json(state_path(project), state)
    append_event(project, "search_initialized", search_id=state["search_id"], baseline_id=baseline["id"],
                 baseline_hard_pass=baseline_eval["hard_pass"], baseline_primary_value=float(baseline_primary),
                 allow_failing_baseline=allow_failing_baseline,
                 protocol_lock_id=lock["lock_id"])
    update_search_archive(project, config)
    return state


def verify_resume_identity(project: Path, state: dict[str, Any]) -> None:
    if sha256_file(config_path(project)) != state["task_config_hash"]:
        raise SystemExit("task.json changed after search start; create a new versioned task")
    if state.get("controller_hash") and sha256_file(CONTROLLER) != state["controller_hash"]:
        raise SystemExit("search controller changed after search start; create a new protocol version")
    recorded_engine = state.get("evaluation_engine_hash", state.get("engine_hash"))
    if sha256_file(ENGINE) != recorded_engine:
        raise SystemExit("evaluation engine changed after search start; create a new protocol version")
    load_events(project)


def latest_dev(candidate: dict[str, Any]) -> dict[str, Any] | None:
    values = candidate.get("evaluations", {}).get("dev", [])
    if isinstance(values, dict):
        return values
    return values[-1] if values else None


def nested(value: dict[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def objective_vector(contract: dict[str, Any], candidate: dict[str, Any]) -> dict[str, float] | None:
    evaluation = latest_dev(candidate)
    if not evaluation or not evaluation.get("hard_pass"):
        return None
    metrics = evaluation.get("metrics", {})
    result: dict[str, float] = {}
    for objective in contract.get("objectives", []):
        raw = nested(metrics, objective["metric"])
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            return None
        result[objective["metric"]] = float(raw)
    return result


def dominates(left: dict[str, float], right: dict[str, float], objectives: list[dict[str, Any]]) -> bool:
    no_worse = True
    strictly_better = False
    for objective in objectives:
        name = objective["metric"]
        if objective.get("direction", "maximize") == "maximize":
            no_worse = no_worse and left[name] >= right[name]
            strictly_better = strictly_better or left[name] > right[name]
        else:
            no_worse = no_worse and left[name] <= right[name]
            strictly_better = strictly_better or left[name] < right[name]
    return no_worse and strictly_better


def primary_sort_key(contract: dict[str, Any], vector: dict[str, float]) -> float:
    objective = contract["objectives"][0]
    value = vector[objective["metric"]]
    return value if objective.get("direction", "maximize") == "maximize" else -value


def update_search_archive(project: Path, config: dict[str, Any]) -> dict[str, Any]:
    engine_archive = load_json(project / "archive.json")
    contract = load_json(project / "contract.json")
    eligible: list[tuple[dict[str, Any], dict[str, float]]] = []
    failures = []
    pending = []
    for candidate in engine_archive["candidates"]:
        vector = objective_vector(contract, candidate)
        evaluation = latest_dev(candidate)
        if vector is not None:
            eligible.append((candidate, vector))
        elif evaluation:
            failures.append({"candidate_id": candidate["id"], "role": candidate["role"],
                             "failure_class": evaluation.get("failure_class"),
                             "evidence_path": evaluation.get("evidence_path")})
        else:
            pending.append(candidate["id"])
    pareto_ids = []
    for candidate, vector in eligible:
        if not any(dominates(other_vector, vector, contract["objectives"])
                   for other, other_vector in eligible if other["id"] != candidate["id"]):
            pareto_ids.append(candidate["id"])
    ranked = sorted(eligible, key=lambda item: primary_sort_key(contract, item[1]), reverse=True)
    elite_ids = [item[0]["id"] for item in ranked[: max(1, int(config["selection"].get("elite_count", 2)))]]
    niche_representatives: dict[str, str] = {}
    for candidate, vector in ranked:
        niche = candidate.get("role") or "unknown"
        niche_representatives.setdefault(niche, candidate["id"])
    signatures: dict[str, list[str]] = {}
    for candidate, _ in eligible:
        signature = candidate.get("behavior_signature")
        if signature and any(value is not None for value in signature.values()):
            signatures.setdefault(sha256_bytes(canonical(signature)), []).append(candidate["id"])
    behavior_similar = [{"signature_hash": key, "candidate_ids": ids}
                        for key, ids in signatures.items() if len(ids) > 1]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "eligible_ids": [candidate["id"] for candidate, _ in eligible],
        "elite_ids": elite_ids,
        "pareto_ids": pareto_ids,
        "niche_representatives": niche_representatives,
        "failures": failures,
        "pending_ids": pending,
        "behavior_similar_groups": behavior_similar,
        "vectors": {candidate["id"]: vector for candidate, vector in eligible},
    }
    save_json(archive_path(project), payload)
    return payload


def candidate_by_id(project: Path, candidate_id: str) -> dict[str, Any]:
    archive = load_json(project / "archive.json")
    for candidate in archive["candidates"]:
        if candidate["id"] == candidate_id:
            return candidate
    raise KeyError(candidate_id)


def select_parent(project: Path, config: dict[str, Any], role: str, slot: int) -> str:
    search_archive = update_search_archive(project, config)
    if role == "repair" and search_archive["failures"]:
        return search_archive["failures"][-1]["candidate_id"]
    pool: list[str] = []
    pool.extend(search_archive["pareto_ids"])
    pool.extend(search_archive["niche_representatives"].values())
    pool.extend(search_archive["elite_ids"])
    unique = list(dict.fromkeys(pool))
    if not unique:
        return load_json(state_path(project))["baseline_id"]
    return unique[(slot - 1) % len(unique)]


def development_feedback(project: Path) -> dict[str, Any]:
    archive = load_json(project / "archive.json")
    rows = []
    for candidate in archive["candidates"]:
        evaluation = latest_dev(candidate)
        if not evaluation:
            continue
        rows.append({
            "candidate_id": candidate["id"],
            "parents": candidate.get("parents", []),
            "role": candidate.get("role"),
            "hypothesis": candidate.get("hypothesis"),
            "mechanism": candidate.get("mechanism"),
            "hard_pass": evaluation.get("hard_pass"),
            "failure_class": evaluation.get("failure_class"),
            "metrics": evaluation.get("metrics", {}),
            "behavior_signature": evaluation.get("behavior_signature", {}),
            "evidence_path": evaluation.get("evidence_path"),
        })
    return {"meaning": "development-only feedback; no holdout outputs", "candidates": rows}


def response_schema(include_artifact: bool = False) -> dict[str, Any]:
    required = ["hypothesis", "mechanism", "summary"]
    properties: dict[str, Any] = {
        "hypothesis": {"type": "string", "minLength": 1},
        "mechanism": {"type": "string", "minLength": 1},
        "summary": {"type": "string"},
    }
    if include_artifact:
        required.append("artifact")
        properties["artifact"] = {"type": "string", "minLength": 1}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def prepare_workspace(project: Path, config: dict[str, Any], generation: int, slot: int,
                      role: str, parent_id: str) -> tuple[str, Path]:
    request_id = f"g{generation:03d}-s{slot:03d}"
    workspace = project / "generator-workspaces" / request_id
    workspace.mkdir(parents=True, exist_ok=True)
    parent = candidate_by_id(project, parent_id)
    parent_path = safe_path(project, parent["artifact_path"])
    candidate_filename = config["artifact"]["candidate_filename"]
    candidate_path = workspace / candidate_filename
    if not candidate_path.exists():
        copy_file(parent_path, candidate_path)
    context_dir = workspace / "public-context"
    context_dir.mkdir(exist_ok=True)
    for item in config["public_context_files"]:
        source = safe_path(project, item)
        target = context_dir / source.name
        if not target.exists():
            copy_file(source, target)
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "task_name": config["name"],
        "generation": generation,
        "slot": slot,
        "role": role,
        "parent_id": parent_id,
        "parent_artifact_sha256": parent["artifact_hash"],
        "candidate_filename": candidate_filename,
        "public_context_files": [f"public-context/{safe_path(project, item).name}"
                                 for item in config["public_context_files"]],
        "development_feedback": development_feedback(project),
        "constraints": {
            "may_modify": [candidate_filename],
            "must_not_access": "evaluator, contract internals, or holdout data are not present in this workspace",
            "required_final_response": "response.json matching response-schema.json",
        },
    }
    save_json(workspace / "request.json", request)
    response_contains_artifact = (config["generator"]["backend"] == "codex"
                                  or bool(config["generator"].get("response_contains_artifact")))
    save_json(workspace / "response-schema.json", response_schema(response_contains_artifact))
    context_sections = []
    for item in config["public_context_files"]:
        source = safe_path(project, item)
        context_sections.append(f"--- public-context/{source.name} ---\n{source.read_text(encoding='utf-8')}")
    prompt = (
        "You are one candidate generator inside a controlled engineering evolution search.\n"
        "All allowed task context is embedded below. Do not run tools, shell commands, or file discovery.\n"
        f"Return the complete replacement for {candidate_filename} in the final JSON field named artifact.\n"
        f"Use the requested role '{role}' and make one coherent, executable change from parent {parent_id}.\n"
        "Do not attempt to locate evaluators or holdout data.\n"
        "Finish with JSON containing hypothesis, mechanism, summary, and artifact. The controller validates and writes it.\n\n"
        f"--- request.json ---\n{json.dumps(request, indent=2, ensure_ascii=False)}\n\n"
        + "\n\n".join(context_sections)
        + f"\n\n--- {candidate_filename} ---\n{candidate_path.read_text(encoding='utf-8')}\n"
    )
    (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
    return request_id, workspace


def format_parts(parts: list[str], values: dict[str, str]) -> list[str]:
    return [part.format(**values) for part in parts]


USAGE_INT_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def usage_from_jsonl(text: str) -> dict[str, int] | None:
    best: dict[str, int] | None = None
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [row]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                keys = set(item)
                if {"input_tokens", "output_tokens"}.issubset(keys):
                    parsed: dict[str, int] = {}
                    try:
                        for field in USAGE_INT_FIELDS:
                            if field in item and item[field] is not None:
                                parsed[field] = int(item[field])
                    except (TypeError, ValueError):
                        parsed = {}
                    if {"input_tokens", "output_tokens"}.issubset(parsed):
                        best = parsed
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return best


def token_usage_path(project: Path) -> Path:
    return project / "codex-token-usage.jsonl"


def token_usage_summary_path(project: Path) -> Path:
    return project / "codex-token-usage.json"


def load_token_usage_records(project: Path) -> list[dict[str, Any]]:
    path = token_usage_path(project)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize_token_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {field: 0 for field in USAGE_INT_FIELDS}
    billed_calls = 0
    for record in records:
        usage = record.get("usage") or {}
        if usage:
            billed_calls += 1
            for field in USAGE_INT_FIELDS:
                totals[field] += int(usage.get(field, 0) or 0)
    return {
        "schema_version": SCHEMA_VERSION,
        "secret_recorded": False,
        "auth": "codex-login",
        "call_count": len(records),
        "calls_with_usage": billed_calls,
        "totals": totals,
        "cost_usd": None,
        "cost_usd_known": False,
        "records_file": token_usage_path(Path(".")).name,
    }


def write_token_usage_record(project: Path, *, request_id: str, generation: int,
                             call_result: dict[str, Any], workspace: Path) -> None:
    if any(row.get("request_id") == request_id for row in load_token_usage_records(project)):
        return
    usage = call_result.get("usage")
    if not call_result.get("model_call") and not usage:
        return
    task = load_json(project / "task.json")
    record = {
        "schema_version": SCHEMA_VERSION,
        "at": utc_now(),
        "request_id": request_id,
        "generation": generation,
        "model": task.get("generator", {}).get("model"),
        "backend": task.get("generator", {}).get("backend"),
        "auth": "codex-login",
        "secret_recorded": False,
        "model_call": bool(call_result.get("model_call")),
        "usage": usage,
        "cost_usd": call_result.get("cost_usd"),
        "duration_sec": call_result.get("duration_sec"),
        "return_code": call_result.get("return_code"),
        "call_result_path": str((workspace / "call-result.json").relative_to(project)),
    }
    append_jsonl(token_usage_path(project), record)
    records = load_token_usage_records(project)
    save_json(token_usage_summary_path(project), summarize_token_usage(records))


def codex_model_call_observed(text: str, response_path: Path, usage: dict[str, int] | None) -> bool:
    if usage or response_path.is_file():
        return True
    observed_types = {"thread.started", "turn.started", "item.completed", "turn.completed"}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("type") in observed_types:
            return True
    return False


def calculate_cost(generator: dict[str, Any], usage: dict[str, int] | None) -> float | None:
    if not usage:
        return None
    pricing = generator.get("pricing_usd_per_million_tokens")
    if not pricing:
        return None
    return (usage["input_tokens"] * float(pricing["input"]) +
            usage["output_tokens"] * float(pricing["output"])) / 1_000_000


def deterministic_generate(project: Path, config: dict[str, Any], workspace: Path,
                           generation: int, slot: int) -> dict[str, Any]:
    plan = config["generator"].get("deterministic_plan", [])
    match = next((item for item in plan if item.get("generation") == generation and item.get("slot") == slot), None)
    if not match:
        return {"return_code": 2, "stdout": "", "stderr": "deterministic plan entry missing",
                "timed_out": False, "duration_sec": 0.0, "command": ["deterministic"]}
    source = safe_path(project, match["artifact"])
    copy_file(source, workspace / config["artifact"]["candidate_filename"])
    response = {"hypothesis": match["hypothesis"], "mechanism": match["mechanism"],
                "summary": match.get("summary", "deterministic controller fixture")}
    save_json(workspace / "response.json", response)
    return {"return_code": 0, "stdout": json.dumps(response), "stderr": "", "timed_out": False,
            "duration_sec": 0.0, "command": ["deterministic", match["artifact"]],
            "model_call": False, "usage": None, "cost_usd": 0.0}


def invoke_generator(project: Path, config: dict[str, Any], workspace: Path,
                     generation: int, slot: int, deadline: float | None = None) -> dict[str, Any]:
    generator = config["generator"]
    backend = generator["backend"]
    configured_timeout = float(generator.get("timeout_sec", 900))
    timeout = configured_timeout
    remaining = operation_timeout(deadline)
    if remaining is not None:
        timeout = min(timeout, remaining)
    controller_budget_limited = remaining is not None and remaining < configured_timeout
    if timeout <= 0:
        return {"return_code": 124, "stdout": "", "stderr": "controller wall-time budget exhausted",
                "timed_out": True, "duration_sec": 0.0, "command": [backend],
                "model_call": False, "usage": None, "cost_usd": None,
                "controller_budget_limited": True}
    values = {"workspace": str(workspace), "request": str(workspace / "request.json"),
              "response": str(workspace / "response.json"),
              "candidate": str(workspace / config["artifact"]["candidate_filename"])}
    if backend == "deterministic":
        return deterministic_generate(project, config, workspace, generation, slot)
    if backend == "command":
        command = format_parts(generator["command"], values)
        result = run_process(command, workspace, timeout)
        result["model_call"] = bool(generator.get("counts_as_model_call", True))
    elif backend == "codex":
        command = [
            generator.get("executable", "codex"), "exec", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--skip-git-repo-check", "--sandbox", "read-only",
            "--config", 'approval_policy="never"',
            "--cd", str(workspace), "--output-schema", str(workspace / "response-schema.json"),
            "--output-last-message", str(workspace / "response.json"), "--json",
            "--model", generator["model"], (workspace / "prompt.txt").read_text(encoding="utf-8"),
        ]
        result = run_process(command, workspace, timeout)
        result["model_call_attempted"] = True
    else:
        image = generator["image"]
        inspect = run_process(["docker", "image", "inspect", image, "--format", "{{.Id}}"], project,
                              min(60.0, timeout))
        if inspect["return_code"] != 0 or inspect["stdout"].strip() != generator["expected_image_id"]:
            return {"return_code": 125, "stdout": inspect["stdout"],
                    "stderr": "docker image identity mismatch: " + inspect["stderr"], "timed_out": False,
                    "duration_sec": inspect["duration_sec"], "command": inspect["command"],
                    "model_call": False, "usage": None, "cost_usd": None}
        mount = f"type=bind,source={workspace},target=/workspace"
        command = ["docker", "run", "--rm", "--cpus", str(generator.get("cpus", 2)),
                   "--memory", str(generator.get("memory", "2g")), "--pids-limit", "256",
                   "--network", generator.get("network", "none"), "--mount", mount,
                   "--workdir", "/workspace"]
        for name in generator.get("env_allowlist", []):
            if name in os.environ:
                command.extend(["--env", name])
        command.append(generator["expected_image_id"])
        command.extend(format_parts(generator["command"],
                                    {"workspace": "/workspace", "request": "/workspace/request.json",
                                     "response": "/workspace/response.json",
                                     "candidate": "/workspace/" + config["artifact"]["candidate_filename"]}))
        docker_remaining = remaining_sec(deadline)
        result = run_process(command, workspace,
                             min(timeout, docker_remaining) if docker_remaining is not None else timeout)
        result["model_call"] = bool(generator.get("counts_as_model_call", True))
    usage = usage_from_jsonl(result.get("stdout", ""))
    result["usage"] = usage
    if backend == "codex":
        result["model_call"] = codex_model_call_observed(
            result.get("stdout", ""), workspace / "response.json", usage)
    result["cost_usd"] = calculate_cost(generator, usage)
    result["controller_budget_limited"] = controller_budget_limited
    return result


def request_events(project: Path, request_id: str) -> list[dict[str, Any]]:
    return [event for event in load_events(project) if event.get("request_id") == request_id]


def request_is_terminal(project: Path, request_id: str) -> bool:
    return any(event["type"] in TERMINAL_GENERATION_EVENTS for event in request_events(project, request_id))


def reconcile_call_accounting(project: Path, state: dict[str, Any], *, save: bool = True) -> dict[str, Any]:
    """Derive call accounting from the append-only event log."""
    events = load_events(project)
    started = [row for row in events if row["type"] == "generation_started"]
    finished = [row for row in events if row["type"] == "generation_call_finished"]
    state["generator_calls_started"] = len(started)
    state["generator_calls_completed"] = len(finished)
    state["model_calls_started"] = sum(bool(row.get("counts_as_model_call")) for row in started)
    state["model_calls_completed"] = sum(bool(row.get("model_call")) for row in finished)
    model_finished = [row for row in finished if row.get("model_call")]
    state["known_cost_usd"] = sum(float(row["cost_usd"]) for row in model_finished
                                  if row.get("cost_usd") is not None)
    state["unknown_cost_calls"] = sum(row.get("cost_usd") is None for row in model_finished)
    state["updated_at"] = utc_now()
    if save:
        save_json(state_path(project), state)
    return state


def register_and_evaluate(project: Path, config: dict[str, Any], state: dict[str, Any], request_id: str,
                          workspace: Path, generation: int, role: str, parent_id: str,
                          call_result: dict[str, Any], deadline: float | None = None) -> None:
    response_file = workspace / "response.json"
    candidate_file = workspace / config["artifact"]["candidate_filename"]
    if not response_file.is_file() or not candidate_file.is_file():
        append_event(project, "generation_failed", request_id=request_id, generation=generation, role=role,
                     parent_id=parent_id, failure_class="missing_generator_output",
                     call_result_path=str((workspace / "call-result.json").relative_to(project)))
        return
    try:
        response = load_json(response_file)
        for key in ("hypothesis", "mechanism", "summary"):
            if not isinstance(response.get(key), str) or not response[key].strip():
                raise ValueError(f"response.{key} missing")
        if config["generator"]["backend"] == "codex" or config["generator"].get("response_contains_artifact"):
            if not isinstance(response.get("artifact"), str) or not response["artifact"].strip():
                raise ValueError("response.artifact missing")
            candidate_file.write_text(response["artifact"], encoding="utf-8", newline="\n")
    except (json.JSONDecodeError, ValueError) as exc:
        append_event(project, "generation_failed", request_id=request_id, generation=generation, role=role,
                     parent_id=parent_id, failure_class="invalid_generator_response", error=str(exc))
        return
    generator = config["generator"]
    model_calls = 1 if call_result.get("model_call") else 0
    register_args = [
        "register", str(project), str(candidate_file), "--parent", parent_id,
        "--generation", str(generation), "--role", role,
        "--hypothesis", response["hypothesis"], "--mechanism", response["mechanism"],
        "--source-label", f"automatic-{request_id}", "--source-type", f"automatic_{generator['backend']}",
        "--author-or-model", generator.get("model", generator["backend"]),
        "--model-calls", str(model_calls), "--generation-duration-sec", str(call_result["duration_sec"]),
        "--generation-notes", response["summary"],
    ]
    if call_result.get("cost_usd") is not None:
        register_args.extend(["--generation-cost-usd", str(call_result["cost_usd"])])
    registered = engine_call(project, *register_args, timeout=operation_timeout(deadline))
    if "duplicate_of" in registered:
        existing = candidate_by_id(project, registered["duplicate_of"])
        if existing.get("source_label") == f"automatic-{request_id}":
            evaluation = latest_dev(existing)
            if evaluation is None:
                evaluation = engine_call(project, "run", str(project), existing["id"], "--split", "dev",
                                         "--repeat-index", "1", "--run-label", f"automatic-recovered-{request_id}",
                                         timeout=operation_timeout(deadline))
            append_event(project, "generation_evaluated", request_id=request_id, generation=generation,
                         role=role, parent_id=parent_id, candidate_id=existing["id"],
                         artifact_hash=existing["artifact_hash"], hard_pass=evaluation["hard_pass"],
                         failure_class=evaluation["failure_class"], metrics=evaluation.get("metrics", {}),
                         evidence_path=evaluation["evidence_path"], response=response,
                         recovered_registration=True)
            return
        append_event(project, "generation_duplicate", request_id=request_id, generation=generation, role=role,
                     parent_id=parent_id, duplicate_of=registered["duplicate_of"],
                     artifact_hash=registered["hash"], response=response)
        return
    evaluation = engine_call(project, "run", str(project), registered["id"], "--split", "dev",
                             "--repeat-index", "1", "--run-label", f"automatic-{request_id}",
                             timeout=operation_timeout(deadline))
    append_event(project, "generation_evaluated", request_id=request_id, generation=generation, role=role,
                 parent_id=parent_id, candidate_id=registered["id"], artifact_hash=registered["artifact_hash"],
                 hard_pass=evaluation["hard_pass"], failure_class=evaluation["failure_class"],
                 metrics=evaluation.get("metrics", {}), evidence_path=evaluation["evidence_path"], response=response)


def account_finished_call(project: Path, state: dict[str, Any], request_id: str,
                          generation: int, call_result: dict[str, Any], workspace: Path) -> None:
    if any(event["type"] == "generation_call_finished" for event in request_events(project, request_id)):
        reconcile_call_accounting(project, state)
        return
    append_event(project, "generation_call_finished", request_id=request_id, generation=generation,
                 return_code=call_result["return_code"], timed_out=call_result["timed_out"],
                 duration_sec=call_result["duration_sec"], usage=call_result.get("usage"),
                 cost_usd=call_result.get("cost_usd"), model_call=bool(call_result.get("model_call")),
                 call_result_path=str((workspace / "call-result.json").relative_to(project)))
    write_token_usage_record(project, request_id=request_id, generation=generation,
                             call_result=call_result, workspace=workspace)
    reconcile_call_accounting(project, state)


def run_slot(project: Path, config: dict[str, Any], state: dict[str, Any], generation: int,
             slot: int, role: str, deadline: float | None = None, *, allow_new: bool = True) -> None:
    request_id = f"g{generation:03d}-s{slot:03d}"
    workspace = project / "generator-workspaces" / request_id
    events = request_events(project, request_id)
    if any(event["type"] in TERMINAL_GENERATION_EVENTS for event in events):
        return
    started = next((event for event in events if event["type"] == "generation_started"), None)
    if started:
        call_result_path = workspace / "call-result.json"
        if not call_result_path.is_file():
            append_event(project, "generation_abandoned_unknown_outcome", request_id=request_id,
                         generation=generation, role=role, parent_id=started["parent_id"],
                         reason="started call has no complete response; not retried to avoid duplicate billing")
            return
        call_result = load_json(call_result_path)
        account_finished_call(project, state, request_id, generation, call_result, workspace)
        if call_result["return_code"] != 0 or call_result["timed_out"]:
            if call_result.get("controller_budget_limited"):
                state["stop_reason"] = "wall_time_budget_exhausted"
                state["updated_at"] = utc_now()
                save_json(state_path(project), state)
            append_event(project, "generation_failed", request_id=request_id, generation=generation, role=role,
                         parent_id=started["parent_id"],
                         failure_class="generator_timeout" if call_result["timed_out"] else "generator_nonzero_exit",
                         return_code=call_result["return_code"],
                         call_result_path=str(call_result_path.relative_to(project)), recovered=True)
        else:
            register_and_evaluate(project, config, state, request_id, workspace, generation, role,
                                  started["parent_id"], call_result, deadline)
        return
    if not allow_new:
        return
    parent_id = select_parent(project, config, role, slot)
    request_id, workspace = prepare_workspace(project, config, generation, slot, role, parent_id)
    budget = config["budget"]
    if state["generator_calls_started"] >= budget["max_generator_calls"]:
        return
    is_model = config["generator"]["backend"] != "deterministic" and bool(
        config["generator"].get("counts_as_model_call", True))
    append_event(project, "generation_started", request_id=request_id, generation=generation, slot=slot,
                 role=role, parent_id=parent_id, workspace=str(workspace.relative_to(project)),
                 backend=config["generator"]["backend"], counts_as_model_call=is_model,
                 prompt_sha256=sha256_file(workspace / "prompt.txt"))
    reconcile_call_accounting(project, state)
    call_result = invoke_generator(project, config, workspace, generation, slot, deadline)
    save_json(workspace / "call-result.json", call_result)
    (workspace / "generator-stdout.log").write_text(call_result.get("stdout", ""), encoding="utf-8")
    (workspace / "generator-stderr.log").write_text(call_result.get("stderr", ""), encoding="utf-8")
    account_finished_call(project, state, request_id, generation, call_result, workspace)
    if call_result["return_code"] != 0 or call_result["timed_out"]:
        if call_result.get("controller_budget_limited"):
            state["stop_reason"] = "wall_time_budget_exhausted"
            state["updated_at"] = utc_now()
            save_json(state_path(project), state)
        append_event(project, "generation_failed", request_id=request_id, generation=generation, role=role,
                     parent_id=parent_id,
                     failure_class="generator_timeout" if call_result["timed_out"] else "generator_nonzero_exit",
                     return_code=call_result["return_code"],
                     call_result_path=str((workspace / "call-result.json").relative_to(project)))
        return
    register_and_evaluate(project, config, state, request_id, workspace, generation, role, parent_id,
                          call_result, deadline)


def recover_inflight_requests(project: Path, config: dict[str, Any], state: dict[str, Any],
                              deadline: float | None) -> None:
    """Resolve already-started requests before applying any new-call stop rule."""
    starts = [row for row in load_events(project) if row["type"] == "generation_started"]
    for started in starts:
        request_id = started["request_id"]
        if request_is_terminal(project, request_id):
            continue
        run_slot(project, config, state, int(started["generation"]), int(started["slot"]),
                 started["role"], deadline, allow_new=False)
        state.update(load_json(state_path(project)))
    reconcile_call_accounting(project, state)


def best_primary(project: Path, config: dict[str, Any]) -> tuple[str | None, float | None]:
    archive = update_search_archive(project, config)
    if not archive["elite_ids"]:
        return None, None
    best_id = archive["elite_ids"][0]
    contract = load_json(project / "contract.json")
    vector = archive["vectors"][best_id]
    return best_id, vector[contract["objectives"][0]["metric"]]


def improved(contract: dict[str, Any], previous: float | None, current: float | None,
             minimum_delta: float) -> bool:
    if current is None:
        return False
    if previous is None:
        return True
    direction = contract["objectives"][0].get("direction", "maximize")
    return current > previous + minimum_delta if direction == "maximize" else current < previous - minimum_delta


def should_stop(project: Path, config: dict[str, Any], state: dict[str, Any]) -> str | None:
    budget = config["budget"]
    # Wall time is a hard execution bound and takes precedence when multiple
    # stop conditions become true at the same boundary.
    if state["accumulated_active_sec"] >= budget["max_wall_time_sec"]:
        return "wall_time_budget_exhausted"
    if state["current_generation"] > budget["max_generations"]:
        return "generation_budget_exhausted"
    if state["generator_calls_started"] >= budget["max_generator_calls"]:
        return "generator_call_budget_exhausted"
    max_cost = budget.get("max_known_cost_usd")
    if max_cost is not None and state["known_cost_usd"] >= float(max_cost):
        return "known_cost_budget_exhausted"
    patience = budget.get("no_improvement_generations")
    if patience is not None and state["no_improvement_generations"] >= int(patience):
        return "no_improvement_stop"
    return None


def existing_holdout_run(project: Path, freeze_id: str, candidate_id: str,
                         repeat: int) -> dict[str, Any] | None:
    for path in sorted((project / "runs-v2").glob("*/run.json")):
        try:
            row = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if (row.get("candidate_id") == candidate_id and row.get("split") == "holdout"
                and row.get("repeat_index") == repeat and row.get("freeze_id") == freeze_id
                and isinstance(row.get("evaluation"), dict)):
            return row
    return None


def matching_holdout_attempts(project: Path, freeze_id: str, candidate_id: str,
                              repeat: int) -> list[tuple[Path, dict[str, Any]]]:
    attempts = []
    for path in sorted((project / "runs-v2").glob("*/run.json")):
        try:
            row = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if (row.get("candidate_id") == candidate_id and row.get("split") == "holdout"
                and row.get("repeat_index") == repeat and row.get("freeze_id") == freeze_id):
            attempts.append((path, row))
    return attempts


def terminalize_unfinished_holdout_attempts(project: Path, freeze_id: str, candidate_id: str,
                                            repeat: int, reason: str) -> list[str]:
    """Preserve an orphaned run as an interrupted terminal attempt."""
    run_ids: list[str] = []
    events = load_events(project)
    access_path = project / "holdout-access.jsonl"
    accesses = []
    if access_path.is_file():
        accesses = [json.loads(line) for line in access_path.read_text(encoding="utf-8").splitlines() if line]
    for path, row in matching_holdout_attempts(project, freeze_id, candidate_id, repeat):
        if isinstance(row.get("evaluation"), dict):
            continue
        run_id = row.get("run_id")
        if not run_id:
            continue
        terminal = row.get("attempt_terminal")
        if not isinstance(terminal, dict):
            terminal = {
                "status": "interrupted",
                "recorded_at": utc_now(),
                "reason": reason,
                "retry_key": f"{freeze_id}:{candidate_id}:{repeat}",
                "retried_by_run_id": None,
            }
            row["attempt_terminal"] = terminal
            row["finished_at"] = terminal["recorded_at"]
            save_json(path, row)
        run_ids.append(run_id)
        if not any(item.get("event") == "interrupted" and item.get("run_id") == run_id
                   for item in accesses):
            access = {"at": utc_now(), "event": "interrupted", "run_id": run_id,
                      "freeze_id": freeze_id, "candidate_id": candidate_id,
                      "repeat_index": repeat, "reason": terminal.get("reason", reason)}
            append_jsonl(access_path, access)
            accesses.append(access)
        if not any(item["type"] == "holdout_attempt_interrupted" and item.get("run_id") == run_id
                   for item in events):
            event = append_event(project, "holdout_attempt_interrupted", run_id=run_id,
                                 freeze_id=freeze_id, candidate_id=candidate_id,
                                 repeat_index=repeat, reason=terminal.get("reason", reason))
            events.append(event)
    return run_ids


def link_holdout_retries(project: Path, interrupted_run_ids: list[str], retry_run_id: str) -> None:
    if not interrupted_run_ids:
        return
    events = load_events(project)
    access_path = project / "holdout-access.jsonl"
    accesses = []
    if access_path.is_file():
        accesses = [json.loads(line) for line in access_path.read_text(encoding="utf-8").splitlines() if line]
    for interrupted_id in interrupted_run_ids:
        for path in (project / "runs-v2").glob("*/run.json"):
            row = load_json(path)
            if row.get("run_id") != interrupted_id:
                continue
            terminal = row.get("attempt_terminal")
            if isinstance(terminal, dict) and terminal.get("retried_by_run_id") != retry_run_id:
                terminal["retried_by_run_id"] = retry_run_id
                save_json(path, row)
            break
        if not any(item["type"] == "holdout_retry_linked"
                   and item.get("interrupted_run_id") == interrupted_id
                   and item.get("retry_run_id") == retry_run_id for item in events):
            event = append_event(project, "holdout_retry_linked",
                                 interrupted_run_id=interrupted_id, retry_run_id=retry_run_id)
            events.append(event)
        if not any(item.get("event") == "retry_linked"
                   and item.get("interrupted_run_id") == interrupted_id
                   and item.get("retry_run_id") == retry_run_id for item in accesses):
            access = {"at": utc_now(), "event": "retry_linked",
                      "interrupted_run_id": interrupted_id, "retry_run_id": retry_run_id}
            append_jsonl(access_path, access)
            accesses.append(access)


def holdout_completion(project: Path, config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    freeze_id = state.get("freeze_id")
    if not freeze_id:
        return {"complete": False, "all_hard_pass": False, "freeze_id": None,
                "expected_count": 0, "completed_count": 0,
                "missing": [{"stage": "freeze", "reason": "freeze not recorded"}], "runs": []}
    freeze = next((item for item in load_json(project / "freezes.json").get("freezes", [])
                   if item.get("freeze_id") == freeze_id), None)
    if freeze is None:
        return {"complete": False, "all_hard_pass": False, "freeze_id": freeze_id,
                "expected_count": 0, "completed_count": 0,
                "missing": [{"stage": "freeze", "reason": "freeze record missing"}], "runs": []}
    repeats = int(config["selection"].get("holdout_repeats", 1))
    expected = [(candidate_id, repeat)
                for repeat in range(1, repeats + 1)
                for candidate_id in freeze["comparison_candidate_ids"]]
    completed = []
    missing = []
    for candidate_id, repeat in expected:
        run = existing_holdout_run(project, freeze_id, candidate_id, repeat)
        if run is None:
            missing.append({"candidate_id": candidate_id, "repeat_index": repeat})
        else:
            completed.append(run)
    return {
        "complete": not missing and len(completed) == len(expected),
        "all_hard_pass": bool(completed) and all(bool(row["evaluation"].get("hard_pass")) for row in completed),
        "freeze_id": freeze_id,
        "expected_count": len(expected),
        "completed_count": len(completed),
        "missing": missing,
        "runs": completed,
    }


def freeze_and_holdout(project: Path, config: dict[str, Any], state: dict[str, Any],
                       deadline: float | None = None) -> dict[str, Any]:
    freeze = None
    if state.get("freeze_id"):
        freeze = next((item for item in load_json(project / "freezes.json").get("freezes", [])
                       if item["freeze_id"] == state["freeze_id"]), None)
        if freeze is None:
            raise RuntimeError(f"recorded freeze not found: {state['freeze_id']}")
    else:
        intent = state.get("freeze_intent")
        if intent is None:
            search_archive = update_search_archive(project, config)
            baseline_id = state["baseline_id"]
            if not search_archive["elite_ids"]:
                state["status"] = "no_eligible_candidate"
                state["selected_candidate_id"] = None
                state["last_error"] = "no candidate passed the frozen development hard gate"
                state["updated_at"] = utc_now()
                save_json(state_path(project), state)
                if not any(
                        event["type"] == "search_no_eligible_candidate"
                        for event in load_events(project)):
                    append_event(project, "search_no_eligible_candidate",
                                 stop_reason=state.get("stop_reason"), baseline_id=baseline_id,
                                 failure_count=len(search_archive["failures"]))
                return {
                    "complete": False,
                    "all_hard_pass": False,
                    "freeze_id": None,
                    "expected_count": 0,
                    "completed_count": 0,
                    "missing": [{"stage": "eligible_candidate"}],
                    "runs": [],
                    "no_eligible_candidate": True,
                }
            selected = search_archive["elite_ids"][0]
            comparator_limit = int(config["selection"].get("freeze_comparators", 1))
            comparator_pool = [baseline_id, *search_archive["pareto_ids"], *search_archive["elite_ids"]]
            eligible_ids = set(search_archive["eligible_ids"])
            comparators = [
                item for item in dict.fromkeys(comparator_pool)
                if item != selected and item in eligible_ids
            ][:comparator_limit]
            intent = {
                "selected_candidate_id": selected,
                "comparison_candidate_ids": [selected, *comparators],
                "reason": (f"automatic search stopped: {state['stop_reason']}; selected by frozen primary "
                           "objective among hard-pass candidates"),
            }
            state["freeze_intent"] = intent
            state["status"] = "freezing"
            save_json(state_path(project), state)
            append_event(project, "freeze_started", **intent)
        for item in reversed(load_json(project / "freezes.json").get("freezes", [])):
            if (item.get("selected_candidate_id") == intent["selected_candidate_id"]
                    and item.get("comparison_candidate_ids") == intent["comparison_candidate_ids"]
                    and item.get("reason") == intent["reason"]):
                freeze = item
                append_event(project, "freeze_recovered", freeze_id=freeze["freeze_id"])
                break
        if freeze is None:
            freeze_timeout = operation_timeout(deadline)
            if freeze_timeout is not None and freeze_timeout <= 0:
                return holdout_completion(project, config, state)
            args = ["freeze", str(project), intent["selected_candidate_id"]]
            for comparator in intent["comparison_candidate_ids"][1:]:
                args.extend(["--comparator", comparator])
            args.extend(["--split", "dev", "--reason", intent["reason"]])
            try:
                freeze = engine_call(project, *args, timeout=freeze_timeout)
            except Exception as exc:
                state["status"] = "holdout_incomplete"
                state["last_error"] = f"freeze engine did not complete: {exc}"
                state["updated_at"] = utc_now()
                save_json(state_path(project), state)
                append_event(project, "freeze_controller_error", error=str(exc),
                             timeout=freeze_timeout, error_type=type(exc).__name__)
                return holdout_completion(project, config, state)
        state["freeze_id"] = freeze["freeze_id"]
        state["selected_candidate_id"] = freeze["selected_candidate_id"]
        state["comparison_candidate_ids"] = freeze["comparison_candidate_ids"]
        state["status"] = "holdout"
        save_json(state_path(project), state)
        if not any(event["type"] == "search_frozen" and event.get("freeze_id") == freeze["freeze_id"]
                   for event in load_events(project)):
            append_event(project, "search_frozen", freeze_id=freeze["freeze_id"],
                         selected_candidate_id=freeze["selected_candidate_id"],
                         comparison_candidate_ids=freeze["comparison_candidate_ids"])
    repeats = int(config["selection"].get("holdout_repeats", 1))
    for repeat in range(1, repeats + 1):
        for candidate_id in freeze["comparison_candidate_ids"]:
            interrupted_run_ids = terminalize_unfinished_holdout_attempts(
                project, freeze["freeze_id"], candidate_id, repeat,
                "recovered unfinished holdout attempt before retry")
            existing = existing_holdout_run(project, freeze["freeze_id"], candidate_id, repeat)
            if existing:
                link_holdout_retries(project, interrupted_run_ids, existing["run_id"])
                finished = [row for row in load_events(project)
                            if row["type"] == "holdout_finished"
                            and row.get("freeze_id") == freeze["freeze_id"]
                            and row.get("candidate_id") == candidate_id
                            and row.get("repeat_index") == repeat]
                if not finished:
                    evaluation = existing["evaluation"]
                    append_event(project, "holdout_finished", candidate_id=candidate_id,
                                 repeat_index=repeat, freeze_id=freeze["freeze_id"],
                                 run_id=existing["run_id"], hard_pass=evaluation["hard_pass"],
                                 failure_class=evaluation["failure_class"],
                                 evidence_path=evaluation["evidence_path"], recovered=True)
                continue
            append_event(project, "holdout_started", candidate_id=candidate_id, repeat_index=repeat,
                         freeze_id=freeze["freeze_id"], retry_of_run_ids=interrupted_run_ids)
            try:
                remaining = operation_timeout(deadline)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("controller wall-time budget exhausted before holdout run")
                evaluation = engine_call(project, "run", str(project), candidate_id, "--split", "holdout",
                                         "--repeat-index", str(repeat), "--run-label", "automatic-frozen-holdout",
                                         timeout=remaining)
                append_event(project, "holdout_finished", candidate_id=candidate_id, repeat_index=repeat,
                             freeze_id=freeze["freeze_id"], run_id=evaluation.get("run_id"),
                             retry_of_run_ids=interrupted_run_ids,
                             hard_pass=evaluation["hard_pass"], failure_class=evaluation["failure_class"],
                             evidence_path=evaluation["evidence_path"])
                link_holdout_retries(project, interrupted_run_ids, evaluation["run_id"])
            except Exception as exc:
                failed_attempts = terminalize_unfinished_holdout_attempts(
                    project, freeze["freeze_id"], candidate_id, repeat,
                    f"controller observed interrupted holdout engine: {exc}")
                append_event(project, "holdout_controller_error", candidate_id=candidate_id,
                             repeat_index=repeat, freeze_id=freeze["freeze_id"], error=str(exc),
                             interrupted_run_ids=failed_attempts)
                if isinstance(exc, TimeoutError):
                    return holdout_completion(project, config, state)
    return holdout_completion(project, config, state)


def export_report(project: Path, config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    engine_archive = load_json(project / "archive.json")
    search_archive = update_search_archive(project, config)
    events = load_events(project)
    completion = holdout_completion(project, config, state)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "task": config["name"],
        "search_state": state,
        "budget": config["budget"],
        "generator": {key: value for key, value in config["generator"].items()
                      if key not in {"deterministic_plan", "command", "env_allowlist"}},
        "selected_candidate_id": state.get("selected_candidate_id"),
        "comparison_candidate_ids": state.get("comparison_candidate_ids", []),
        "archive": search_archive,
        "candidates": engine_archive["candidates"],
        "event_count": len(events),
        "holdout_completion": {key: value for key, value in completion.items() if key != "runs"},
        "evidence": {
            "event_log": "search-events.jsonl",
            "engine_archive": "archive.json",
            "search_archive": "search-archive.json",
            "runs": "runs-v2",
            "freezes": "freezes.json",
            "holdout_access": "holdout-access.jsonl",
        },
        "claims": {
            "implemented": ["serial multi-generation control", "event-derived resumable call accounting",
                            "hard-gated evaluation", "elite/Pareto/niche/failure archives",
                            "source and behavior similarity records", "complete frozen holdout matrix checking"],
            "verified_here": "See the task's actual candidates, failures, events, and runs.",
            "not_implied": ["global optimum", "search-efficiency leadership", "automatic reliable judge creation"],
        },
    }
    save_json(project / "final-report.json", payload)
    lines = [
        f"# Automatic evolution report: {config['name']}", "",
        f"- Status: `{state['status']}`", f"- Stop reason: `{state.get('stop_reason')}`",
        f"- Selected candidate: `{state.get('selected_candidate_id')}`",
        f"- Generator calls: {state['generator_calls_completed']} completed / {state['generator_calls_started']} started",
        f"- Model calls: {state['model_calls_completed']} completed / {state['model_calls_started']} started",
        f"- Known cost: ${state['known_cost_usd']:.6f}; unknown-cost calls: {state['unknown_cost_calls']}", "",
        f"- Holdout: {completion['completed_count']} / {completion['expected_count']} terminal runs; "
        f"all hard-pass: {completion['all_hard_pass']}", "",
        "## Archive", "",
        f"- Elite: {', '.join(search_archive['elite_ids']) or 'none'}",
        f"- Pareto: {', '.join(search_archive['pareto_ids']) or 'none'}",
        f"- Failures: {len(search_archive['failures'])}",
        f"- Behavior-similar groups: {len(search_archive['behavior_similar_groups'])}", "",
        "The report establishes results only for this frozen contract, budget, and recorded workloads.",
    ]
    (project / "final-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def execute_search(project: Path) -> dict[str, Any]:
    project = project.resolve()
    config = load_config(project)
    state = initialize_project(project, config)
    verify_resume_identity(project, state)
    reconcile_call_accounting(project, state)
    if state["status"] == "no_eligible_candidate":
        search_archive = update_search_archive(project, config)
        if search_archive["eligible_ids"]:
            raise RuntimeError(
                "no_eligible_candidate state conflicts with eligible archive entries")
        if state.get("freeze_id") is not None or state.get("selected_candidate_id") is not None:
            raise RuntimeError(
                "no_eligible_candidate state unexpectedly contains a freeze or selection")
        if not any(
                event["type"] == "search_no_eligible_candidate"
                for event in load_events(project)):
            append_event(
                project,
                "search_no_eligible_candidate",
                stop_reason=state.get("stop_reason"),
                baseline_id=state["baseline_id"],
                failure_count=len(search_archive["failures"]),
                recovered=True,
            )
        return export_report(project, config, state)
    if state["status"] in {"completed", "completed_with_holdout_failures"}:
        completion = holdout_completion(project, config, state)
        if completion["complete"]:
            derived_status = "completed" if completion["all_hard_pass"] else "completed_with_holdout_failures"
            if state["status"] != derived_status:
                state["status"] = derived_status
                state["updated_at"] = utc_now()
                save_json(state_path(project), state)
                append_event(project, "completion_status_corrected", status=derived_status,
                             freeze_id=state.get("freeze_id"))
            return export_report(project, config, state)
        state["status"] = "holdout_incomplete"
        state["last_error"] = f"completion revoked; missing holdout work: {completion['missing']}"
        save_json(state_path(project), state)
        append_event(project, "completion_revoked", freeze_id=state.get("freeze_id"),
                     missing=completion["missing"])
    session_started = time.perf_counter()
    accumulated_before_session = float(state.get("accumulated_active_sec", 0.0))
    remaining_budget = max(0.0, float(config["budget"]["max_wall_time_sec"]) - accumulated_before_session)
    deadline = session_started + remaining_budget

    def persist_elapsed() -> None:
        state["accumulated_active_sec"] = accumulated_before_session + (time.perf_counter() - session_started)
        state["updated_at"] = utc_now()
        save_json(state_path(project), state)

    try:
        # A durable response belongs to a call that already happened.  Recover it
        # before deciding whether another call may be started.
        recover_inflight_requests(project, config, state, deadline)
        state.update(load_json(state_path(project)))
        persist_elapsed()
        if not state.get("freeze_id") and not state.get("stop_reason"):
            state["status"] = "running"
            while True:
                persist_elapsed()
                reason = should_stop(project, config, state)
                if reason:
                    state["stop_reason"] = reason
                    break
                generation = state["current_generation"]
                slots = int(config["budget"]["candidates_per_generation"])
                roles = config["generator"]["roles"]
                if not any(event["type"] == "generation_opened" and event.get("generation") == generation
                           for event in load_events(project)):
                    append_event(project, "generation_opened", generation=generation, slots=slots)
                for slot in range(1, slots + 1):
                    persist_elapsed()
                    reason = should_stop(project, config, state)
                    if reason:
                        state["stop_reason"] = reason
                        break
                    run_slot(project, config, state, generation, slot, roles[slot - 1], deadline)
                    state = load_json(state_path(project))
                    persist_elapsed()
                archive = update_search_archive(project, config)
                contract = load_json(project / "contract.json")
                best_id, best_value = best_primary(project, config)
                if improved(contract, state.get("best_primary_value"), best_value,
                            float(config["selection"].get("minimum_improvement", 0.0))):
                    state["best_primary_value"] = best_value
                    state["no_improvement_generations"] = 0
                    improvement = True
                else:
                    state["no_improvement_generations"] += 1
                    improvement = False
                append_event(project, "generation_closed", generation=generation, best_candidate_id=best_id,
                             best_primary_value=best_value, improved=improvement,
                             pareto_ids=archive["pareto_ids"], failure_count=len(archive["failures"]),
                             partial=bool(state.get("stop_reason")))
                state["current_generation"] += 1
                state["updated_at"] = utc_now()
                save_json(state_path(project), state)
                if state.get("stop_reason"):
                    break
        persist_elapsed()
        state["status"] = "freezing"
        state["updated_at"] = utc_now()
        save_json(state_path(project), state)
        completion = freeze_and_holdout(project, config, state, deadline)
        state = load_json(state_path(project))
        persist_elapsed()
        if completion.get("no_eligible_candidate"):
            return export_report(project, config, state)
        if not completion["complete"]:
            state["status"] = "holdout_incomplete"
            state["last_error"] = f"expected holdout matrix incomplete: {completion['missing']}"
            state["updated_at"] = utc_now()
            save_json(state_path(project), state)
            append_event(project, "search_incomplete", stop_reason=state.get("stop_reason"),
                         freeze_id=state.get("freeze_id"), expected_count=completion["expected_count"],
                         completed_count=completion["completed_count"], missing=completion["missing"])
            return export_report(project, config, state)
        state["status"] = "completed" if completion["all_hard_pass"] else "completed_with_holdout_failures"
        state["last_error"] = None
        state["updated_at"] = utc_now()
        save_json(state_path(project), state)
        if not any(row["type"] == "search_completed" and row.get("freeze_id") == state["freeze_id"]
                   for row in load_events(project)):
            append_event(project, "search_completed", stop_reason=state["stop_reason"], status=state["status"],
                         holdout_all_hard_pass=completion["all_hard_pass"],
                         selected_candidate_id=state["selected_candidate_id"], freeze_id=state["freeze_id"])
        return export_report(project, config, state)
    except BaseException as exc:
        state = load_json(state_path(project)) if state_path(project).is_file() else state
        state["accumulated_active_sec"] = accumulated_before_session + (time.perf_counter() - session_started)
        state["status"] = "interrupted"
        state["last_error"] = repr(exc)
        state["updated_at"] = utc_now()
        save_json(state_path(project), state)
        append_event(project, "search_interrupted", error=repr(exc))
        raise


def scaffold(template: str, output: Path) -> None:
    source = SKILL_ROOT / "assets" / "task-templates" / template
    if not source.is_dir():
        raise SystemExit(f"unknown template: {template}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    copy_tree(source, output)
    print(output.resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run resumable, evidence-backed multi-generation search")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate"); validate.add_argument("project")
    start = sub.add_parser("start"); start.add_argument("project")
    resume = sub.add_parser("resume"); resume.add_argument("project")
    run = sub.add_parser("run"); run.add_argument("project")
    status = sub.add_parser("status"); status.add_argument("project")
    export = sub.add_parser("export"); export.add_argument("project")
    scaffold_parser = sub.add_parser("scaffold"); scaffold_parser.add_argument("template")
    scaffold_parser.add_argument("output")
    args = parser.parse_args()
    if args.command == "scaffold":
        scaffold(args.template, Path(args.output).resolve())
        return
    project = Path(args.project).resolve()
    config = load_config(project)
    if args.command == "validate":
        print(json.dumps({"valid": True, "project": str(project), "task": config["name"]}, indent=2))
    elif args.command in {"start", "resume", "run"}:
        report = execute_search(project)
        print(json.dumps({"status": report["search_state"]["status"],
                          "selected_candidate_id": report["selected_candidate_id"],
                          "report": str(project / "final-report.json")}, indent=2))
        if report["search_state"]["status"] not in {"completed", "completed_with_holdout_failures"}:
            raise SystemExit(2)
    elif args.command == "status":
        state = load_json(state_path(project)) if state_path(project).is_file() else {"status": "not_started"}
        archive = load_json(archive_path(project)) if archive_path(project).is_file() else None
        print(json.dumps({"task": config["name"], "state": state, "archive": archive}, indent=2,
                         ensure_ascii=False))
    else:
        if not state_path(project).is_file():
            raise SystemExit("search has not started")
        print(json.dumps(export_report(project, config, load_json(state_path(project))), indent=2,
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
