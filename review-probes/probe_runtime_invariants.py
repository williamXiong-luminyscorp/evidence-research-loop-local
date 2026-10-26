"""Independent probes for Research Graph and Scientist Runtime invariants.

This file is intentionally outside the shipped test suite.  Each probe records
the current behavior for a safety property that should become a regression test
before the new runtimes are accepted.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "evidence-research-loop"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


graph = load("probe_research_graph", SKILL / "scripts" / "research_graph.py")
scientist = load("probe_scientist_runtime", SKILL / "scripts" / "scientist_runtime.py")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def graph_fixture(root: Path, statements: dict[str, str], dependencies: dict[str, list[str]],
                  goals: list[str], portable: set[str] | None = None) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=False)
    portable = portable or set()
    verifier = root / "verifier.py"
    verifier.write_text(
        "import json, pathlib, sys\n"
        "artifact = pathlib.Path(sys.argv[1])\n"
        "result = pathlib.Path(sys.argv[2])\n"
        "result.mkdir(parents=True, exist_ok=True)\n"
        "passed = artifact.read_text(encoding='utf-8').strip() == 'good'\n"
        "(result / 'result.json').write_text(json.dumps({'hard_pass': passed}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    nodes = []
    for node_id, statement in statements.items():
        nodes.append({
            "id": node_id,
            "type": "implementation",
            "statement": statement,
            "dependencies": dependencies.get(node_id, []),
            "scope": "portable" if node_id in portable else "project",
            "output_contract": {"kind": "text", "version": 1},
            "verifier": {
                "id": "probe-verifier-v1",
                "command": [sys.executable, str(verifier), "{artifact}", "{result_dir}"],
                "timeout_sec": 10,
                "result_file": "result.json",
                "pass_field": "hard_pass",
            },
        })
    blueprint = root / "blueprint.json"
    write_json(blueprint, {
        "schema_version": 1,
        "graph_id": "probe-graph",
        "title": "Research Graph invariant probes",
        "goal_node_ids": goals,
        "nodes": nodes,
    })
    project = root / "graph-project"
    graph.initialize(project, blueprint)
    artifact = root / "good.txt"
    artifact.write_text("good\n", encoding="utf-8")
    return project, artifact


def execute_node(project: Path, artifact: Path, node_id: str):
    worker = f"worker-{node_id}"
    lease = graph.claim(project, worker, 60, node_id)
    assert lease is not None
    return graph.execute(
        project, node_id, worker, lease["lease"]["token"], artifact,
        graph.contributor("agent", worker, "probe-model", ["implementation"]),
    )


def probe_expired_lease(root: Path) -> dict[str, object]:
    project, artifact = graph_fixture(root, {"goal": "implement goal"}, {}, ["goal"])
    lease = graph.claim(project, "worker", -1, "goal")
    assert lease is not None
    accepted = True
    error = None
    try:
        graph.heartbeat(project, "goal", "worker", lease["lease"]["token"], 60)
    except Exception as exc:  # noqa: BLE001 - probe records actual boundary
        accepted = False
        error = str(exc)
    return {"expired_lease_heartbeat_accepted": accepted, "error": error,
            "desired": "expired lease must be rejected"}


def probe_semantic_asset_reuse(root: Path) -> dict[str, object]:
    project, artifact = graph_fixture(
        root,
        {"source": "calculate payroll tax", "target": "prove access control is safe"},
        {}, ["target"], {"source"},
    )
    result = execute_node(project, artifact, "source")
    accepted = True
    error = None
    try:
        graph.reuse_asset(
            project, "target", result["asset"]["asset_id"],
            graph.contributor("agent", "reuse-worker", "probe-model", ["reuse"]),
        )
    except Exception as exc:  # noqa: BLE001
        accepted = False
        error = str(exc)
    return {"semantically_unrelated_asset_reuse_accepted": accepted, "error": error,
            "graph_accepted": graph.status(project)["accepted"],
            "audit": graph.audit(project),
            "desired": "reuse must bind statement/input/verifier semantics, not only output shape"}


def probe_verifier_drift(root: Path) -> dict[str, object]:
    project, _ = graph_fixture(root, {"goal": "reject invalid artifacts"}, {}, ["goal"])
    verifier = root / "verifier.py"
    verifier.write_text(
        "import json, pathlib, sys\n"
        "result = pathlib.Path(sys.argv[2])\n"
        "result.mkdir(parents=True, exist_ok=True)\n"
        "(result / 'result.json').write_text(json.dumps({'hard_pass': True}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    bad = root / "bad.txt"
    bad.write_text("bad\n", encoding="utf-8")
    accepted = False
    error = None
    try:
        result = execute_node(project, bad, "goal")
        accepted = result["outcome"]["passed"]
    except Exception as exc:  # noqa: BLE001 - probe records the enforced boundary
        error = str(exc)
    return {"changed_verifier_accepted_bad_artifact": accepted,
            "error": error,
            "audit": graph.audit(project),
            "desired": "freeze and revalidate verifier executable/dependency hashes before every run"}


def probe_superseded_dependency(root: Path) -> dict[str, object]:
    project, artifact = graph_fixture(
        root, {"foundation": "build foundation", "goal": "integrate foundation"},
        {"goal": ["foundation"]}, ["goal"],
    )
    execute_node(project, artifact, "foundation")
    execute_node(project, artifact, "goal")
    revision = root / "revision.json"
    write_json(revision, {
        "expected_revision": 1,
        "reason": "foundation is invalid and must be replaced",
        "add_nodes": [{
            "id": "replacement", "type": "implementation",
            "statement": "build replacement foundation", "dependencies": [],
            "scope": "project", "output_contract": {"kind": "text", "version": 1},
            "verifier": graph.require_graph(project)["nodes"]["foundation"]["verifier"],
        }],
        "dependency_updates": [],
        "supersede": [{"node_id": "foundation", "replacement_ids": ["replacement"]}],
        "invalidate": [],
    })
    graph.revise(project, revision)
    return {"accepted_after_dependency_superseded": graph.status(project)["accepted"],
            "goal_status": graph.require_graph(project)["nodes"]["goal"]["status"],
            "audit": graph.audit(project),
            "desired": "superseding a dependency must stale all verified descendants"}


def scientist_fixture(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=False)
    worker = root / "scientist-worker.py"
    worker.write_text(
        "import json, pathlib, sys\n"
        "stage, request, response, attempt_dir = sys.argv[1:]\n"
        "d = pathlib.Path(attempt_dir)\n"
        "usage = {'model_calls': 0, 'known_cost_usd': 0.0}\n"
        "if stage == 'ideation': out = {'stage': stage, 'ideas': "
        "[{'idea_id':'i','hypothesis':'h','falsifiable_prediction':'p','minimum_experiment':'e'}], 'usage': usage}\n"
        "elif stage == 'literature': out = {'stage': stage, 'sources': "
        "[{'source_id':'s','title':'t','locator':'local:s'}], "
        "'novelty_assessment':{'status':'unknown','reason':'not tested'}, 'usage': usage}\n"
        "elif stage == 'experiment':\n"
        " p=d/'raw.json'; p.write_text('1', encoding='utf-8'); out={'stage':stage,'experiments':"
        "[{'experiment_id':'e','status':'passed','metrics':{'x':1},'evidence_files':['raw.json']}], 'usage':usage}\n"
        "elif stage == 'analysis': out={'stage':stage,'claims':"
        "[{'claim_id':'c','statement':'supported','status':'supported','evidence_ids':['e']}], 'usage':usage}\n"
        "else: out={'stage':stage,'decision':'accept','claim_reviews':"
        "[{'claim_id':'c','decision':'accept','reason':'ok'}], 'usage':usage}\n"
        "pathlib.Path(response).write_text(json.dumps(out), encoding='utf-8')\n",
        encoding="utf-8",
    )
    project = root / "scientist-project"
    project.mkdir()
    frozen = project / "frozen.txt"
    frozen.write_text("original\n", encoding="utf-8")
    command = [sys.executable, str(worker), "{stage}", "{request}", "{response}", "{attempt_dir}"]
    write_json(project / "research-plan.json", {
        "schema_version": 1,
        "plan_version": "probe-v1",
        "research_question": "Does the frozen intervention work?",
        "claim_boundary": "Only the frozen probe input.",
        "frozen_input_files": ["frozen.txt"],
        "budget": {"max_stage_calls": 5, "max_wall_time_sec": 60,
                   "max_known_cost_usd": None},
        "acceptance": {"required_claim_ids": ["c"]},
        "stages": {stage: {
            "enabled": True, "max_attempts": 1,
            "backend": {"kind": "command", "command": command, "timeout_sec": 10,
                        "env_allowlist": [], "may_use_model": False},
        } for stage in scientist.STAGES},
    })
    return project, frozen


def probe_rewritten_protocol_lock(root: Path) -> dict[str, object]:
    project, frozen = scientist_fixture(root)
    report = scientist.execute(project)
    frozen.write_text("replacement\n", encoding="utf-8")
    lock_path = project / "protocol-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["frozen_inputs"][0]["sha256"] = scientist.sha256_file(frozen)
    write_json(lock_path, lock)
    audit = scientist.audit(project)
    return {"initially_accepted": report["acceptance"]["accepted"],
            "audit_after_input_and_lock_rewrite": audit,
            "desired": "audit must bind the immutable protocol lock to an earlier event/hash anchor"}


def probe_rewritten_analysis_and_state(root: Path) -> dict[str, object]:
    project, _ = scientist_fixture(root)
    report = scientist.execute(project)
    state_path = scientist.state_path(project)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    output_path = project / state["stages"]["analysis"]["output_path"]
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["claims"][0]["statement"] = "replacement claim inserted after execution"
    write_json(output_path, output)
    new_hash = scientist.sha256_file(output_path)
    state["stages"]["analysis"]["output_hash"] = new_hash
    write_json(state_path, state)
    write_json(project / "analysis-results.json", output)
    matrix_path = project / "claim-evidence-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["claims"] = output["claims"]
    matrix["response_hash"] = new_hash
    write_json(matrix_path, matrix)
    audit = scientist.audit(project)
    return {"initially_accepted": report["acceptance"]["accepted"],
            "audit_after_analysis_and_state_rewrite": audit,
            "desired": "audit must cross-check stage output hashes against durable attempt and event records"}


def main() -> None:
    parent = ROOT / "review-probes" / ".tmp"
    parent.mkdir(parents=True, exist_ok=True)
    base = parent / f"run-{uuid.uuid4().hex}"
    base.mkdir()
    results = {
        "run_directory": str(base),
        "expired_lease": probe_expired_lease(base / "lease"),
        "semantic_asset_reuse": probe_semantic_asset_reuse(base / "reuse"),
        "verifier_drift": probe_verifier_drift(base / "verifier-drift"),
        "superseded_dependency": probe_superseded_dependency(base / "supersede"),
        "rewritten_protocol_lock": probe_rewritten_protocol_lock(base / "scientist"),
        "rewritten_analysis_and_state": probe_rewritten_analysis_and_state(
            base / "scientist-state"),
    }
    write_json(base / "probe-results.json", results)
    write_json(ROOT / "review-probes" / "verification-latest.json", results)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
