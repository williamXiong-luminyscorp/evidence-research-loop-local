from __future__ import annotations

"""Standalone MCP/server regression runner.

This runner deliberately avoids pytest's ``tmp_path`` fixture.  Some managed
Windows hosts attach ACLs to pytest's base temporary directory that make the
session-cleanup phase fail even after every assertion has passed.  Each run
instead gets an immutable, workspace-local evidence directory and emits one
machine-readable summary.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import patch

import httpx
from mcp.client import Client
from starlette.responses import JSONResponse

os.environ["ERL_ALLOW_DETERMINISTIC_TEST_MODE"] = "1"


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402
import worker  # noqa: E402


EXPECTED_TOOLS = {
    "get_capabilities",
    "list_projects",
    "attach_local_project",
    "create_project",
    "write_files",
    "list_files",
    "read_file",
    "validate_project",
    "start_search",
    "resume_search",
    "get_status",
    "audit_project",
    "finalize_evidence",
    "initialize_research_graph",
    "claim_research_node",
    "heartbeat_research_node",
    "submit_research_node",
    "fail_research_node",
    "reuse_research_asset",
    "revise_research_graph",
    "resume_research_graph",
    "get_research_graph_status",
    "audit_research_graph",
    "validate_scientist_project",
    "start_scientist_study",
    "resume_scientist_study",
    "get_scientist_status",
    "audit_scientist_project",
    "export_scientist_report",
    "initialize_composition_graph", "submit_composition", "get_composition_status",
    "audit_composition_graph", "resume_composition_graph", "revoke_composition_evidence",
    "export_composition_asset", "import_composition_asset", "search_composition_assets",
    "record_composition_readback", "attest_composition_milestone",
}


def check(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextmanager
def environment(**updates: str | None):
    previous = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class RegressionRun:
    def __init__(self) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.run_root = ROOT / "test-output" / "server-regressions" / self.run_id
        self.projects = self.run_root / "projects"
        self.projects.mkdir(parents=True)
        self.results: list[dict[str, Any]] = []
        self._original_projects_root = server.PROJECTS_ROOT
        self._original_local_registry = server.LOCAL_REGISTRY
        server.PROJECTS_ROOT = self.projects
        server.LOCAL_REGISTRY = self.projects / ".local-projects.json"

    def close(self) -> None:
        server.PROJECTS_ROOT = self._original_projects_root
        server.LOCAL_REGISTRY = self._original_local_registry

    async def case(
        self,
        name: str,
        function: Callable[[], None | Awaitable[None]],
    ) -> None:
        started = time.perf_counter()
        record: dict[str, Any] = {"name": name}
        try:
            value = function()
            if value is not None:
                await value
            record["status"] = "passed"
        except BaseException as exc:
            record.update(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
        record["elapsed_sec"] = round(time.perf_counter() - started, 6)
        self.results.append(record)
        print(json.dumps({"event": "regression_case", **record}, ensure_ascii=False), flush=True)

    def write_summary(self) -> Path:
        passed = sum(item["status"] == "passed" for item in self.results)
        summary = {
            "schema_version": 1,
            "run_id": self.run_id,
            "runner": Path(__file__).relative_to(ROOT).as_posix(),
            "server_version": server.SERVER_VERSION,
            "engine_version": server.ENGINE_VERSION,
            "python": sys.version,
            "platform": sys.platform,
            "run_root": str(self.run_root.resolve()),
            "passed": passed,
            "failed": len(self.results) - passed,
            "total": len(self.results),
            "cases": self.results,
        }
        target = self.run_root / "verification.json"
        target.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        latest = ROOT / "test-output" / "server-regressions" / "verification-latest.json"
        latest.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target


def case_engine_identity() -> None:
    identity = server._engine_identity()
    check(
        identity["engine_version"] == "evidence-research-loop-v2.6.5",
        str(identity),
    )
    check(identity["engine_hash_valid"] is True, str(identity))
    check(identity["engine_file_count"] > 0, str(identity))


async def case_mcp_contract() -> None:
    async with Client(server.mcp) as client:
        listed = await client.list_tools()
        tools = listed.tools
        names = {tool.name for tool in tools}
        check(len(tools) == len(EXPECTED_TOOLS), f"expected {len(EXPECTED_TOOLS)} tools, observed {len(tools)}")
        check(names == EXPECTED_TOOLS, f"tool mismatch: {sorted(names ^ EXPECTED_TOOLS)}")
        for tool in tools:
            schema = tool.input_schema
            check(isinstance(schema, dict), f"{tool.name} has no input schema")
            check(schema.get("type") == "object", f"{tool.name} schema is not an object: {schema}")
        result = await client.call_tool("get_capabilities", {})
        content = result.structured_content
        check(content["engine_hash_valid"] is True, str(content))
        check(content["execution"]["codex_cli_bundled"] is False, str(content))


def case_workspace_boundaries(run: RegressionRun) -> None:
    try:
        server._project_path("../escape", must_exist=False)
    except ValueError:
        pass
    else:
        raise AssertionError("project path traversal was accepted")

    allowed = run.run_root / "allowed"
    project = allowed / "existing"
    outside = run.run_root / "outside"
    project.mkdir(parents=True)
    outside.mkdir()
    with environment(ERL_LOCAL_ALLOWED_ROOTS=None):
        try:
            server.attach_local_project("denied-project", str(project))
        except PermissionError:
            pass
        else:
            raise AssertionError("local attachment without an allowlist was accepted")
    with environment(ERL_LOCAL_ALLOWED_ROOTS=str(allowed)):
        attached = server.attach_local_project("existing-project", str(project))
        check(attached["attached"] is True, str(attached))
        check(server._project_path("existing-project") == project.resolve(), str(attached))
        try:
            server.attach_local_project("outside-project", str(outside))
        except PermissionError:
            pass
        else:
            raise AssertionError("attachment outside the allowlist was accepted")


def case_secret_environment(run: RegressionRun) -> None:
    project = run.projects / "env-test"
    project.mkdir()
    with environment(MCP_AUTH_TOKEN="must-not-leak", AZURE_CLIENT_SECRET="must-not-leak"):
        child_env = server._subprocess_env(project)
    check("MCP_AUTH_TOKEN" not in child_env, str(child_env))
    check("AZURE_CLIENT_SECRET" not in child_env, str(child_env))
    check(child_env["HOME"].startswith(str(project)), str(child_env))


async def case_bearer_auth() -> None:
    async def ok_app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    app = server.BearerAuthMiddleware(ok_app, "secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/mcp")
        wrong = await client.post("/mcp", headers={"Authorization": "Bearer wrong"})
        allowed = await client.post("/mcp", headers={"Authorization": "Bearer secret-token"})
        health = await client.get("/healthz")
    check(missing.status_code == 401, str(missing.status_code))
    check(wrong.status_code == 401, str(wrong.status_code))
    check(allowed.status_code == 200, str(allowed.status_code))
    check(health.status_code == 200, str(health.status_code))


def case_transfer_freeze_guard() -> None:
    created = server.create_project("transfer-test", "python-code")
    check(created["created"] is True, str(created))
    data = "changed brief\n"
    digest = hashlib.sha256(data.encode()).hexdigest()
    result = server.write_files(
        "transfer-test", [{"path": "brief.md", "content": data, "sha256": digest}], overwrite=True
    )
    check(result["written"][0]["sha256"] == digest, str(result))
    project = server._project_path("transfer-test")
    (project / "search-state.json").write_text('{"status":"running"}\n', encoding="utf-8")
    try:
        server.write_files("transfer-test", [{"path": "brief.md", "content": "later"}], overwrite=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("runtime project mutation was accepted")


async def case_research_graph_flow(run: RegressionRun) -> None:
    project = run.projects / "graph-flow"
    project.mkdir()
    verifier = project / "verifier.py"
    verifier.write_text(
        "import json, pathlib, sys\n"
        "artifact = pathlib.Path(sys.argv[1])\n"
        "result = pathlib.Path(sys.argv[2])\n"
        "result.mkdir(parents=True, exist_ok=True)\n"
        "passed = artifact.read_text(encoding='utf-8').strip() == 'good'\n"
        "(result / 'result.json').write_text(json.dumps({'hard_pass': passed}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    blueprint = {
        "schema_version": 1,
        "graph_id": "mcp-graph",
        "title": "MCP graph integration",
        "goal_node_ids": ["goal"],
        "nodes": [
            {
                "id": "goal",
                "type": "implementation",
                "statement": "produce the verified goal artifact",
                "dependencies": [],
                "scope": "project",
                "output_contract": {"kind": "text", "version": 1},
                "verifier": {
                    "id": "mcp-test-verifier",
                    "command": [sys.executable, str(verifier), "{artifact}", "{result_dir}"],
                    "timeout_sec": 10,
                    "result_file": "result.json",
                    "pass_field": "hard_pass",
                },
            }
        ],
    }
    (project / "research-blueprint.json").write_text(
        json.dumps(blueprint, indent=2) + "\n", encoding="utf-8"
    )
    initialized = await server.initialize_research_graph("graph-flow")
    check(initialized["initialized"] is True, str(initialized))
    claimed = await server.claim_research_node("graph-flow", "agent-1")
    check(claimed["claimed"] is True, str(claimed))
    lease = claimed["result"]["lease"]["token"]
    submitted = await server.submit_research_node(
        "graph-flow",
        "goal",
        "agent-1",
        lease,
        "agent",
        "agent-1",
        artifact_content="good\n",
        model="fixture-model",
        roles=["implementation"],
        source_ids=["fixture-source"],
        cost_usd=0.01,
    )
    check(submitted["verified"] is True, str(submitted))
    audit = await server.audit_research_graph("graph-flow")
    check(audit["result"]["valid"] is True, str(audit))
    check(audit["result"]["accepted"] is True, str(audit))
    check(audit["result"]["contribution_count"] == 1, str(audit))
    try:
        server.write_files(
            "graph-flow", [{"path": "research-blueprint.json", "content": "{}"}], overwrite=True
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("active graph blueprint mutation was accepted")


def case_scientist_worker_dispatch(run: RegressionRun) -> None:
    project = run.run_root / "scientist-worker-project"
    project.mkdir()
    result_path = run.run_root / "scientist-worker-result.json"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    argv = ["worker.py", "scientist-start", str(project), str(result_path)]
    with patch.object(worker.subprocess, "run", fake_run), patch.object(worker.sys, "argv", argv):
        check(worker.main() == 0, "scientist worker returned nonzero")
    check(calls[0][0][1] == str(worker.SCIENTIST_RUNTIME), str(calls))
    check(calls[0][0][2] == "start", str(calls))
    check(json.loads(result_path.read_text(encoding="utf-8"))["exit_code"] == 0, str(calls))


async def case_full_search() -> None:
    created = server.create_project("full-search", "python-code")
    check(created["created"] is True, str(created))
    validation = await server.validate_project("full-search")
    check(validation["valid"] is True, str(validation))
    started = server.start_search("full-search")
    check(started["launched"] is True, str(started))
    job_id = started["job"]["job_id"]
    deadline = time.time() + 120
    status = None
    while time.time() < deadline:
        status = server.get_status("full-search", job_id)
        if status["status"] == "finished":
            break
        await asyncio.sleep(0.25)
    check(status is not None, "search status unavailable")
    check(status["status"] == "finished", str(status))
    check(status["search"]["status"] == "completed", str(status))
    audit = await server.audit_project("full-search")
    check(audit["result"]["valid"] is True, str(audit))
    check(audit["result"]["accepted"] is True, str(audit))
    finalized = await server.finalize_evidence("full-search")
    check(finalized["written"] is True, str(finalized))
    check(finalized["verify"]["result"]["valid"] is True, str(finalized))


async def main() -> int:
    run = RegressionRun()
    try:
        cases: list[tuple[str, Callable[[], None | Awaitable[None]]]] = [
            ("engine_identity", case_engine_identity),
            ("mcp_29_tool_contract", case_mcp_contract),
            ("workspace_boundaries", lambda: case_workspace_boundaries(run)),
            ("secret_environment_filter", lambda: case_secret_environment(run)),
            ("bearer_auth", case_bearer_auth),
            ("transfer_hash_and_freeze_guard", case_transfer_freeze_guard),
            ("research_graph_mcp_flow", lambda: case_research_graph_flow(run)),
            ("scientist_worker_dispatch", lambda: case_scientist_worker_dispatch(run)),
            ("deterministic_search_audit_finalize", case_full_search),
        ]
        for name, function in cases:
            await run.case(name, function)
        summary_path = run.write_summary()
        passed = sum(item["status"] == "passed" for item in run.results)
        result = {
            "event": "server_regression_complete",
            "passed": passed,
            "failed": len(run.results) - passed,
            "total": len(run.results),
            "summary": str(summary_path.resolve()),
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if passed == len(run.results) else 1
    finally:
        run.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
