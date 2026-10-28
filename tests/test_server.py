from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from mcp.client import Client
from starlette.responses import JSONResponse

import server
import worker


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def deterministic_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERL_ALLOW_DETERMINISTIC_TEST_MODE", "1")


@pytest.fixture
def isolated_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(server, "PROJECTS_ROOT", root)
    monkeypatch.setattr(server, "LOCAL_REGISTRY", root / ".local-projects.json")
    return root


def test_bundled_engine_hash_is_fixed_and_valid() -> None:
    identity = server._engine_identity()
    assert identity["engine_version"] == "evidence-research-loop-v2.6.4"
    assert identity["engine_hash_valid"] is True


@pytest.mark.anyio
async def test_mcp_lists_and_calls_capabilities() -> None:
    async with Client(server.mcp) as client:
        listed = await client.list_tools()
        tools = listed.tools
        names = {tool.name for tool in tools}
        assert {
            "create_project", "start_search", "resume_search", "audit_project", "finalize_evidence",
            "initialize_research_graph", "claim_research_node", "submit_research_node",
            "revise_research_graph", "audit_research_graph", "start_scientist_study",
            "resume_scientist_study", "audit_scientist_project",
        } <= names
        result = await client.call_tool("get_capabilities", {})
        assert result.structured_content["engine_hash_valid"] is True
        assert result.structured_content["execution"]["codex_cli_bundled"] is False


def test_path_traversal_and_local_attachment_are_denied(isolated_projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        server._project_path("../escape", must_exist=False)
    monkeypatch.delenv("ERL_LOCAL_ALLOWED_ROOTS", raising=False)
    with pytest.raises(PermissionError):
        server.attach_local_project("valid-id", str(isolated_projects))


def test_local_attachment_respects_allowlist(isolated_projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    project = allowed / "existing"
    project.mkdir()
    monkeypatch.setenv("ERL_LOCAL_ALLOWED_ROOTS", str(allowed))
    attached = server.attach_local_project("existing-project", str(project))
    assert attached["attached"] is True
    assert server._project_path("existing-project") == project.resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PermissionError):
        server.attach_local_project("outside-project", str(outside))


def test_secret_environment_is_not_inherited(isolated_projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = isolated_projects / "env-test"
    project.mkdir()
    monkeypatch.setenv("MCP_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "must-not-leak")
    child_env = server._subprocess_env(project)
    assert "MCP_AUTH_TOKEN" not in child_env
    assert "AZURE_CLIENT_SECRET" not in child_env
    assert child_env["HOME"].startswith(str(project))


@pytest.mark.anyio
async def test_bearer_auth_middleware() -> None:
    async def ok_app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    app = server.BearerAuthMiddleware(ok_app, "secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/mcp")
        wrong = await client.post("/mcp", headers={"Authorization": "Bearer wrong"})
        allowed = await client.post("/mcp", headers={"Authorization": "Bearer secret-token"})
        health = await client.get("/healthz")
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert health.status_code == 200


def test_transfer_hash_and_freeze_guard(isolated_projects: Path) -> None:
    created = server.create_project("transfer-test", "python-code")
    assert created["created"] is True
    data = "changed brief\n"
    digest = hashlib.sha256(data.encode()).hexdigest()
    result = server.write_files(
        "transfer-test", [{"path": "brief.md", "content": data, "sha256": digest}], overwrite=True
    )
    assert result["written"][0]["sha256"] == digest
    project = server._project_path("transfer-test")
    (project / "search-state.json").write_text('{"status":"running"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError):
        server.write_files("transfer-test", [{"path": "brief.md", "content": "later"}], overwrite=True)


@pytest.mark.anyio
async def test_research_graph_mcp_flow_records_verified_credit(isolated_projects: Path) -> None:
    project = isolated_projects / "graph-flow"
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
        "nodes": [{
            "id": "goal",
            "type": "implementation",
            "statement": "produce the verified goal artifact",
            "dependencies": [],
            "scope": "project",
            "output_contract": {"kind": "text", "version": 1},
            "verifier": {
                "id": "mcp-test-verifier",
                "command": [os.sys.executable, str(verifier), "{artifact}", "{result_dir}"],
                "timeout_sec": 10,
                "result_file": "result.json",
                "pass_field": "hard_pass",
            },
        }],
    }
    (project / "research-blueprint.json").write_text(json.dumps(blueprint), encoding="utf-8")

    initialized = await server.initialize_research_graph("graph-flow")
    assert initialized["initialized"] is True, initialized
    claimed = await server.claim_research_node("graph-flow", "agent-1")
    assert claimed["claimed"] is True, claimed
    lease = claimed["result"]["lease"]["token"]
    submitted = await server.submit_research_node(
        "graph-flow", "goal", "agent-1", lease, "agent", "agent-1",
        artifact_content="good\n", model="fixture-model", roles=["implementation"],
        source_ids=["fixture-source"], cost_usd=0.01,
    )
    assert submitted["verified"] is True, submitted
    audit = await server.audit_research_graph("graph-flow")
    assert audit["result"]["valid"] is True, audit
    assert audit["result"]["accepted"] is True
    assert audit["result"]["contribution_count"] == 1
    with pytest.raises(RuntimeError):
        server.write_files("graph-flow", [{"path": "research-blueprint.json", "content": "{}"}], overwrite=True)


def test_worker_dispatches_scientist_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result_path = tmp_path / "result.json"
    calls = []

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(
        worker.sys, "argv", ["worker.py", "scientist-start", str(project), str(result_path)]
    )
    assert worker.main() == 0
    assert calls[0][0][1] == str(worker.SCIENTIST_RUNTIME)
    assert calls[0][0][2] == "start"
    assert json.loads(result_path.read_text(encoding="utf-8"))["exit_code"] == 0


@pytest.mark.anyio
async def test_full_deterministic_search_and_audit(isolated_projects: Path) -> None:
    server.create_project("full-search", "python-code")
    validation = await server.validate_project("full-search")
    assert validation["valid"] is True, validation
    started = server.start_search("full-search")
    assert started["launched"] is True
    job_id = started["job"]["job_id"]
    deadline = time.time() + 90
    status = None
    while time.time() < deadline:
        status = server.get_status("full-search", job_id)
        if status["status"] == "finished":
            break
        await asyncio.sleep(0.25)
    assert status is not None
    assert status["status"] == "finished", status
    assert status["search"]["status"] == "completed"
    audit = await server.audit_project("full-search")
    assert audit["result"]["valid"] is True
    assert audit["result"]["accepted"] is True
    finalized = await server.finalize_evidence("full-search")
    assert finalized["written"] is True
    assert finalized["verify"]["result"]["valid"] is True

