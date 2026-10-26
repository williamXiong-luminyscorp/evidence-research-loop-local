#!/usr/bin/env python3
"""Execute and audit a versioned claim-task-artifact-evidence research graph.

The graph is deliberately domain-neutral.  A node may represent a theorem,
software component, experiment, business rule, or human review obligation.  A
domain adapter supplies the verifier command; this module supplies dependency
tracking, work leases, recovery, provenance, reusable verified assets, and
blueprint revision.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
TERMINAL_NODE_STATES = {"verified", "rejected", "superseded"}
WORKABLE_STATES = {"ready"}
SHARED_VOLUME_PERMISSIONS = os.getenv(
    "ERL_SHARED_VOLUME_PERMISSIONS", ""
).lower() in {"1", "true", "yes"}


class GraphError(RuntimeError):
    """Raised when a graph operation violates an invariant."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    """Copy bytes without metadata on shared filesystems that reject copystat."""
    if SHARED_VOLUME_PERMISSIONS:
        shutil.copyfile(source, destination)
    else:
        shutil.copy2(source, destination)


def token_fingerprint(token: str) -> str:
    """Return a non-secret correlation value for lease audit events."""
    return sha256_bytes(token.encode("utf-8"))[:16]


def verifier_contract_payload(verifier: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(verifier)
    value.pop("contract_hash", None)
    return value


def verifier_contract_hash(verifier: dict[str, Any]) -> str:
    return sha256_bytes(canonical(verifier_contract_payload(verifier)))


def _resolve_frozen_file(raw: str, bases: list[Path], required: bool) -> Path | None:
    if "{" in raw or "}" in raw:
        if required:
            raise GraphError(f"frozen verifier file cannot contain a placeholder: {raw}")
        return None
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    candidates = [path] if path.is_absolute() else [base / path for base in bases]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    if required:
        raise GraphError(f"frozen verifier file does not exist: {raw}")
    return None


def freeze_verifier(verifier: dict[str, Any], source_dir: Path,
                    project: Path) -> dict[str, Any]:
    """Normalize and hash command files before a graph can execute them.

    Command arguments that resolve to local files are discovered automatically.
    Adapters may list imported scripts/configuration in ``frozen_files``.  The
    stored manifest is part of the verifier contract and is checked before each
    execution and again by audit.
    """
    frozen = copy.deepcopy(verifier)
    frozen.pop("frozen_file_manifest", None)
    frozen.pop("contract_hash", None)
    bases = [source_dir.resolve(), project.resolve()]
    command = list(frozen.get("command", []))
    discovered: dict[str, dict[str, Any]] = {}
    for index, part in enumerate(command):
        resolved = _resolve_frozen_file(part, bases, False)
        if resolved is None and index == 0:
            executable = shutil.which(part)
            if executable:
                resolved = Path(executable).resolve()
        if resolved is None:
            continue
        command[index] = str(resolved)
        discovered[str(resolved)] = {"path": str(resolved), "source": "command"}
    explicit: list[str] = []
    for value in frozen.get("frozen_files", []):
        if not isinstance(value, str) or not value:
            raise GraphError("verifier.frozen_files must contain non-empty paths")
        resolved = _resolve_frozen_file(value, bases, True)
        assert resolved is not None
        explicit.append(str(resolved))
        discovered[str(resolved)] = {"path": str(resolved), "source": "explicit"}
    frozen["command"] = command
    frozen["frozen_files"] = explicit
    manifest: list[dict[str, Any]] = []
    for path_text in sorted(discovered):
        path = Path(path_text)
        manifest.append({
            **discovered[path_text],
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        })
    frozen["frozen_file_manifest"] = manifest
    frozen["contract_hash"] = verifier_contract_hash(frozen)
    return frozen


def freeze_blueprint_verifiers(blueprint: dict[str, Any], source_dir: Path,
                               project: Path) -> None:
    for node in blueprint.get("nodes", []):
        verifier = node.get("verifier")
        if verifier:
            node["verifier"] = freeze_verifier(verifier, source_dir, project)


def verifier_integrity_errors(verifier: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_contract = verifier.get("contract_hash")
    actual_contract = verifier_contract_hash(verifier)
    if expected_contract != actual_contract:
        errors.append("verifier contract hash mismatch")
    manifest = verifier.get("frozen_file_manifest", [])
    if not isinstance(manifest, list):
        return errors + ["verifier frozen-file manifest is invalid"]
    for entry in manifest:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            errors.append("verifier frozen-file manifest entry is invalid")
            continue
        path = Path(entry["path"])
        if not path.is_file():
            errors.append(f"frozen verifier file missing: {path}")
            continue
        if sha256_file(path) != entry.get("sha256"):
            errors.append(f"frozen verifier file hash mismatch: {path}")
        if path.stat().st_size != entry.get("size_bytes"):
            errors.append(f"frozen verifier file size mismatch: {path}")
    return errors


def require_verifier_integrity(node: dict[str, Any]) -> None:
    verifier = node.get("verifier")
    if not verifier:
        return
    errors = verifier_integrity_errors(verifier)
    if errors:
        raise GraphError(f"node {node['id']} verifier integrity failure: " + "; ".join(errors))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # OneDrive and antivirus scanners can briefly hold a just-written JSON file
    # on Windows.  Keep the atomic replace invariant while tolerating that
    # transient sharing violation; never fall back to an in-place write.
    for attempt in range(20):
        try:
            os.replace(temp, path)
            break
        except PermissionError:
            if attempt == 19:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.025 * (attempt + 1))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise GraphError(f"invalid JSONL at {path}:{number}: {exc}") from exc
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def graph_path(project: Path) -> Path:
    return project / "research-graph.json"


def events_path(project: Path) -> Path:
    return project / "graph-events.jsonl"


def credits_path(project: Path) -> Path:
    return project / "contributions.jsonl"


def registry_path(project: Path) -> Path:
    return project / "verified-assets.json"


def attempt_path(project: Path, attempt_id: str) -> Path:
    return project / "attempts" / attempt_id / "attempt.json"


@contextlib.contextmanager
def project_lock(project: Path, timeout_sec: float = 15.0) -> Iterator[None]:
    """Acquire a small cross-platform lock using atomic file creation."""
    project.mkdir(parents=True, exist_ok=True)
    lock = project / ".research-graph.lock"
    deadline = time.monotonic() + timeout_sec
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            os.fsync(fd)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 120:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise GraphError(f"timed out acquiring graph lock: {lock}")
            time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lock.unlink(missing_ok=True)


def append_chain(path: Path, payload: dict[str, Any], prefix: str) -> dict[str, Any]:
    rows = read_jsonl(path)
    previous = rows[-1][f"{prefix}_hash"] if rows else None
    row = {**payload, f"previous_{prefix}_hash": previous}
    row[f"{prefix}_hash"] = sha256_bytes(canonical(row))
    append_jsonl(path, row)
    return row


def append_event(project: Path, event_type: str, **fields: Any) -> dict[str, Any]:
    return append_chain(events_path(project), {
        "event_id": f"evt-{uuid.uuid4().hex}",
        "at": utc_now(),
        "type": event_type,
        **fields,
    }, "event")


def contribution_exists(project: Path, contribution_id: str) -> bool:
    return any(row.get("contribution_id") == contribution_id
               for row in read_jsonl(credits_path(project)))


def append_contribution(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    contribution_id = payload["contribution_id"]
    for row in read_jsonl(credits_path(project)):
        if row.get("contribution_id") == contribution_id:
            return row
    return append_chain(credits_path(project), {"at": utc_now(), **payload}, "credit")


def event_exists(project: Path, event_type: str, key: str, value: Any) -> bool:
    return any(row.get("type") == event_type and row.get(key) == value
               for row in read_jsonl(events_path(project)))


def node_spec(node: dict[str, Any]) -> dict[str, Any]:
    runtime = {"status", "lease", "attempt_ids", "active_asset_id", "failure",
               "verified_at", "updated_at", "invalidated_by_revision"}
    return {key: copy.deepcopy(value) for key, value in node.items() if key not in runtime}


def blueprint_from_graph(graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph["graph_id"],
        "title": graph["title"],
        "goal_node_ids": graph["goal_node_ids"],
        "nodes": [node_spec(graph["nodes"][key]) for key in sorted(graph["nodes"])],
    }


def blueprint_hash(blueprint: dict[str, Any]) -> str:
    return sha256_bytes(canonical(blueprint))


def validate_blueprint(blueprint: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if blueprint.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(blueprint.get("graph_id"), str) or not blueprint.get("graph_id"):
        errors.append("graph_id must be a non-empty string")
    if not isinstance(blueprint.get("title"), str) or not blueprint.get("title"):
        errors.append("title must be a non-empty string")
    nodes = blueprint.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes must be a non-empty array")
        return errors
    by_id: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{index}] must be an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or any(ch.isspace() for ch in node_id):
            errors.append(f"nodes[{index}].id must be a non-empty whitespace-free string")
            continue
        if node_id in by_id:
            errors.append(f"duplicate node id: {node_id}")
        by_id[node_id] = node
        if not isinstance(node.get("type"), str) or not node.get("type"):
            errors.append(f"node {node_id}: type must be a non-empty string")
        if not isinstance(node.get("statement"), str) or not node.get("statement"):
            errors.append(f"node {node_id}: statement must be a non-empty string")
        if not isinstance(node.get("dependencies", []), list):
            errors.append(f"node {node_id}: dependencies must be an array")
        if not isinstance(node.get("output_contract", {}), dict):
            errors.append(f"node {node_id}: output_contract must be an object")
        verifier = node.get("verifier")
        if verifier is not None:
            if not isinstance(verifier, dict):
                errors.append(f"node {node_id}: verifier must be an object")
            elif not isinstance(verifier.get("command"), list) or not all(
                    isinstance(part, str) for part in verifier.get("command", [])):
                errors.append(f"node {node_id}: verifier.command must be a string array")
            else:
                if not isinstance(verifier.get("frozen_files", []), list) or not all(
                        isinstance(part, str) for part in verifier.get("frozen_files", [])):
                    errors.append(f"node {node_id}: verifier.frozen_files must be a string array")
                if not isinstance(verifier.get("env_allowlist", []), list) or not all(
                        isinstance(part, str) for part in verifier.get("env_allowlist", [])):
                    errors.append(f"node {node_id}: verifier.env_allowlist must be a string array")
    for node_id, node in by_id.items():
        for dependency in node.get("dependencies", []):
            if dependency == node_id:
                errors.append(f"node {node_id}: self dependency")
            elif dependency not in by_id:
                errors.append(f"node {node_id}: missing dependency {dependency}")
    goals = blueprint.get("goal_node_ids")
    if not isinstance(goals, list) or not goals:
        errors.append("goal_node_ids must be a non-empty array")
    else:
        for goal in goals:
            if goal not in by_id:
                errors.append(f"unknown goal node: {goal}")
    indegree = {key: 0 for key in by_id}
    children = {key: [] for key in by_id}
    for node_id, node in by_id.items():
        for dependency in node.get("dependencies", []):
            if dependency in by_id:
                indegree[node_id] += 1
                children[dependency].append(node_id)
    queue = [key for key, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(by_id):
        errors.append("dependency graph contains a cycle")
    return errors


def require_graph(project: Path) -> dict[str, Any]:
    path = graph_path(project)
    if not path.is_file():
        raise GraphError(f"research graph not initialized: {path}")
    return load_json(path)


def initial_node(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        **copy.deepcopy(spec),
        "dependencies": list(spec.get("dependencies", [])),
        "scope": spec.get("scope", "project"),
        "priority": int(spec.get("priority", 0)),
        "max_attempts": int(spec.get("max_attempts", 3)),
        "status": "blocked",
        "lease": None,
        "attempt_ids": [],
        "active_asset_id": None,
        "failure": None,
        "verified_at": None,
        "updated_at": utc_now(),
        "invalidated_by_revision": None,
    }


def refresh_ready(graph: dict[str, Any]) -> None:
    nodes = graph["nodes"]
    changed = True
    while changed:
        changed = False
        for node in nodes.values():
            if node["status"] not in {"blocked", "stale"}:
                continue
            if all(nodes[dependency]["status"] == "verified"
                   for dependency in node["dependencies"]):
                node["status"] = "ready"
                node["updated_at"] = utc_now()
                changed = True


def descendants(graph: dict[str, Any], node_ids: set[str]) -> set[str]:
    result: set[str] = set()
    frontier = list(node_ids)
    while frontier:
        parent = frontier.pop()
        for node_id, node in graph["nodes"].items():
            if parent in node["dependencies"] and node_id not in result:
                result.add(node_id)
                frontier.append(node_id)
    return result


def invalidate_nodes(graph: dict[str, Any], node_ids: set[str], revision: int,
                     include_descendants: bool = True) -> set[str]:
    affected = set(node_ids)
    if include_descendants:
        affected |= descendants(graph, node_ids)
    for node_id in affected:
        node = graph["nodes"][node_id]
        if node["status"] == "superseded":
            continue
        node["status"] = "stale"
        node["lease"] = None
        node["active_asset_id"] = None
        node["invalidated_by_revision"] = revision
        node["updated_at"] = utc_now()
    return affected


def initialize(project: Path, blueprint_file: Path) -> dict[str, Any]:
    project = project.resolve()
    blueprint_file = blueprint_file.resolve()
    blueprint = load_json(blueprint_file)
    errors = validate_blueprint(blueprint)
    if errors:
        raise GraphError("invalid blueprint:\n- " + "\n- ".join(errors))
    freeze_blueprint_verifiers(blueprint, blueprint_file.parent, project)
    errors = validate_blueprint(blueprint)
    if errors:
        raise GraphError("invalid frozen blueprint:\n- " + "\n- ".join(errors))
    with project_lock(project):
        if graph_path(project).exists():
            raise GraphError(f"research graph already exists: {graph_path(project)}")
        for directory in ("attempts", "verified-assets/files", "blueprint-revisions"):
            (project / directory).mkdir(parents=True, exist_ok=True)
        graph = {
            "schema_version": SCHEMA_VERSION,
            "graph_id": blueprint["graph_id"],
            "title": blueprint["title"],
            "blueprint_revision": 1,
            "blueprint_hash": None,
            "goal_node_ids": list(blueprint["goal_node_ids"]),
            "nodes": {node["id"]: initial_node(node) for node in blueprint["nodes"]},
            "attempt_outcomes": {},
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        refresh_ready(graph)
        normalized_blueprint = blueprint_from_graph(graph)
        graph["blueprint_hash"] = blueprint_hash(normalized_blueprint)
        save_json(graph_path(project), graph)
        save_json(project / "blueprint-revisions" / "revision-0001.json", normalized_blueprint)
        save_json(registry_path(project), {"schema_version": 1, "assets": {}})
        append_event(project, "graph_initialized", graph_id=graph["graph_id"],
                     blueprint_revision=1, blueprint_hash=graph["blueprint_hash"],
                     node_count=len(graph["nodes"]))
    return graph


def lease_valid(project: Path, graph: dict[str, Any], node: dict[str, Any],
                worker: str, token: str) -> None:
    lease = node.get("lease")
    if not lease or lease.get("worker") != worker or lease.get("token") != token:
        raise GraphError(f"worker {worker!r} does not hold the lease for node {node['id']}")
    if float(lease.get("expires_epoch", 0)) <= time.time():
        recover_expired_locked(project, graph)
        refresh_ready(graph)
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
        raise GraphError(f"lease expired for node {node['id']}; work was recovered and must be reclaimed")


def interrupt_attempt(project: Path, graph: dict[str, Any], node: dict[str, Any],
                      attempt_id: str, reason: str) -> None:
    path = attempt_path(project, attempt_id)
    if path.is_file():
        attempt = load_json(path)
        if attempt.get("status") == "running":
            attempt.update({"status": "interrupted", "classification": "interrupted",
                            "reason": reason, "finished_at": utc_now(), "finalized": True})
            save_json(path, attempt)
    graph["attempt_outcomes"][attempt_id] = {
        "node_id": node["id"], "outcome": "interrupted", "at": utc_now()
    }
    node["lease"] = None
    if len(node["attempt_ids"]) >= node["max_attempts"]:
        node["status"] = "failed"
        node["failure"] = {"classification": "attempt_limit", "reason": reason, "at": utc_now()}
        affected = invalidate_nodes(graph, descendants(graph, {node["id"]}),
                                    graph["blueprint_revision"], False)
        append_event(project, "node_failed", node_id=node["id"], attempt_id=attempt_id,
                     failure_class="attempt_limit", stale_descendants=sorted(affected))
    else:
        node["status"] = "ready"
        append_event(project, "attempt_interrupted", node_id=node["id"],
                     attempt_id=attempt_id, reason=reason)
    node["updated_at"] = utc_now()


def recover_expired_locked(project: Path, graph: dict[str, Any]) -> None:
    now = time.time()
    for node in graph["nodes"].values():
        lease = node.get("lease")
        if not lease or float(lease["expires_epoch"]) > now:
            continue
        running = None
        for attempt_id in reversed(node["attempt_ids"]):
            path = attempt_path(project, attempt_id)
            if path.is_file() and load_json(path).get("status") == "running":
                running = attempt_id
                break
        if running:
            interrupt_attempt(project, graph, node, running, "lease expired before terminal result")
        else:
            node["lease"] = None
            node["status"] = "ready"
            node["updated_at"] = utc_now()
            append_event(project, "lease_expired", node_id=node["id"],
                         worker=lease["worker"],
                         lease_fingerprint=token_fingerprint(lease["token"]))


def claim(project: Path, worker: str, lease_seconds: float,
          requested_node: str | None = None) -> dict[str, Any] | None:
    project = project.resolve()
    with project_lock(project):
        graph = require_graph(project)
        recover_expired_locked(project, graph)
        refresh_ready(graph)
        eligible = [node for node in graph["nodes"].values()
                    if node["status"] in WORKABLE_STATES]
        if requested_node:
            eligible = [node for node in eligible if node["id"] == requested_node]
        eligible.sort(key=lambda node: (-int(node.get("priority", 0)), node["id"]))
        if not eligible:
            save_json(graph_path(project), graph)
            return None
        node = eligible[0]
        registry = load_json(registry_path(project))
        dependency_assets: list[dict[str, Any]] = []
        for dependency_id in node["dependencies"]:
            dependency = graph["nodes"][dependency_id]
            asset_id = dependency.get("active_asset_id")
            asset = registry.get("assets", {}).get(asset_id)
            if not asset:
                # A ready node should make this unreachable; fail closed rather
                # than leasing work without the inputs it is supposed to build on.
                raise GraphError(
                    f"verified dependency {dependency_id} has no registered active asset")
            dependency_assets.append({
                "node_id": dependency_id,
                "asset_id": asset_id,
                "artifact_path": asset["artifact_path"],
                "artifact_hash": asset["artifact_hash"],
                "output_contract_hash": asset["output_contract_hash"],
                "semantic_binding": copy.deepcopy(asset.get("semantic_binding")),
                "semantic_binding_hash": asset.get("semantic_binding_hash"),
            })
        token = uuid.uuid4().hex
        lease = {
            "worker": worker,
            "token": token,
            "claimed_at": utc_now(),
            "expires_epoch": time.time() + lease_seconds,
            "lease_seconds": lease_seconds,
        }
        node["lease"] = lease
        node["status"] = "leased"
        node["updated_at"] = utc_now()
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
        append_event(project, "node_leased", node_id=node["id"], worker=worker,
                     lease_fingerprint=token_fingerprint(token),
                     expires_epoch=lease["expires_epoch"])
        return {"node_id": node["id"], "statement": node["statement"],
                "dependencies": node["dependencies"], "lease": lease,
                "dependency_assets": dependency_assets,
                "blueprint_revision": graph["blueprint_revision"]}


def heartbeat(project: Path, node_id: str, worker: str, token: str,
              lease_seconds: float) -> dict[str, Any]:
    project = project.resolve()
    with project_lock(project):
        graph = require_graph(project)
        node = graph["nodes"].get(node_id)
        if not node:
            raise GraphError(f"unknown node: {node_id}")
        lease_valid(project, graph, node, worker, token)
        node["lease"]["expires_epoch"] = time.time() + lease_seconds
        node["lease"]["heartbeat_at"] = utc_now()
        node["updated_at"] = utc_now()
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
        append_event(project, "lease_heartbeat", node_id=node_id, worker=worker,
                     lease_fingerprint=token_fingerprint(token),
                     expires_epoch=node["lease"]["expires_epoch"])
        return copy.deepcopy(node["lease"])


def contributor(kind: str, contributor_id: str, model: str | None,
                roles: list[str] | None = None) -> dict[str, Any]:
    value = {"kind": kind, "id": contributor_id, "roles": roles or ["implementation"]}
    if model:
        value["model"] = model
    return value


def start_attempt(project: Path, node_id: str, worker: str, token: str,
                  artifact: Path, author: dict[str, Any], cost_usd: float | None = None,
                  source_ids: list[str] | None = None) -> dict[str, Any]:
    project = project.resolve()
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise GraphError(f"artifact must be a file: {artifact}")
    with project_lock(project):
        graph = require_graph(project)
        node = graph["nodes"].get(node_id)
        if not node:
            raise GraphError(f"unknown node: {node_id}")
        lease_valid(project, graph, node, worker, token)
        if node["status"] not in {"leased", "running"}:
            raise GraphError(f"node {node_id} is not leased: {node['status']}")
        require_verifier_integrity(node)
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        directory = project / "attempts" / attempt_id
        directory.mkdir(parents=True)
        stored = directory / f"artifact{artifact.suffix}"
        copy_file(artifact, stored)
        timeout = float(node.get("verifier", {}).get("timeout_sec", 300))
        node["lease"]["expires_epoch"] = max(
            float(node["lease"]["expires_epoch"]), time.time() + timeout + 30)
        attempt = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "node_id": node_id,
            "blueprint_revision": graph["blueprint_revision"],
            "worker": worker,
            "lease_fingerprint": token_fingerprint(token),
            "status": "running",
            "started_at": utc_now(),
            "artifact_path": stored.relative_to(project).as_posix(),
            "artifact_hash": sha256_file(stored),
            "contributor": author,
            "known_cost_usd": cost_usd,
            "source_ids": source_ids or [],
            "verifier_contract_hash": (node.get("verifier") or {}).get("contract_hash"),
            "verifier_frozen_files": copy.deepcopy(
                (node.get("verifier") or {}).get("frozen_file_manifest", [])),
            "finalized": False,
        }
        save_json(directory / "attempt.json", attempt)
        node["attempt_ids"].append(attempt_id)
        node["status"] = "running"
        node["updated_at"] = utc_now()
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
        append_event(project, "attempt_started", node_id=node_id, attempt_id=attempt_id,
                     worker=worker, artifact_hash=attempt["artifact_hash"],
                     blueprint_revision=graph["blueprint_revision"])
        return attempt


def expand_command(parts: list[str], project: Path, attempt: dict[str, Any],
                   result_dir: Path) -> list[str]:
    values = {
        "project": str(project),
        "artifact": str(project / attempt["artifact_path"]),
        "attempt_dir": str(attempt_path(project, attempt["attempt_id"]).parent),
        "result_dir": str(result_dir),
        "node_id": attempt["node_id"],
    }
    return [part.format(**values) for part in parts]


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def filtered_verifier_environment(allowlist: list[str] | None = None) -> dict[str, str]:
    """Build a minimal child environment instead of forwarding account secrets."""
    safe = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "LANG", "LC_ALL", "TZ",
    }
    safe.update(allowlist or [])
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        item.upper() for item in safe
    }}
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def run_verifier(command: list[str], cwd: Path, timeout_sec: float,
                 stdout_path: Path, stderr_path: Path,
                 env_allowlist: list[str] | None = None) -> dict[str, Any]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    environment = filtered_verifier_environment(env_allowlist)
    started = time.perf_counter()
    with stdout_path.open("w", encoding="utf-8") as stdout, \
            stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr,
                                   text=True, creationflags=creationflags,
                                   start_new_session=os.name != "nt", env=environment)
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process(process)
            exit_code = process.returncode
    return {"exit_code": exit_code, "timed_out": timed_out,
            "elapsed_sec": time.perf_counter() - started,
            "environment_keys": sorted(environment)}


def finish_attempt_record(project: Path, attempt_id: str,
                          execution: dict[str, Any]) -> dict[str, Any]:
    path = attempt_path(project, attempt_id)
    attempt = load_json(path)
    attempt.update(execution)
    attempt["finished_at"] = utc_now()
    attempt["finalized"] = False
    save_json(path, attempt)
    return attempt


def verifier_outcome(project: Path, node: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    verifier = node.get("verifier") or {}
    if attempt.get("timed_out"):
        return {"passed": False, "classification": "timeout", "reason": "verifier timed out"}
    if attempt.get("exit_code") != 0:
        return {"passed": False, "classification": "nonzero_exit",
                "reason": f"verifier exited with {attempt.get('exit_code')}"}
    result_name = verifier.get("result_file")
    result = None
    if result_name:
        result_path = attempt_path(project, attempt["attempt_id"]).parent / "results" / result_name
        if not result_path.is_file():
            return {"passed": False, "classification": "missing_output",
                    "reason": f"missing verifier result: {result_name}"}
        try:
            result = load_json(result_path)
        except (OSError, json.JSONDecodeError) as exc:
            return {"passed": False, "classification": "invalid_output", "reason": str(exc)}
        pass_field = verifier.get("pass_field", "hard_pass")
        if result.get(pass_field) is not True:
            return {"passed": False, "classification": "hard_gate_failed",
                    "reason": f"{pass_field} is not true", "result": result}
    return {"passed": True, "classification": "verified", "reason": "verifier accepted",
            "result": result}


def asset_contract_hash(node: dict[str, Any]) -> str:
    return sha256_bytes(canonical(node.get("output_contract", {})))


def node_semantic_binding(graph: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Describe the exact claim, inputs, and verifier an asset proves.

    Output shape alone is intentionally insufficient: two unrelated claims can
    both produce text or JSON.  Dependency asset identities bind the proof to
    the inputs that were actually verified.
    """
    dependency_assets: list[dict[str, Any]] = []
    for dependency_id in node.get("dependencies", []):
        dependency = graph["nodes"][dependency_id]
        dependency_assets.append({
            "node_id": dependency_id,
            "asset_id": dependency.get("active_asset_id"),
        })
    verifier = node.get("verifier") or {}
    return {
        "statement_hash": sha256_bytes(canonical(node.get("statement", ""))),
        "dependency_assets": dependency_assets,
        "output_contract_hash": asset_contract_hash(node),
        "verifier_contract_hash": verifier.get("contract_hash"),
    }


def semantic_binding_hash(binding: dict[str, Any]) -> str:
    return sha256_bytes(canonical(binding))


def ensure_finalization_records(project: Path, graph: dict[str, Any], node: dict[str, Any],
                                attempt: dict[str, Any], outcome: dict[str, Any],
                                asset: dict[str, Any] | None) -> None:
    attempt_id = attempt["attempt_id"]
    contribution_id = f"contrib-{attempt_id}"
    append_contribution(project, {
        "contribution_id": contribution_id,
        "graph_id": graph["graph_id"],
        "blueprint_revision": attempt["blueprint_revision"],
        "node_id": node["id"],
        "attempt_id": attempt_id,
        "contributor": attempt["contributor"],
        "source_ids": attempt.get("source_ids", []),
        "artifact_hash": attempt.get("artifact_hash"),
        "verifier_id": (node.get("verifier") or {}).get("id", "exit-code"),
        "outcome": "verified" if outcome["passed"] else "failed",
        "classification": outcome["classification"],
        "known_cost_usd": attempt.get("known_cost_usd"),
        "duration_sec": attempt.get("elapsed_sec"),
        "asset_id": asset.get("asset_id") if asset else None,
    })
    event_type = "node_verified" if outcome["passed"] else "node_failed"
    if not event_exists(project, event_type, "attempt_id", attempt_id):
        append_event(project, event_type, node_id=node["id"], attempt_id=attempt_id,
                     classification=outcome["classification"], reason=outcome["reason"],
                     asset_id=asset.get("asset_id") if asset else None)


def finalize_attempt(project: Path, attempt_id: str) -> dict[str, Any]:
    project = project.resolve()
    with project_lock(project):
        graph = require_graph(project)
        path = attempt_path(project, attempt_id)
        if not path.is_file():
            raise GraphError(f"unknown attempt: {attempt_id}")
        attempt = load_json(path)
        if attempt.get("status") not in {"verifier_finished", "verified", "failed"}:
            raise GraphError(f"attempt is not terminal: {attempt_id} ({attempt.get('status')})")
        node = graph["nodes"].get(attempt["node_id"])
        if not node:
            raise GraphError(f"attempt references missing node: {attempt['node_id']}")
        outcome = attempt.get("outcome") or verifier_outcome(project, node, attempt)
        attempt["outcome"] = outcome
        registry = load_json(registry_path(project))
        asset = None
        if outcome["passed"]:
            binding = node_semantic_binding(graph, node)
            binding_hash = semantic_binding_hash(binding)
            identity = sha256_bytes(canonical({
                "artifact_hash": attempt["artifact_hash"],
                "semantic_binding_hash": binding_hash,
                "scope": node.get("scope", "project"),
            }))
            asset_id = f"asset-{identity[:20]}"
            source = project / attempt["artifact_path"]
            destination = project / "verified-assets" / "files" / f"{asset_id}{source.suffix}"
            if not destination.exists():
                copy_file(source, destination)
            asset = {
                "asset_id": asset_id,
                "artifact_hash": attempt["artifact_hash"],
                "artifact_path": destination.relative_to(project).as_posix(),
                "output_contract_hash": asset_contract_hash(node),
                "semantic_binding": binding,
                "semantic_binding_hash": binding_hash,
                "scope": node.get("scope", "project"),
                "source_node_id": node["id"],
                "source_attempt_id": attempt_id,
                "verified_at": utc_now(),
                "verifier_id": (node.get("verifier") or {}).get("id", "exit-code"),
                "verifier_contract_hash": attempt.get("verifier_contract_hash"),
                "verifier_frozen_files": copy.deepcopy(
                    attempt.get("verifier_frozen_files", [])),
                "blueprint_revision": attempt["blueprint_revision"],
            }
            registry["assets"].setdefault(asset_id, asset)
            node["status"] = "verified"
            node["active_asset_id"] = asset_id
            node["verified_at"] = utc_now()
            node["failure"] = None
        else:
            node["status"] = "failed"
            node["active_asset_id"] = None
            node["failure"] = {"classification": outcome["classification"],
                               "reason": outcome["reason"], "at": utc_now(),
                               "attempt_id": attempt_id}
            invalidate_nodes(graph, descendants(graph, {node["id"]}),
                             graph["blueprint_revision"], False)
        node["lease"] = None
        node["updated_at"] = utc_now()
        graph["attempt_outcomes"][attempt_id] = {
            "node_id": node["id"], "outcome": "verified" if outcome["passed"] else "failed",
            "classification": outcome["classification"], "at": utc_now(),
            "asset_id": asset.get("asset_id") if asset else None,
        }
        refresh_ready(graph)
        graph["updated_at"] = utc_now()
        save_json(registry_path(project), registry)
        save_json(graph_path(project), graph)
        ensure_finalization_records(project, graph, node, attempt, outcome, asset)
        attempt["status"] = "verified" if outcome["passed"] else "failed"
        attempt["finalized"] = True
        save_json(path, attempt)
        return {"attempt": attempt, "outcome": outcome, "asset": asset}


def execute(project: Path, node_id: str, worker: str, token: str, artifact: Path,
            author: dict[str, Any], cost_usd: float | None = None,
            source_ids: list[str] | None = None) -> dict[str, Any]:
    project = project.resolve()
    attempt = start_attempt(project, node_id, worker, token, artifact, author,
                            cost_usd, source_ids)
    graph = require_graph(project)
    node = graph["nodes"][node_id]
    verifier = node.get("verifier")
    if not verifier:
        execution = {"status": "verifier_finished", "exit_code": None,
                     "timed_out": False, "elapsed_sec": 0.0,
                     "outcome": {"passed": False, "classification": "missing_verifier",
                                 "reason": "node has no verifier"}}
        finish_attempt_record(project, attempt["attempt_id"], execution)
        return finalize_attempt(project, attempt["attempt_id"])
    # Recheck immediately before spawning to narrow the hash-check/execute
    # window and reject a verifier edited after the attempt was registered.
    require_verifier_integrity(node)
    directory = attempt_path(project, attempt["attempt_id"]).parent
    result_dir = directory / "results"
    result_dir.mkdir()
    command = expand_command(verifier["command"], project, attempt, result_dir)
    execution = run_verifier(command, project, float(verifier.get("timeout_sec", 300)),
                             directory / "stdout.txt", directory / "stderr.txt",
                             verifier.get("env_allowlist", []))
    execution.update({"status": "verifier_finished", "command": command,
                      "verifier_id": verifier.get("id", "command")})
    attempt = finish_attempt_record(project, attempt["attempt_id"], execution)
    attempt["outcome"] = verifier_outcome(project, node, attempt)
    save_json(attempt_path(project, attempt["attempt_id"]), attempt)
    return finalize_attempt(project, attempt["attempt_id"])


def record_failure(project: Path, node_id: str, worker: str, token: str,
                   failure_class: str, reason: str) -> dict[str, Any]:
    project = project.resolve()
    with project_lock(project):
        graph = require_graph(project)
        node = graph["nodes"].get(node_id)
        if not node:
            raise GraphError(f"unknown node: {node_id}")
        lease_valid(project, graph, node, worker, token)
        node["status"] = "failed"
        node["lease"] = None
        node["failure"] = {"classification": failure_class, "reason": reason, "at": utc_now()}
        affected = invalidate_nodes(graph, descendants(graph, {node_id}),
                                    graph["blueprint_revision"], False)
        failure_id = f"failure-{uuid.uuid4().hex}"
        node["updated_at"] = utc_now()
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
        append_contribution(project, {
            "contribution_id": f"contrib-{failure_id}", "graph_id": graph["graph_id"],
            "blueprint_revision": graph["blueprint_revision"], "node_id": node_id,
            "attempt_id": failure_id,
            "contributor": {"kind": "agent", "id": worker, "roles": ["diagnosis"]},
            "source_ids": [], "artifact_hash": None, "verifier_id": "worker-report",
            "outcome": "failed", "classification": failure_class,
            "known_cost_usd": None, "duration_sec": None, "asset_id": None,
        })
        append_event(project, "node_failed", node_id=node_id, attempt_id=None,
                     failure_id=failure_id, classification=failure_class, reason=reason,
                     stale_descendants=sorted(affected))
        return {"node_id": node_id, "status": "failed", "failure_id": failure_id,
                "stale_descendants": sorted(affected)}


def reuse_asset(project: Path, node_id: str, asset_id: str,
                author: dict[str, Any], source_ids: list[str] | None = None) -> dict[str, Any]:
    project = project.resolve()
    with project_lock(project):
        graph = require_graph(project)
        refresh_ready(graph)
        node = graph["nodes"].get(node_id)
        if not node:
            raise GraphError(f"unknown node: {node_id}")
        if node["status"] != "ready":
            raise GraphError(f"node {node_id} is not ready: {node['status']}")
        registry = load_json(registry_path(project))
        asset = registry["assets"].get(asset_id)
        if not asset:
            raise GraphError(f"unknown verified asset: {asset_id}")
        if asset["output_contract_hash"] != asset_contract_hash(node):
            raise GraphError("asset output contract does not match target node")
        if asset["scope"] not in {node.get("scope", "project"), "portable"}:
            raise GraphError("asset scope is not compatible with target node")
        target_binding = node_semantic_binding(graph, node)
        if asset.get("semantic_binding_hash") != semantic_binding_hash(target_binding):
            raise GraphError(
                "asset semantic binding does not match target statement, inputs, and verifier")
        if asset.get("semantic_binding") != target_binding:
            raise GraphError("asset semantic binding record is inconsistent")
        for entry in asset.get("verifier_frozen_files", []):
            path = Path(entry.get("path", ""))
            if not path.is_file() or sha256_file(path) != entry.get("sha256"):
                raise GraphError(f"asset verifier evidence is no longer intact: {path}")
        reuse_id = f"reuse-{uuid.uuid4().hex}"
        node["status"] = "verified"
        node["active_asset_id"] = asset_id
        node["verified_at"] = utc_now()
        node["updated_at"] = utc_now()
        graph["attempt_outcomes"][reuse_id] = {
            "node_id": node_id, "outcome": "verified_by_reuse", "asset_id": asset_id,
            "at": utc_now(),
        }
        refresh_ready(graph)
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
        append_contribution(project, {
            "contribution_id": f"contrib-{reuse_id}", "graph_id": graph["graph_id"],
            "blueprint_revision": graph["blueprint_revision"], "node_id": node_id,
            "attempt_id": reuse_id, "contributor": author,
            "source_ids": source_ids or [asset_id], "artifact_hash": asset["artifact_hash"],
            "verifier_id": asset["verifier_id"], "outcome": "verified_by_reuse",
            "classification": "reused_verified_asset", "known_cost_usd": 0.0,
            "duration_sec": 0.0, "asset_id": asset_id,
        })
        append_event(project, "asset_reused", node_id=node_id, reuse_id=reuse_id,
                     asset_id=asset_id, source_node_id=asset["source_node_id"])
        return {"node_id": node_id, "status": "verified", "asset": asset,
                "reuse_id": reuse_id}


def revise(project: Path, revision_file: Path) -> dict[str, Any]:
    project = project.resolve()
    revision_file = revision_file.resolve()
    revision = load_json(revision_file)
    with project_lock(project):
        graph = require_graph(project)
        for existing_node in graph["nodes"].values():
            require_verifier_integrity(existing_node)
        current = graph["blueprint_revision"]
        if revision.get("expected_revision") != current:
            raise GraphError(f"revision expected {revision.get('expected_revision')}, current is {current}")
        if not isinstance(revision.get("reason"), str) or not revision["reason"]:
            raise GraphError("revision reason is required")
        next_revision = current + 1
        affected: set[str] = set()
        supersede_targets = {
            item["node_id"] for item in revision.get("supersede", [])
            if item.get("node_id") in graph["nodes"]
        }
        supersede_descendants = descendants(graph, supersede_targets)
        frozen_additions: list[dict[str, Any]] = []
        for original_spec in revision.get("add_nodes", []):
            spec = copy.deepcopy(original_spec)
            if spec.get("verifier"):
                spec["verifier"] = freeze_verifier(
                    spec["verifier"], revision_file.parent, project)
            node_id = spec.get("id")
            if node_id in graph["nodes"]:
                raise GraphError(f"revision adds existing node: {node_id}")
            graph["nodes"][node_id] = initial_node(spec)
            affected.add(node_id)
            frozen_additions.append(spec)
        revision["add_nodes"] = frozen_additions
        explicit_targets = {
            update["node_id"] for update in revision.get("dependency_updates", [])
        } | {
            item["node_id"] for item in revision.get("supersede", [])
        } | set(revision.get("invalidate", []))
        known_targets = explicit_targets & set(graph["nodes"])
        potentially_affected = known_targets | descendants(graph, known_targets)
        active = sorted(node_id for node_id in potentially_affected
                        if graph["nodes"][node_id]["status"] in {"leased", "running"})
        if active:
            raise GraphError(
                "cannot revise nodes with active work; finish, fail, or let leases expire: "
                + ", ".join(active))
        for update in revision.get("dependency_updates", []):
            node_id = update["node_id"]
            if node_id not in graph["nodes"]:
                raise GraphError(f"dependency update references missing node: {node_id}")
            graph["nodes"][node_id]["dependencies"] = list(update["dependencies"])
            affected |= invalidate_nodes(graph, {node_id}, next_revision, True)
        for item in revision.get("supersede", []):
            node_id = item["node_id"]
            if node_id not in graph["nodes"]:
                raise GraphError(f"supersede references missing node: {node_id}")
            graph["nodes"][node_id]["status"] = "superseded"
            graph["nodes"][node_id]["lease"] = None
            graph["nodes"][node_id]["active_asset_id"] = None
            graph["nodes"][node_id]["superseded_by"] = list(item.get("replacement_ids", []))
            affected.add(node_id)
        affected |= invalidate_nodes(graph, supersede_descendants, next_revision, False)
        requested_invalidations = set(revision.get("invalidate", []))
        unknown = requested_invalidations - set(graph["nodes"])
        if unknown:
            raise GraphError(f"invalidation references missing nodes: {sorted(unknown)}")
        affected |= invalidate_nodes(graph, requested_invalidations, next_revision, True)
        if "goal_node_ids" in revision:
            graph["goal_node_ids"] = list(revision["goal_node_ids"])
        blueprint = blueprint_from_graph(graph)
        errors = validate_blueprint(blueprint)
        if errors:
            raise GraphError("revised blueprint is invalid:\n- " + "\n- ".join(errors))
        graph["blueprint_revision"] = next_revision
        graph["blueprint_hash"] = blueprint_hash(blueprint)
        refresh_ready(graph)
        graph["updated_at"] = utc_now()
        save_json(project / "blueprint-revisions" / f"revision-{next_revision:04d}.json", blueprint)
        save_json(graph_path(project), graph)
        append_event(project, "blueprint_revised", blueprint_revision=next_revision,
                     blueprint_hash=graph["blueprint_hash"], reason=revision["reason"],
                     affected_node_ids=sorted(affected), revision_spec=revision)
        return {"blueprint_revision": next_revision, "blueprint_hash": graph["blueprint_hash"],
                "affected_node_ids": sorted(affected), "status": status(project)}


def resume(project: Path) -> dict[str, Any]:
    project = project.resolve()
    finalized: list[str] = []
    interrupted: list[str] = []
    for path in sorted((project / "attempts").glob("*/attempt.json")):
        attempt = load_json(path)
        if attempt.get("status") == "verifier_finished" and not attempt.get("finalized"):
            finalize_attempt(project, attempt["attempt_id"])
            finalized.append(attempt["attempt_id"])
    with project_lock(project):
        graph = require_graph(project)
        before = {node["id"]: node["status"] for node in graph["nodes"].values()}
        recover_expired_locked(project, graph)
        refresh_ready(graph)
        for node_id, old_status in before.items():
            if old_status == "running" and graph["nodes"][node_id]["status"] != "running":
                interrupted.append(node_id)
        graph["updated_at"] = utc_now()
        save_json(graph_path(project), graph)
    return {"finalized_attempt_ids": finalized, "interrupted_node_ids": interrupted,
            "status": status(project)}


def status(project: Path) -> dict[str, Any]:
    project = project.resolve()
    graph = require_graph(project)
    counts: dict[str, int] = {}
    for node in graph["nodes"].values():
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    goals = {goal: graph["nodes"][goal]["status"] for goal in graph["goal_node_ids"]}
    return {
        "graph_id": graph["graph_id"],
        "blueprint_revision": graph["blueprint_revision"],
        "blueprint_hash": graph["blueprint_hash"],
        "counts": counts,
        "ready_node_ids": sorted(node["id"] for node in graph["nodes"].values()
                                 if node["status"] == "ready"),
        "leased": {
            node["id"]: {
                **{key: value for key, value in node["lease"].items() if key != "token"},
                "lease_fingerprint": token_fingerprint(node["lease"]["token"]),
            }
            for node in graph["nodes"].values() if node.get("lease")
        },
        "goals": goals,
        "accepted": all(value == "verified" for value in goals.values()),
    }


def verify_chain(rows: list[dict[str, Any]], prefix: str) -> list[str]:
    errors: list[str] = []
    previous = None
    for index, row in enumerate(rows, 1):
        if row.get(f"previous_{prefix}_hash") != previous:
            errors.append(f"{prefix} chain previous hash mismatch at row {index}")
        recorded = row.get(f"{prefix}_hash")
        payload = dict(row)
        payload.pop(f"{prefix}_hash", None)
        actual = sha256_bytes(canonical(payload))
        if recorded != actual:
            errors.append(f"{prefix} hash mismatch at row {index}")
        previous = recorded
    return errors


def audit(project: Path) -> dict[str, Any]:
    project = project.resolve()
    errors: list[str] = []
    try:
        graph = require_graph(project)
    except (GraphError, OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "accepted": False, "errors": [str(exc)]}
    blueprint = blueprint_from_graph(graph)
    errors.extend(validate_blueprint(blueprint))
    if graph.get("blueprint_hash") != blueprint_hash(blueprint):
        errors.append("current blueprint hash mismatch")
    revision_path = project / "blueprint-revisions" / f"revision-{graph['blueprint_revision']:04d}.json"
    if not revision_path.is_file():
        errors.append("current blueprint revision snapshot missing")
    else:
        snapshot = load_json(revision_path)
        if blueprint_hash(snapshot) != graph.get("blueprint_hash"):
            errors.append("current blueprint revision snapshot does not match graph")
    try:
        events = read_jsonl(events_path(project))
        errors.extend(verify_chain(events, "event"))
    except GraphError as exc:
        errors.append(str(exc))
        events = []
    try:
        credits = read_jsonl(credits_path(project))
        errors.extend(verify_chain(credits, "credit"))
    except GraphError as exc:
        errors.append(str(exc))
        credits = []
    if not events or events[0].get("type") != "graph_initialized":
        errors.append("graph initialization event missing")
    for index, event in enumerate(events, 1):
        if "token" in event or "lease_token" in event:
            errors.append(f"raw lease token exposed in event row {index}")
    registry = load_json(registry_path(project)) if registry_path(project).is_file() else {"assets": {}}
    for asset_id, asset in registry.get("assets", {}).items():
        path = project / asset["artifact_path"]
        if not path.is_file():
            errors.append(f"asset file missing: {asset_id}")
        elif sha256_file(path) != asset["artifact_hash"]:
            errors.append(f"asset hash mismatch: {asset_id}")
        binding = asset.get("semantic_binding")
        if not isinstance(binding, dict):
            errors.append(f"asset semantic binding missing: {asset_id}")
        elif semantic_binding_hash(binding) != asset.get("semantic_binding_hash"):
            errors.append(f"asset semantic binding hash mismatch: {asset_id}")
        expected_identity = sha256_bytes(canonical({
            "artifact_hash": asset.get("artifact_hash"),
            "semantic_binding_hash": asset.get("semantic_binding_hash"),
            "scope": asset.get("scope"),
        }))
        if asset_id != f"asset-{expected_identity[:20]}":
            errors.append(f"asset identity mismatch: {asset_id}")
        for entry in asset.get("verifier_frozen_files", []):
            verifier_path = Path(entry.get("path", ""))
            if not verifier_path.is_file():
                errors.append(f"asset frozen verifier file missing: {asset_id}: {verifier_path}")
            elif sha256_file(verifier_path) != entry.get("sha256"):
                errors.append(f"asset frozen verifier hash mismatch: {asset_id}: {verifier_path}")
    for node_id, node in graph["nodes"].items():
        verifier = node.get("verifier")
        if verifier:
            errors.extend(f"node {node_id}: {error}"
                          for error in verifier_integrity_errors(verifier))
        if node["status"] == "verified":
            asset_id = node.get("active_asset_id")
            if not asset_id or asset_id not in registry.get("assets", {}):
                errors.append(f"verified node has no registered asset: {node_id}")
            else:
                asset = registry["assets"][asset_id]
                current_binding = node_semantic_binding(graph, node)
                if asset.get("semantic_binding_hash") != semantic_binding_hash(current_binding):
                    errors.append(f"verified node asset semantic mismatch: {node_id}")
        if node["status"] == "ready" and not all(
                graph["nodes"][dep]["status"] == "verified" for dep in node["dependencies"]):
            errors.append(f"ready node has unverified dependencies: {node_id}")
    for attempt_id, outcome in graph.get("attempt_outcomes", {}).items():
        if attempt_id.startswith("reuse-"):
            continue
        path = attempt_path(project, attempt_id)
        if not path.is_file():
            errors.append(f"attempt outcome has no record: {attempt_id}")
        else:
            attempt = load_json(path)
            if not attempt.get("finalized"):
                errors.append(f"attempt outcome is not finalized: {attempt_id}")
            if attempt.get("node_id") != outcome.get("node_id"):
                errors.append(f"attempt node mismatch: {attempt_id}")
    credit_ids = {row.get("attempt_id") for row in credits}
    for attempt_id, outcome in graph.get("attempt_outcomes", {}).items():
        if outcome["outcome"] in {"verified", "failed", "verified_by_reuse"} and attempt_id not in credit_ids:
            errors.append(f"terminal contribution missing: {attempt_id}")
    report = status(project)
    report.update({"valid": not errors, "accepted": not errors and report["accepted"],
                   "errors": errors, "event_count": len(events),
                   "contribution_count": len(credits),
                   "asset_count": len(registry.get("assets", {}))})
    return report


def parse_author(args: argparse.Namespace) -> dict[str, Any]:
    return contributor(args.contributor_kind, args.contributor_id,
                       getattr(args, "model", None), getattr(args, "role", None))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("project")
    init_parser.add_argument("blueprint")
    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("project")
    claim_parser.add_argument("--worker", required=True)
    claim_parser.add_argument("--node")
    claim_parser.add_argument("--lease-seconds", type=float, default=300)
    heartbeat_parser = subparsers.add_parser("heartbeat")
    heartbeat_parser.add_argument("project")
    heartbeat_parser.add_argument("node")
    heartbeat_parser.add_argument("--worker", required=True)
    heartbeat_parser.add_argument("--token", required=True)
    heartbeat_parser.add_argument("--lease-seconds", type=float, default=300)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("project")
    execute_parser.add_argument("node")
    execute_parser.add_argument("artifact")
    execute_parser.add_argument("--worker", required=True)
    execute_parser.add_argument("--token", required=True)
    execute_parser.add_argument("--contributor-kind", required=True,
                                choices=["human", "agent", "model", "tool"])
    execute_parser.add_argument("--contributor-id", required=True)
    execute_parser.add_argument("--model")
    execute_parser.add_argument("--role", action="append")
    execute_parser.add_argument("--cost-usd", type=float)
    execute_parser.add_argument("--source-id", action="append")
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("project")
    fail_parser.add_argument("node")
    fail_parser.add_argument("--worker", required=True)
    fail_parser.add_argument("--token", required=True)
    fail_parser.add_argument("--failure-class", required=True)
    fail_parser.add_argument("--reason", required=True)
    reuse_parser = subparsers.add_parser("reuse")
    reuse_parser.add_argument("project")
    reuse_parser.add_argument("node")
    reuse_parser.add_argument("asset")
    reuse_parser.add_argument("--contributor-kind", required=True,
                              choices=["human", "agent", "model", "tool"])
    reuse_parser.add_argument("--contributor-id", required=True)
    reuse_parser.add_argument("--model")
    reuse_parser.add_argument("--role", action="append")
    reuse_parser.add_argument("--source-id", action="append")
    revise_parser = subparsers.add_parser("revise")
    revise_parser.add_argument("project")
    revise_parser.add_argument("revision")
    for name in ("resume", "status", "audit"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("project")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(Path(args.project), Path(args.blueprint))
        elif args.command == "claim":
            result = claim(Path(args.project), args.worker, args.lease_seconds, args.node)
            if result is None:
                print(json.dumps({"claimed": False}, indent=2))
                raise SystemExit(2)
        elif args.command == "heartbeat":
            result = heartbeat(Path(args.project), args.node, args.worker,
                               args.token, args.lease_seconds)
        elif args.command == "execute":
            result = execute(Path(args.project), args.node, args.worker, args.token,
                             Path(args.artifact), parse_author(args), args.cost_usd,
                             args.source_id)
        elif args.command == "fail":
            result = record_failure(Path(args.project), args.node, args.worker, args.token,
                                    args.failure_class, args.reason)
        elif args.command == "reuse":
            result = reuse_asset(Path(args.project), args.node, args.asset,
                                 parse_author(args), args.source_id)
        elif args.command == "revise":
            result = revise(Path(args.project), Path(args.revision))
        elif args.command == "resume":
            result = resume(Path(args.project))
        elif args.command == "status":
            result = status(Path(args.project))
        else:
            result = audit(Path(args.project))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "audit" and not result["valid"]:
            raise SystemExit(1)
        if args.command == "execute" and not result["outcome"]["passed"]:
            raise SystemExit(2)
    except GraphError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
