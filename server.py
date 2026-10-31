from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse


SERVER_VERSION = "1.2.4"
ENGINE_VERSION = "evidence-research-loop-v2.6.6"
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
TERMINAL_STATES = {
    "completed",
    "completed_with_holdout_failures",
    "no_eligible_candidate",
    "holdout_incomplete",
}
MAX_FILE_BYTES = int(os.getenv("ERL_MAX_FILE_BYTES", str(1024 * 1024)))
MAX_PROJECT_BYTES = int(os.getenv("ERL_MAX_PROJECT_BYTES", str(100 * 1024 * 1024)))
RUN_UID = int(os.getenv("ERL_RUN_UID", "20000"))
RUN_GID = int(os.getenv("ERL_RUN_GID", "20000"))
SHARED_VOLUME_PERMISSIONS = os.getenv("ERL_SHARED_VOLUME_PERMISSIONS", "").lower() in {"1", "true", "yes"}

APP_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = Path(os.getenv("ERL_SKILL_ROOT", APP_ROOT / "evidence-research-loop")).resolve()
PROJECTS_ROOT = Path(os.getenv("ERL_PROJECTS_ROOT", APP_ROOT / ".data" / "projects")).resolve()
CONTROLLER = SKILL_ROOT / "scripts" / "evolution_controller.py"
AUDITOR = SKILL_ROOT / "scripts" / "audit_search.py"
RESEARCH_GRAPH = SKILL_ROOT / "scripts" / "research_graph.py"
COMPOSITION_GRAPH = SKILL_ROOT / "scripts" / "composition_graph.py"
SCIENTIST_RUNTIME = SKILL_ROOT / "scripts" / "scientist_runtime.py"
VERSION_FILE = APP_ROOT / "version.json"
LOCAL_REGISTRY = PROJECTS_ROOT / ".local-projects.json"
PROJECT_TEMPLATES = {
    "python-code",
    "json-strategy",
    "scientist-mode",
    "research-graph",
    "scientist-runtime",
    "composition-graph",
}

if SHARED_VOLUME_PERMISSIONS:
    # Azure Files reports every entry as uid/gid 0 and does not honor chown.
    # A zero umask lets the separate uid-20000 worker use files that the root
    # MCP process creates, while the container boundary and bearer auth remain
    # the external access controls.
    os.umask(0)
PROJECTS_ROOT.mkdir(mode=0o777, parents=True, exist_ok=True)


def _json_file(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    last_error: OSError | json.JSONDecodeError | None = None
    for attempt in range(8):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    def included(path: Path) -> bool:
        relative = path.relative_to(root)
        return path.is_file() and not any(
            part == "__pycache__" or part.startswith((".tmp-", ".test-"))
            for part in relative.parts
        )

    for path in sorted(item for item in root.rglob("*") if included(item)):
        relative = path.relative_to(root).as_posix()
        files[relative] = {"sha256": _sha256_file(path), "size": path.stat().st_size}
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"file_count": len(files), "tree_sha256": _sha256_bytes(canonical), "files": files}


def _engine_identity() -> dict[str, Any]:
    expected = _json_file(VERSION_FILE, {})
    observed = _tree_manifest(SKILL_ROOT)
    expected_hash = expected.get("skill_tree_sha256")
    return {
        "server_version": SERVER_VERSION,
        "engine_version": ENGINE_VERSION,
        "expected_skill_tree_sha256": expected_hash,
        "observed_skill_tree_sha256": observed["tree_sha256"],
        "engine_hash_valid": bool(expected_hash) and secrets.compare_digest(expected_hash, observed["tree_sha256"]),
        "engine_file_count": observed["file_count"],
    }


def _allowed_local_roots() -> list[Path]:
    raw = os.getenv("ERL_LOCAL_ALLOWED_ROOTS", "")
    return [Path(value).resolve() for value in raw.split(os.pathsep) if value.strip()]


def _local_registry() -> dict[str, str]:
    value = _json_file(LOCAL_REGISTRY, {})
    return value if isinstance(value, dict) else {}


def _is_under(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _project_path(project_id: str, *, must_exist: bool = True) -> Path:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must match ^[a-z0-9][a-z0-9-]{1,62}$")
    registered = _local_registry().get(project_id)
    if registered:
        path = Path(registered).resolve()
        roots = _allowed_local_roots()
        if not roots or not _is_under(path, roots):
            raise PermissionError("registered local project is outside ERL_LOCAL_ALLOWED_ROOTS")
    else:
        path = (PROJECTS_ROOT / project_id).resolve()
        if path.parent != PROJECTS_ROOT:
            raise ValueError("project path escaped the configured workspace")
    if must_exist and not path.is_dir():
        raise FileNotFoundError(f"project does not exist: {project_id}")
    return path


def _relative_path(project: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    if not normalized or normalized.startswith("/") or "\x00" in normalized:
        raise ValueError("relative_path must be a non-empty relative path")
    path = (project / normalized).resolve()
    if project not in path.parents:
        raise ValueError("relative_path escaped the project workspace")
    if path.name in {"", ".", ".."}:
        raise ValueError("relative_path must identify a file")
    return path


def _project_size(project: Path) -> int:
    return sum(path.stat().st_size for path in project.rglob("*") if path.is_file())


def _write_runtime_payload(project: Path, directory: str, suffix: str, data: bytes) -> tuple[Path, str]:
    """Persist an immutable MCP-supplied runtime payload inside the project."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"payload exceeds {MAX_FILE_BYTES} bytes")
    if _project_size(project) + len(data) > MAX_PROJECT_BYTES:
        raise ValueError(f"project would exceed {MAX_PROJECT_BYTES} bytes")
    target_dir = project / directory
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{suffix}"
    target.write_bytes(data)
    _chown_tree(target_dir)
    return target, _sha256_bytes(data)


def _chown_tree(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    for item in [path, *path.rglob("*")]:
        try:
            os.chown(item, RUN_UID, RUN_GID)
            # Docker's immutable engine tree is copied with mode 0555.  A
            # project cloned from its templates inherits those modes, so the
            # unprivileged search worker owns the files but still cannot add
            # artifacts.  Preserve existing execute/read bits while ensuring
            # the owner can mutate its isolated project workspace.
            mode = stat.S_IMODE(item.stat().st_mode) | stat.S_IRUSR | stat.S_IWUSR
            if item.is_dir():
                mode |= stat.S_IXUSR
            os.chmod(item, mode)
        except OSError:
            pass


def _copy_template(source: Path, destination: Path) -> None:
    """Copy a trusted template without inheriting the engine's read-only modes."""
    if not SHARED_VOLUME_PERMISSIONS:
        shutil.copytree(source, destination)
        return
    destination.mkdir(mode=0o777, parents=True)
    for item in sorted(source.rglob("*")):
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(mode=0o777, parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(mode=0o777, parents=True, exist_ok=True)
            shutil.copyfile(item, target)


def _drop_privileges() -> None:
    if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(RUN_GID)
    os.setuid(RUN_UID)
    os.umask(0 if SHARED_VOLUME_PERMISSIONS else 0o027)


def _subprocess_env(project: Path) -> dict[str, str]:
    home = project / ".runtime" / "home"
    temp = project / ".runtime" / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    _chown_tree(project / ".runtime")
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "ERL_SKILL_ROOT": str(SKILL_ROOT),
    }
    if os.getenv("ERL_ALLOW_DETERMINISTIC_TEST_MODE", "").lower() in {"1", "true", "yes"}:
        env["ERL_ALLOW_DETERMINISTIC_TEST_MODE"] = "1"
    if SHARED_VOLUME_PERMISSIONS:
        env["ERL_SHARED_VOLUME_PERMISSIONS"] = "1"
    return env


def _run_sync(command: list[str], project: Path, timeout: int = 120) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": project,
        "env": _subprocess_env(project),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            kwargs["preexec_fn"] = _drop_privileges
    started = time.time()
    try:
        result = subprocess.run(command, **kwargs)
        return {
            "exit_code": result.returncode,
            "timed_out": False,
            "elapsed_sec": round(time.time() - started, 6),
            "stdout": result.stdout[-200_000:],
            "stderr": result.stderr[-200_000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "timed_out": True,
            "elapsed_sec": round(time.time() - started, 6),
            "stdout": (exc.stdout or "")[-200_000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-200_000:] if isinstance(exc.stderr, str) else "",
        }


def _parse_last_json(stdout: str) -> Any:
    decoder = json.JSONDecoder()
    candidates = [index for index, char in enumerate(stdout) if char in "[{"]
    parsed = None
    for index in candidates:
        try:
            value, end = decoder.raw_decode(stdout[index:])
            if not stdout[index + end :].strip():
                parsed = value
        except json.JSONDecodeError:
            continue
    return parsed


def _command_result(result: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_last_json(result.get("stdout", ""))
    return {**result, "result": parsed}


def _state_summary(project: Path) -> dict[str, Any]:
    state = _json_file(project / "search-state.json", {"status": "not_started"})
    return {
        "status": state.get("status", "not_started"),
        "stop_reason": state.get("stop_reason"),
        "generation": state.get("generation"),
        "generator_calls_started": state.get("generator_calls_started", 0),
        "generator_calls_completed": state.get("generator_calls_completed", 0),
        "selected_candidate_id": state.get("selected_candidate_id"),
        "freeze_id": state.get("freeze_id"),
    }


def _scientist_state_summary(project: Path) -> dict[str, Any]:
    state = _json_file(project / "scientist-state.json", {"status": "not_started"})
    stages = state.get("stages", {}) if isinstance(state, dict) else {}
    return {
        "status": state.get("status", "not_started"),
        "current_stage": state.get("current_stage"),
        "stop_reason": state.get("stop_reason"),
        "plan_version": state.get("plan_version"),
        "plan_hash": state.get("plan_hash"),
        "stages": {name: value.get("status") for name, value in stages.items()
                   if isinstance(value, dict)},
    }


def _research_graph_state_summary(project: Path) -> dict[str, Any]:
    graph = _json_file(project / "research-graph.json")
    if not isinstance(graph, dict):
        return {"status": "not_initialized"}
    counts: dict[str, int] = {}
    nodes = graph.get("nodes", {})
    if isinstance(nodes, dict):
        for node in nodes.values():
            if isinstance(node, dict):
                status = str(node.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
    goal_ids = graph.get("goal_node_ids", [])
    goals = {node_id: nodes.get(node_id, {}).get("status", "missing")
             for node_id in goal_ids if isinstance(node_id, str)}
    return {
        "status": "accepted" if goals and all(value == "verified" for value in goals.values()) else "active",
        "graph_id": graph.get("graph_id"),
        "blueprint_revision": graph.get("blueprint_revision"),
        "counts": counts,
        "goals": goals,
        "accepted": bool(goals) and all(value == "verified" for value in goals.values()),
    }


def _active_job(project: Path) -> dict[str, Any] | None:
    jobs = project / "mcp-jobs"
    if not jobs.is_dir():
        return None
    for meta_path in sorted(jobs.glob("*/meta.json"), reverse=True):
        meta = _json_file(meta_path, {})
        result_path = meta_path.parent / "result.json"
        if result_path.is_file():
            continue
        pid = meta.get("pid")
        if pid and _pid_exists(int(pid)):
            return {**meta, "status": "running"}
        if meta:
            return {**meta, "status": "unknown_after_process_exit"}
    return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _launch_job(project: Path, mode: str) -> dict[str, Any]:
    active = _active_job(project)
    if active and active["status"] == "running":
        return {"launched": False, "reason": "job_already_running", "job": active}
    job_id = f"job-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    job_dir = project / "mcp-jobs" / job_id
    job_dir.mkdir(parents=True)
    _chown_tree(job_dir)
    meta = {"job_id": job_id, "mode": mode, "started_at_unix": time.time(), "project": project.name}
    meta_path = job_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    _chown_tree(meta_path)
    log_handle = (job_dir / "worker.log").open("ab", buffering=0)
    command = [sys.executable, str(APP_ROOT / "worker.py"), mode, str(project), str(job_dir / "result.json")]
    kwargs: dict[str, Any] = {
        "cwd": project,
        "env": _subprocess_env(project),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            kwargs["preexec_fn"] = _drop_privileges
    process = subprocess.Popen(command, **kwargs)
    log_handle.close()
    meta["pid"] = process.pid
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    _chown_tree(meta_path)
    return {"launched": True, "job": {**meta, "status": "running"}}


def _job_status(project: Path, job_id: str | None = None) -> dict[str, Any]:
    jobs = project / "mcp-jobs"
    if not jobs.is_dir():
        return {"status": "no_jobs"}
    job_dirs = sorted((path for path in jobs.iterdir() if path.is_dir()), reverse=True)
    if job_id:
        job_dirs = [path for path in job_dirs if path.name == job_id]
    if not job_dirs:
        return {"status": "job_not_found", "job_id": job_id}
    job_dir = job_dirs[0]
    meta = _json_file(job_dir / "meta.json", {})
    result = _json_file(job_dir / "result.json")
    log_path = job_dir / "worker.log"
    log_tail = ""
    if log_path.is_file():
        with log_path.open("rb") as handle:
            handle.seek(max(0, log_path.stat().st_size - 50_000))
            log_tail = handle.read().decode("utf-8", errors="replace")
    status = "finished" if result is not None else ("running" if _pid_exists(int(meta.get("pid", 0))) else "unknown_after_process_exit")
    return {
        "status": status,
        "meta": meta,
        "result": result,
        "log_tail": log_tail,
        "search": _state_summary(project),
        "scientist": _scientist_state_summary(project),
        "research_graph": _research_graph_state_summary(project),
    }


mcp = MCPServer(
    "evidence-research-loop",
    title="Evidence Research Loop",
    description=(
        "Evidence-backed, resumable engineering solution evolution with versioned outer search, "
        "a node-level Research Graph, and a command-backed Scientist Runtime."
    ),
    version=SERVER_VERSION,
    instructions=(
        "Create or upload a versioned task with a baseline and frozen evaluator, validate it, then start or resume "
        "the search. Treat completed as execution completeness; use audit_project to determine evidence validity and acceptance."
    ),
)


@mcp.custom_route("/healthz", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    identity = _engine_identity()
    code = 200 if identity["engine_hash_valid"] else 503
    return JSONResponse({"status": "ok" if code == 200 else "engine_hash_mismatch", **identity}, status_code=code)


@mcp.tool()
def get_capabilities() -> dict[str, Any]:
    """Return server, engine, security-boundary, template, and workspace capability information."""
    identity = _engine_identity()
    return {
        **identity,
        "templates": sorted(PROJECT_TEMPLATES),
        "workspace_root": str(PROJECTS_ROOT),
        "max_file_bytes": MAX_FILE_BYTES,
        "max_project_bytes": MAX_PROJECT_BYTES,
        "execution": {
            "background_jobs": True,
            "controller_backends_bundled": ["deterministic", "command"],
            "research_graph": True,
            "composition_graph": True,
            "composition_guarantee": "empirical",
            "scientist_runtime": True,
            "codex_cli_bundled": False,
            "process_identity": {"uid": RUN_UID, "gid": RUN_GID},
            "secret_inheritance_to_jobs": False,
        },
    }


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List server-side research projects and their concise search state."""
    projects = []
    for path in sorted(item for item in PROJECTS_ROOT.iterdir() if item.is_dir() and PROJECT_ID.fullmatch(item.name)):
        projects.append({"project_id": path.name, "location": "managed", "path": str(path),
                         "size_bytes": _project_size(path), **_state_summary(path)})
    for project_id, raw_path in sorted(_local_registry().items()):
        try:
            path = _project_path(project_id)
            projects.append({"project_id": project_id, "location": "attached-local", "path": str(path),
                             "size_bytes": _project_size(path), **_state_summary(path)})
        except (OSError, ValueError, PermissionError) as exc:
            projects.append({"project_id": project_id, "location": "attached-local", "path": raw_path,
                             "status": "unavailable", "error": str(exc)})
    return {"count": len(projects), "projects": projects}


@mcp.tool()
def attach_local_project(project_id: str, absolute_path: str) -> dict[str, Any]:
    """Attach an existing local directory; disabled unless ERL_LOCAL_ALLOWED_ROOTS is configured."""
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must match ^[a-z0-9][a-z0-9-]{1,62}$")
    roots = _allowed_local_roots()
    if not roots:
        raise PermissionError("local project attachment is disabled on this server")
    path = Path(absolute_path).resolve()
    if not path.is_dir():
        raise FileNotFoundError(absolute_path)
    if not _is_under(path, roots):
        raise PermissionError("path is outside ERL_LOCAL_ALLOWED_ROOTS")
    managed = PROJECTS_ROOT / project_id
    registry = _local_registry()
    if managed.exists() or project_id in registry:
        raise FileExistsError(f"project ID is already registered: {project_id}")
    registry[project_id] = str(path)
    temp = LOCAL_REGISTRY.with_name(f".{LOCAL_REGISTRY.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(LOCAL_REGISTRY)
    return {"attached": True, "project_id": project_id, "path": str(path),
            "allowed_root": str(next(root for root in roots if path == root or root in path.parents))}


@mcp.tool()
def create_project(project_id: str, template: str = "python-code") -> dict[str, Any]:
    """Create a new immutable-by-default project from a bundled task template."""
    identity = _engine_identity()
    if not identity["engine_hash_valid"]:
        raise RuntimeError("bundled engine hash does not match version.json")
    if template not in PROJECT_TEMPLATES:
        raise ValueError(f"template must be one of: {', '.join(sorted(PROJECT_TEMPLATES))}")
    project = _project_path(project_id, must_exist=False)
    if project.exists():
        raise FileExistsError(f"project already exists: {project_id}")
    source = SKILL_ROOT / "assets" / "task-templates" / template
    _copy_template(source, project)
    metadata = {
        "project_id": project_id,
        "template": template,
        "created_at_unix": time.time(),
        "server_version": SERVER_VERSION,
        "engine_version": ENGINE_VERSION,
        "engine_tree_sha256": identity["observed_skill_tree_sha256"],
    }
    (project / "mcp-project.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    _chown_tree(project)
    return {"created": True, "project": metadata, "size_bytes": _project_size(project)}


@mcp.tool()
def write_files(project_id: str, files: list[dict[str, str]], overwrite: bool = False) -> dict[str, Any]:
    """Write UTF-8 or base64 files before search starts; optional sha256 values guard transfer integrity."""
    project = _project_path(project_id)
    state_files = ("search-state.json", "scientist-state.json", "research-graph.json", "composition.json")
    if any((project / name).exists() for name in state_files):
        raise RuntimeError(
            "project has started; create a new versioned project instead of changing frozen inputs "
            "(research graph artifacts and revisions have dedicated tools)"
        )
    if not files or len(files) > 100:
        raise ValueError("files must contain between 1 and 100 entries")
    prepared: list[tuple[Path, bytes, str]] = []
    seen: set[Path] = set()
    for item in files:
        relative = item.get("path", "")
        destination = _relative_path(project, relative)
        if destination in seen:
            raise ValueError(f"duplicate path in request: {relative}")
        seen.add(destination)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"file exists and overwrite is false: {relative}")
        if "content_base64" in item:
            try:
                data = base64.b64decode(item["content_base64"], validate=True)
            except Exception as exc:
                raise ValueError(f"invalid base64 for {relative}") from exc
        elif "content" in item:
            data = item["content"].encode("utf-8")
        else:
            raise ValueError(f"{relative} must contain content or content_base64")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        digest = _sha256_bytes(data)
        expected = item.get("sha256")
        if expected and not secrets.compare_digest(expected.lower(), digest):
            raise ValueError(f"sha256 mismatch for {relative}")
        prepared.append((destination, data, digest))
    replaced_size = sum(path.stat().st_size for path, _, _ in prepared if path.is_file())
    projected = _project_size(project) - replaced_size + sum(len(data) for _, data, _ in prepared)
    if projected > MAX_PROJECT_BYTES:
        raise ValueError(f"project would exceed {MAX_PROJECT_BYTES} bytes")
    written = []
    for destination, data, digest in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(data)
        temp.replace(destination)
        _chown_tree(destination.parent)
        written.append({"path": destination.relative_to(project).as_posix(), "size": len(data), "sha256": digest})
    return {"project_id": project_id, "written": written, "project_size_bytes": projected}


@mcp.tool()
def list_files(project_id: str, prefix: str = "", limit: int = 500) -> dict[str, Any]:
    """List project files with sizes and hashes for evidence review."""
    project = _project_path(project_id)
    if limit < 1 or limit > 2000:
        raise ValueError("limit must be between 1 and 2000")
    rows = []
    for path in sorted(item for item in project.rglob("*") if item.is_file()):
        relative = path.relative_to(project).as_posix()
        if prefix and not relative.startswith(prefix):
            continue
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256_file(path)})
        if len(rows) >= limit:
            break
    return {"project_id": project_id, "count": len(rows), "truncated": len(rows) == limit, "files": rows}


@mcp.tool()
def read_file(project_id: str, relative_path: str, max_bytes: int = 262144) -> dict[str, Any]:
    """Read one project file for review, returning UTF-8 text when possible and base64 otherwise."""
    project = _project_path(project_id)
    path = _relative_path(project, relative_path)
    if not path.is_file():
        raise FileNotFoundError(relative_path)
    if max_bytes < 1 or max_bytes > MAX_FILE_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_FILE_BYTES}")
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"file is {len(data)} bytes; requested maximum is {max_bytes}")
    result = {"path": relative_path, "size": len(data), "sha256": _sha256_bytes(data)}
    try:
        result.update({"encoding": "utf-8", "content": data.decode("utf-8")})
    except UnicodeDecodeError:
        result.update({"encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")})
    return result


@mcp.tool()
async def validate_project(project_id: str) -> dict[str, Any]:
    """Validate task.json, baseline, evaluator contract, frozen inputs, and engine compatibility."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(CONTROLLER), "validate", str(project)], project, 120
    )
    return {"project_id": project_id, "valid": result["exit_code"] == 0 and not result["timed_out"], **_command_result(result)}


@mcp.tool()
def start_search(project_id: str) -> dict[str, Any]:
    """Start a validated search as a background job and return immediately with a job ID."""
    project = _project_path(project_id)
    if (project / "search-state.json").exists():
        raise RuntimeError("search-state.json already exists; use resume_search")
    return {"project_id": project_id, **_launch_job(project, "start")}


@mcp.tool()
def resume_search(project_id: str) -> dict[str, Any]:
    """Resume a previously started or interrupted search as a background job."""
    project = _project_path(project_id)
    if not (project / "search-state.json").exists():
        raise RuntimeError("search has not started; use start_search")
    return {"project_id": project_id, **_launch_job(project, "resume")}


@mcp.tool()
def get_status(project_id: str, job_id: str | None = None) -> dict[str, Any]:
    """Return the latest background job, controller state, and worker log tail."""
    project = _project_path(project_id)
    return {"project_id": project_id, **_job_status(project, job_id)}


@mcp.tool()
async def audit_project(project_id: str, verify_manifest: bool = False) -> dict[str, Any]:
    """Independently audit event chains, runs, counters, freeze matrix, hashes, and acceptance."""
    project = _project_path(project_id)
    command = [sys.executable, str(AUDITOR), str(project)]
    if verify_manifest:
        command.append("--verify-manifest")
    result = await asyncio.to_thread(_run_sync, command, project, 180)
    return {"project_id": project_id, **_command_result(result)}


@mcp.tool()
async def finalize_evidence(project_id: str) -> dict[str, Any]:
    """Write audit-summary.json and evidence-manifest.json after a terminal search, then verify them."""
    project = _project_path(project_id)
    state = _state_summary(project)
    if state["status"] not in TERMINAL_STATES:
        raise RuntimeError(f"search is not terminal: {state['status']}")
    write = await asyncio.to_thread(
        _run_sync,
        [sys.executable, str(AUDITOR), str(project), "--write-summary", "--write-manifest"],
        project,
        180,
    )
    if write["exit_code"] != 0:
        return {"project_id": project_id, "written": False, **_command_result(write)}
    verify = await asyncio.to_thread(
        _run_sync,
        [sys.executable, str(AUDITOR), str(project), "--verify-manifest"],
        project,
        180,
    )
    return {"project_id": project_id, "written": True, "write": _command_result(write), "verify": _command_result(verify)}


@mcp.tool()
async def initialize_research_graph(
    project_id: str, blueprint_path: str = "research-blueprint.json"
) -> dict[str, Any]:
    """Freeze a Research Graph blueprint and initialize its node DAG, evidence ledgers, and asset registry."""
    project = _project_path(project_id)
    if (project / "research-graph.json").exists():
        raise RuntimeError("research graph already initialized; use resume_research_graph")
    blueprint = _relative_path(project, blueprint_path)
    if not blueprint.is_file():
        raise FileNotFoundError(blueprint_path)
    result = await asyncio.to_thread(
        _run_sync,
        [sys.executable, str(RESEARCH_GRAPH), "init", str(project), str(blueprint)],
        project,
        120,
    )
    return {"project_id": project_id, "initialized": result["exit_code"] == 0, **_command_result(result)}


@mcp.tool()
async def claim_research_node(
    project_id: str,
    worker_id: str,
    node_id: str | None = None,
    lease_seconds: float = 300,
) -> dict[str, Any]:
    """Atomically claim one ready Research Graph node and return its statement, dependencies, and lease token."""
    if not worker_id.strip():
        raise ValueError("worker_id is required")
    if lease_seconds <= 0 or lease_seconds > 86400:
        raise ValueError("lease_seconds must be between 0 and 86400")
    project = _project_path(project_id)
    command = [
        sys.executable, str(RESEARCH_GRAPH), "claim", str(project),
        "--worker", worker_id, "--lease-seconds", str(lease_seconds),
    ]
    if node_id:
        command.extend(["--node", node_id])
    result = await asyncio.to_thread(_run_sync, command, project, 120)
    parsed = _parse_last_json(result.get("stdout", ""))
    no_work = result["exit_code"] == 2 and isinstance(parsed, dict) and parsed.get("claimed") is False
    return {
        "project_id": project_id,
        "claimed": bool(result["exit_code"] == 0 and isinstance(parsed, dict)),
        "no_ready_node": no_work,
        **_command_result(result),
    }


@mcp.tool()
async def heartbeat_research_node(
    project_id: str,
    node_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: float = 300,
) -> dict[str, Any]:
    """Extend an active Research Graph lease while an Agent continues work on that node."""
    if lease_seconds <= 0 or lease_seconds > 86400:
        raise ValueError("lease_seconds must be between 0 and 86400")
    project = _project_path(project_id)
    command = [
        sys.executable, str(RESEARCH_GRAPH), "heartbeat", str(project), node_id,
        "--worker", worker_id, "--token", lease_token,
        "--lease-seconds", str(lease_seconds),
    ]
    result = await asyncio.to_thread(_run_sync, command, project, 120)
    return {"project_id": project_id, "extended": result["exit_code"] == 0, **_command_result(result)}


@mcp.tool()
async def submit_research_node(
    project_id: str,
    node_id: str,
    worker_id: str,
    lease_token: str,
    contributor_kind: str,
    contributor_id: str,
    artifact_content: str | None = None,
    artifact_base64: str | None = None,
    artifact_sha256: str | None = None,
    model: str | None = None,
    roles: list[str] | None = None,
    source_ids: list[str] | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """Store a node artifact immutably, run its frozen verifier, and record node-level contribution credit."""
    if (artifact_content is None) == (artifact_base64 is None):
        raise ValueError("provide exactly one of artifact_content or artifact_base64")
    if contributor_kind not in {"human", "agent", "model", "tool"}:
        raise ValueError("contributor_kind must be human, agent, model, or tool")
    if not contributor_id.strip():
        raise ValueError("contributor_id is required")
    if cost_usd is not None and cost_usd < 0:
        raise ValueError("cost_usd cannot be negative")
    if artifact_content is not None:
        data = artifact_content.encode("utf-8")
        suffix = ".txt"
    else:
        try:
            data = base64.b64decode(artifact_base64 or "", validate=True)
        except Exception as exc:
            raise ValueError("artifact_base64 is invalid") from exc
        suffix = ".bin"
    digest = _sha256_bytes(data)
    if artifact_sha256 and not secrets.compare_digest(artifact_sha256.lower(), digest):
        raise ValueError("artifact_sha256 mismatch")
    project = _project_path(project_id)
    artifact, _ = _write_runtime_payload(project, "research-submissions", suffix, data)
    command = [
        sys.executable, str(RESEARCH_GRAPH), "execute", str(project), node_id, str(artifact),
        "--worker", worker_id, "--token", lease_token,
        "--contributor-kind", contributor_kind, "--contributor-id", contributor_id,
    ]
    if model:
        command.extend(["--model", model])
    for role in roles or []:
        command.extend(["--role", role])
    if cost_usd is not None:
        command.extend(["--cost-usd", str(cost_usd)])
    for source_id in source_ids or []:
        command.extend(["--source-id", source_id])
    result = await asyncio.to_thread(_run_sync, command, project, 900)
    return {
        "project_id": project_id,
        "artifact_sha256": digest,
        "verified": result["exit_code"] == 0,
        **_command_result(result),
    }


@mcp.tool()
async def fail_research_node(
    project_id: str,
    node_id: str,
    worker_id: str,
    lease_token: str,
    failure_class: str,
    reason: str,
) -> dict[str, Any]:
    """Record an Agent-reported node failure so downstream work becomes stale and can drive graph revision."""
    project = _project_path(project_id)
    command = [
        sys.executable, str(RESEARCH_GRAPH), "fail", str(project), node_id,
        "--worker", worker_id, "--token", lease_token,
        "--failure-class", failure_class, "--reason", reason,
    ]
    result = await asyncio.to_thread(_run_sync, command, project, 120)
    return {"project_id": project_id, "recorded": result["exit_code"] == 0, **_command_result(result)}


@mcp.tool()
async def reuse_research_asset(
    project_id: str,
    node_id: str,
    asset_id: str,
    contributor_kind: str,
    contributor_id: str,
    model: str | None = None,
    roles: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Reuse a contract-compatible verified asset on a ready node and record its provenance and credit."""
    if contributor_kind not in {"human", "agent", "model", "tool"}:
        raise ValueError("contributor_kind must be human, agent, model, or tool")
    project = _project_path(project_id)
    command = [
        sys.executable, str(RESEARCH_GRAPH), "reuse", str(project), node_id, asset_id,
        "--contributor-kind", contributor_kind, "--contributor-id", contributor_id,
    ]
    if model:
        command.extend(["--model", model])
    for role in roles or []:
        command.extend(["--role", role])
    for source_id in source_ids or []:
        command.extend(["--source-id", source_id])
    result = await asyncio.to_thread(_run_sync, command, project, 120)
    return {"project_id": project_id, "reused": result["exit_code"] == 0, **_command_result(result)}


@mcp.tool()
async def revise_research_graph(project_id: str, revision: dict[str, Any]) -> dict[str, Any]:
    """Apply a versioned split/rewire/supersede/invalidate revision after node failures are recorded."""
    if not isinstance(revision, dict):
        raise ValueError("revision must be an object")
    project = _project_path(project_id)
    payload = (json.dumps(revision, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    revision_path, digest = _write_runtime_payload(project, "incoming-revisions", ".json", payload)
    result = await asyncio.to_thread(
        _run_sync,
        [sys.executable, str(RESEARCH_GRAPH), "revise", str(project), str(revision_path)],
        project,
        120,
    )
    return {
        "project_id": project_id,
        "revision_sha256": digest,
        "revised": result["exit_code"] == 0,
        **_command_result(result),
    }


@mcp.tool()
async def resume_research_graph(project_id: str) -> dict[str, Any]:
    """Reconcile expired leases and incomplete verifier attempts, then refresh the ready frontier."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(RESEARCH_GRAPH), "resume", str(project)], project, 120
    )
    return {"project_id": project_id, "resumed": result["exit_code"] == 0, **_command_result(result)}


@mcp.tool()
async def get_research_graph_status(project_id: str) -> dict[str, Any]:
    """Return node counts, ready frontier, leases, goal states, and Research Graph acceptance."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(RESEARCH_GRAPH), "status", str(project)], project, 120
    )
    return {"project_id": project_id, **_command_result(result)}


@mcp.tool()
async def audit_research_graph(project_id: str) -> dict[str, Any]:
    """Independently verify the Research Graph, attempts, assets, event chain, contribution chain, and acceptance."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(RESEARCH_GRAPH), "audit", str(project)], project, 180
    )
    return {"project_id": project_id, **_command_result(result)}


async def _composition_command(project: Path, args: list[str], timeout: int = 120) -> dict[str, Any]:
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(COMPOSITION_GRAPH), *args], project, timeout)
    return _command_result(result)


@mcp.tool()
async def initialize_composition_graph(project_id: str, blueprint_path: str = "composition-blueprint.json") -> dict[str, Any]:
    """Freeze immutable engineering claims and verifiers for an evidence-backed AND-OR composition graph."""
    project = _project_path(project_id)
    blueprint = _relative_path(project, blueprint_path)
    if not blueprint.is_file():
        raise FileNotFoundError(blueprint_path)
    result = await _composition_command(project, ["init", str(project), str(blueprint)])
    return {"project_id": project_id, "initialized": result["exit_code"] == 0, **result}


@mcp.tool()
async def submit_composition(project_id: str, submission: dict[str, Any], artifact_content: str | None = None) -> dict[str, Any]:
    """Execute frozen direct, conditional reduction, bound integration/holdout, or counterexample checks.

    A successful command is NOT necessarily an accepted goal; inspect the returned graph states.
    Submission kinds and required bindings are documented in references/composition-graph.md.
    """
    project = _project_path(project_id)
    request = dict(submission)
    if artifact_content is not None:
        if request.get("artifact") is not None:
            raise ValueError("provide artifact_content or a project artifact path, not both")
        artifact, _ = _write_runtime_payload(project, "composition-incoming", ".txt", artifact_content.encode("utf-8"))
        request["artifact"] = str(artifact)
    elif "artifact" in request:
        request["artifact"] = str(_relative_path(project, request["artifact"]))
    payload, _ = _write_runtime_payload(project, "composition-incoming", ".json",
        (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"project_id": project_id, **await _composition_command(project, ["submit", str(project), str(payload)], 900)}


@mcp.tool()
async def get_composition_status(project_id: str) -> dict[str, Any]:
    """Derive claim and alternative-path states from evidence; conditional checks alone never accept engineering goals."""
    project = _project_path(project_id)
    return {"project_id": project_id, **await _composition_command(project, ["status", str(project)])}


@mcp.tool()
async def audit_composition_graph(project_id: str) -> dict[str, Any]:
    """Reconcile immutable claims, event records, artifact bindings, verification outputs, and goal acceptance."""
    project = _project_path(project_id)
    return {"project_id": project_id, **await _composition_command(project, ["audit", str(project)], 180)}


@mcp.tool()
async def resume_composition_graph(project_id: str) -> dict[str, Any]:
    """Recover interrupted verification attempts without erasing evidence or inventing a successful terminal result."""
    project = _project_path(project_id)
    return {"project_id": project_id, **await _composition_command(project, ["resume", str(project)])}


@mcp.tool()
async def revoke_composition_evidence(project_id: str, evidence_id: str, reason: str) -> dict[str, Any]:
    """Retract evidence and invalidate only its dependent paths; preserve alternative paths and original records."""
    if not reason.strip():
        raise ValueError("reason is required")
    project = _project_path(project_id)
    return {"project_id": project_id, **await _composition_command(project, ["revoke", str(project), evidence_id, "--reason", reason])}


@mcp.tool()
async def export_composition_asset(project_id: str, claim_id: str) -> dict[str, Any]:
    """Export an accepted claim's evidence bundle for compatible-target revalidation, without granting automatic trust."""
    project = _project_path(project_id)
    destination = project / "composition-exports" / (uuid.uuid4().hex + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _chown_tree(destination.parent)
    result = await _composition_command(project, ["export", str(project), claim_id, str(destination)], 180)
    return {"project_id": project_id, "path": destination.relative_to(project).as_posix(), **result}


@mcp.tool()
async def import_composition_asset(project_id: str, claim_id: str, asset_json: str) -> dict[str, Any]:
    """Import a bundle by verifying its exact contract/environment and running the target verifier; labels are not trusted."""
    project = _project_path(project_id)
    payload = json.loads(asset_json)
    if not isinstance(payload, dict) or not isinstance(payload.get("source_project"), str):
        raise ValueError("asset must name a source_project")
    source = Path(payload["source_project"]).resolve()
    if not _is_under(source, [PROJECTS_ROOT.resolve(), *_allowed_local_roots()]):
        raise PermissionError("asset source is outside configured project roots")
    asset, _ = _write_runtime_payload(project, "composition-imports", ".json", asset_json.encode("utf-8"))
    return {"project_id": project_id, **await _composition_command(project, ["import", str(project), claim_id, str(asset)], 900)}


@mcp.tool()
async def search_composition_assets(project_id: str, query: str) -> dict[str, Any]:
    """Find exported asset candidates by text; retrieval similarity never authorizes reuse."""
    project = _project_path(project_id)
    root = project / "composition-exports"
    root.mkdir(exist_ok=True)
    _chown_tree(root)
    return {"project_id": project_id, **await _composition_command(project, ["search", str(root), query])}


@mcp.tool()
async def record_composition_readback(project_id: str, claim_id: str, text: str, author: str,
                                      source_blind_declared: bool = False) -> dict[str, Any]:
    """Record a claimed blind read-back, without asserting that input isolation or reviewer identity was verified."""
    if not source_blind_declared:
        raise ValueError("source_blind_declared must be explicit; do not invent independence")
    project = _project_path(project_id)
    path, _ = _write_runtime_payload(project, "composition-readbacks", ".txt", text.encode("utf-8"))
    return {"project_id": project_id, **await _composition_command(project,
        ["readback", str(project), claim_id, str(path), "--author", author, "--source-blind-declared"])}


@mcp.tool()
async def attest_composition_milestone(project_id: str, claim_id: str, readback_id: str,
                                      reviewer: str, decision: str) -> dict[str, Any]:
    """Record an explicitly supplied human review decision; agent must not invent approval. Identity is not authenticated here."""
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    project = _project_path(project_id)
    return {"project_id": project_id, **await _composition_command(project,
        ["attest", str(project), claim_id, readback_id, "--reviewer", reviewer, "--decision", decision])}


@mcp.tool()
async def validate_scientist_project(project_id: str) -> dict[str, Any]:
    """Validate the real Scientist Runtime plan, command stages, frozen inputs, budgets, and acceptance rules."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(SCIENTIST_RUNTIME), "validate", str(project)], project, 120
    )
    return {"project_id": project_id, "valid": result["exit_code"] == 0, **_command_result(result)}


@mcp.tool()
def start_scientist_study(project_id: str) -> dict[str, Any]:
    """Start the command-backed ideation, literature, experiment, analysis, and review pipeline in the background."""
    project = _project_path(project_id)
    if (project / "scientist-state.json").exists():
        raise RuntimeError("scientist-state.json already exists; use resume_scientist_study")
    return {"project_id": project_id, **_launch_job(project, "scientist-start")}


@mcp.tool()
def resume_scientist_study(project_id: str) -> dict[str, Any]:
    """Resume a Scientist Runtime study from its immutable stage attempts without repeating completed stages."""
    project = _project_path(project_id)
    if not (project / "scientist-state.json").exists():
        raise RuntimeError("scientist study has not started; use start_scientist_study")
    return {"project_id": project_id, **_launch_job(project, "scientist-resume")}


@mcp.tool()
def get_scientist_status(project_id: str, job_id: str | None = None) -> dict[str, Any]:
    """Return the Scientist Runtime stage state plus background worker status and logs."""
    project = _project_path(project_id)
    return {"project_id": project_id, **_job_status(project, job_id)}


@mcp.tool()
async def audit_scientist_project(project_id: str) -> dict[str, Any]:
    """Audit stage attempts, hashes, ledgers, claim-evidence links, review decisions, and acceptance."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(SCIENTIST_RUNTIME), "audit", str(project)], project, 180
    )
    return {"project_id": project_id, **_command_result(result)}


@mcp.tool()
async def export_scientist_report(project_id: str) -> dict[str, Any]:
    """Export a report derived from the recorded Scientist Runtime evidence and decisions."""
    project = _project_path(project_id)
    result = await asyncio.to_thread(
        _run_sync, [sys.executable, str(SCIENTIST_RUNTIME), "export", str(project)], project, 180
    )
    return {"project_id": project_id, **_command_result(result)}


class BearerAuthMiddleware:
    def __init__(self, app: Any, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        if not self.token:
            response = JSONResponse({"error": "server authentication is not configured"}, status_code=503)
            await response(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        provided = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self.token}"
        if not secrets.compare_digest(provided, expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
_mcp_app = mcp.streamable_http_app(
    host="0.0.0.0",
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    max_request_body_size=4 * 1024 * 1024,
    transport_security=security,
)
app = BearerAuthMiddleware(_mcp_app, os.getenv("MCP_AUTH_TOKEN"))

