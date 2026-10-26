from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "research_graph", SKILL_ROOT / "scripts" / "research_graph.py")
graph_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph_api)


class ResearchGraphTests(unittest.TestCase):
    def setUp(self):
        override = os.environ.get("ERL_TEST_TMP")
        if override:
            base = Path(override)
            base.mkdir(parents=True, exist_ok=True)
            self.root = base / f"graph-{uuid.uuid4().hex}"
            self.root.mkdir()
        else:
            base = SKILL_ROOT / "tests" / ".tmp-research-graph"
            base.mkdir(parents=True, exist_ok=True)
            self.root = base / f"graph-{uuid.uuid4().hex}"
            self.root.mkdir()
        self.project = self.root / "project"
        self.verifier = self.root / "verifier.py"
        self.verifier.write_text(
            "import json, pathlib, sys\n"
            "artifact = pathlib.Path(sys.argv[1])\n"
            "result = pathlib.Path(sys.argv[2])\n"
            "result.mkdir(parents=True, exist_ok=True)\n"
            "passed = artifact.read_text(encoding='utf-8').strip() == 'good'\n"
            "(result / 'result.json').write_text(json.dumps({'hard_pass': passed}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.good = self.root / "good.txt"
        self.good.write_text("good\n", encoding="utf-8")
        self.bad = self.root / "bad.txt"
        self.bad.write_text("bad\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def node(self, node_id, dependencies=None, contract=None, scope="project",
             statement=None):
        return {
            "id": node_id,
            "type": "implementation",
            "statement": statement or f"complete {node_id}",
            "dependencies": dependencies or [],
            "scope": scope,
            "output_contract": contract or {"kind": "text", "version": 1},
            "verifier": {
                "id": "fixture-verifier-v1",
                "command": [sys.executable, str(self.verifier), "{artifact}", "{result_dir}"],
                "timeout_sec": 10,
                "result_file": "result.json",
                "pass_field": "hard_pass",
            },
        }

    def initialize(self, nodes, goals):
        blueprint = {
            "schema_version": 1,
            "graph_id": "test-graph",
            "title": "Executable research graph test",
            "goal_node_ids": goals,
            "nodes": nodes,
        }
        path = self.root / "blueprint.json"
        path.write_text(json.dumps(blueprint), encoding="utf-8")
        return graph_api.initialize(self.project, path)

    @staticmethod
    def author(name="agent-a"):
        return graph_api.contributor("agent", name, "deterministic-test-model", ["implementation"])

    def execute_ready(self, node_id, artifact=None, worker=None):
        worker = worker or f"worker-{node_id}"
        lease = graph_api.claim(self.project, worker, 60, node_id)
        self.assertIsNotNone(lease)
        return graph_api.execute(
            self.project, node_id, worker, lease["lease"]["token"],
            artifact or self.good, self.author(worker), 0.01, ["fixture"],
        )

    def test_atomic_claim_prevents_duplicate_assignment(self):
        self.initialize([self.node("only")], ["only"])
        barrier = threading.Barrier(2)

        def contender(name):
            barrier.wait()
            return graph_api.claim(self.project, name, 60, "only")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, ["agent-a", "agent-b"]))
        self.assertEqual(1, sum(result is not None for result in results))
        state = graph_api.status(self.project)
        self.assertEqual(1, len(state["leased"]))
        self.assertFalse(state["accepted"])

    def test_real_verifier_unlocks_dependency_and_records_credit_asset(self):
        self.initialize([self.node("design"), self.node("build", ["design"])], ["build"])
        result = self.execute_ready("design")
        self.assertTrue(result["outcome"]["passed"])
        state = graph_api.status(self.project)
        self.assertEqual(["build"], state["ready_node_ids"])
        registry = graph_api.load_json(graph_api.registry_path(self.project))
        self.assertEqual(1, len(registry["assets"]))
        credits = graph_api.read_jsonl(graph_api.credits_path(self.project))
        self.assertEqual("verified", credits[0]["outcome"])
        self.assertEqual("deterministic-test-model", credits[0]["contributor"]["model"])
        self.execute_ready("build")
        audit = graph_api.audit(self.project)
        self.assertTrue(audit["valid"], audit["errors"])
        self.assertTrue(audit["accepted"])
        self.assertEqual(2, audit["contribution_count"])

    def test_shared_volume_copy_avoids_copystat_for_attempt_and_asset(self):
        self.initialize([self.node("goal")], ["goal"])
        with patch.object(graph_api, "SHARED_VOLUME_PERMISSIONS", True), \
                patch.object(graph_api.shutil, "copy2", side_effect=PermissionError("copystat denied")):
            result = self.execute_ready("goal")
        self.assertTrue(result["outcome"]["passed"], result)
        self.assertEqual("verified", graph_api.status(self.project)["goals"]["goal"])
        self.assertTrue(graph_api.audit(self.project)["accepted"])

    def test_claim_returns_only_direct_verified_dependency_assets(self):
        self.initialize([
            self.node("foundation"),
            self.node("unrelated"),
            self.node("goal", ["foundation"]),
        ], ["goal"])
        foundation = self.execute_ready("foundation")["asset"]
        unrelated = self.execute_ready("unrelated")["asset"]
        claimed = graph_api.claim(self.project, "goal-worker", 60, "goal")
        self.assertIsNotNone(claimed)
        assets = claimed["dependency_assets"]
        self.assertEqual(["foundation"], [item["node_id"] for item in assets])
        self.assertEqual(foundation["asset_id"], assets[0]["asset_id"])
        self.assertEqual(foundation["artifact_hash"], assets[0]["artifact_hash"])
        self.assertEqual(foundation["semantic_binding"], assets[0]["semantic_binding"])
        self.assertNotEqual(unrelated["asset_id"], assets[0]["asset_id"])

    def test_failure_stales_descendant_and_revision_splits_rewires_graph(self):
        self.initialize([
            self.node("foundation"),
            self.node("large", ["foundation"]),
            self.node("integration", ["large"]),
        ], ["integration"])
        self.execute_ready("foundation")
        failed = self.execute_ready("large", self.bad)
        self.assertFalse(failed["outcome"]["passed"])
        current = graph_api.require_graph(self.project)
        self.assertEqual("failed", current["nodes"]["large"]["status"])
        self.assertEqual("stale", current["nodes"]["integration"]["status"])

        revision = {
            "expected_revision": 1,
            "reason": "the large node failed; split it into independently verifiable parts",
            "add_nodes": [
                self.node("part-a", ["foundation"]),
                self.node("part-b", ["foundation"]),
            ],
            "dependency_updates": [
                {"node_id": "integration", "dependencies": ["part-a", "part-b"]}
            ],
            "supersede": [
                {"node_id": "large", "replacement_ids": ["part-a", "part-b"]}
            ],
            "invalidate": [],
        }
        revision_path = self.root / "revision.json"
        revision_path.write_text(json.dumps(revision), encoding="utf-8")
        result = graph_api.revise(self.project, revision_path)
        self.assertEqual(2, result["blueprint_revision"])
        current = graph_api.require_graph(self.project)
        self.assertEqual("superseded", current["nodes"]["large"]["status"])
        self.assertEqual("ready", current["nodes"]["part-a"]["status"])
        self.assertEqual("ready", current["nodes"]["part-b"]["status"])
        self.assertEqual("stale", current["nodes"]["integration"]["status"])
        self.execute_ready("part-a")
        self.execute_ready("part-b")
        self.assertEqual(["integration"], graph_api.status(self.project)["ready_node_ids"])
        self.execute_ready("integration")
        audit = graph_api.audit(self.project)
        self.assertTrue(audit["valid"], audit["errors"])
        self.assertTrue(audit["accepted"])
        self.assertTrue((self.project / "blueprint-revisions" / "revision-0002.json").is_file())

    def test_verified_asset_can_be_reused_only_under_matching_contract(self):
        common = {"kind": "portable-rule", "version": 1}
        self.initialize([
            self.node("source", contract=common, scope="portable",
                      statement="establish the same portable invariant"),
            self.node("target", contract=common, scope="another-domain",
                      statement="establish the same portable invariant"),
            self.node("unrelated", contract=common,
                      statement="establish an unrelated security claim"),
            self.node("wrong", contract={"kind": "other", "version": 1}),
        ], ["target"])
        completed = self.execute_ready("source")
        asset_id = completed["asset"]["asset_id"]
        reused = graph_api.reuse_asset(
            self.project, "target", asset_id,
            graph_api.contributor("agent", "reuse-agent", "test-model", ["reuse"]),
        )
        self.assertEqual("verified", reused["status"])
        with self.assertRaisesRegex(graph_api.GraphError, "semantic binding"):
            graph_api.reuse_asset(
                self.project, "unrelated", asset_id, self.author("unrelated-agent"))
        with self.assertRaises(graph_api.GraphError):
            graph_api.reuse_asset(self.project, "wrong", asset_id, self.author("wrong-agent"))
        audit = graph_api.audit(self.project)
        self.assertTrue(audit["valid"], audit["errors"])
        self.assertTrue(audit["accepted"])
        outcomes = [row["outcome"] for row in graph_api.read_jsonl(
            graph_api.credits_path(self.project))]
        self.assertIn("verified_by_reuse", outcomes)

    def test_expired_lease_is_rejected_and_recovered_for_all_mutations(self):
        self.initialize([
            self.node("heartbeat-node"), self.node("execute-node"), self.node("fail-node")
        ], ["heartbeat-node"])

        heartbeat_lease = graph_api.claim(
            self.project, "heartbeat-worker", -1, "heartbeat-node")
        with self.assertRaisesRegex(graph_api.GraphError, "expired"):
            graph_api.heartbeat(
                self.project, "heartbeat-node", "heartbeat-worker",
                heartbeat_lease["lease"]["token"], 60)

        execute_lease = graph_api.claim(self.project, "execute-worker", -1, "execute-node")
        with self.assertRaisesRegex(graph_api.GraphError, "expired"):
            graph_api.execute(
                self.project, "execute-node", "execute-worker",
                execute_lease["lease"]["token"], self.good,
                self.author("execute-worker"))

        fail_lease = graph_api.claim(self.project, "fail-worker", -1, "fail-node")
        with self.assertRaisesRegex(graph_api.GraphError, "expired"):
            graph_api.record_failure(
                self.project, "fail-node", "fail-worker", fail_lease["lease"]["token"],
                "worker_error", "must not be accepted after expiry")

        current = graph_api.require_graph(self.project)
        self.assertEqual("ready", current["nodes"]["heartbeat-node"]["status"])
        self.assertEqual("ready", current["nodes"]["execute-node"]["status"])
        self.assertEqual("ready", current["nodes"]["fail-node"]["status"])
        events = graph_api.read_jsonl(graph_api.events_path(self.project))
        self.assertFalse(any("token" in event or "lease_token" in event for event in events))

    def test_verifier_file_is_frozen_and_drift_is_rejected(self):
        self.initialize([self.node("goal")], ["goal"])
        frozen = graph_api.require_graph(self.project)["nodes"]["goal"]["verifier"]
        self.assertTrue(frozen["frozen_file_manifest"])
        self.assertEqual(frozen["contract_hash"], graph_api.verifier_contract_hash(frozen))
        self.verifier.write_text(
            "import json, pathlib, sys\n"
            "result = pathlib.Path(sys.argv[2]); result.mkdir(parents=True, exist_ok=True)\n"
            "(result / 'result.json').write_text(json.dumps({'hard_pass': True}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        lease = graph_api.claim(self.project, "worker", 60, "goal")
        with self.assertRaisesRegex(graph_api.GraphError, "verifier integrity failure"):
            graph_api.execute(
                self.project, "goal", "worker", lease["lease"]["token"], self.bad,
                self.author("worker"))
        audit = graph_api.audit(self.project)
        self.assertFalse(audit["valid"])
        self.assertTrue(any("frozen verifier file hash mismatch" in error
                            for error in audit["errors"]))

    def test_superseding_verified_dependency_stales_verified_goal(self):
        self.initialize([
            self.node("foundation"), self.node("goal", ["foundation"])
        ], ["goal"])
        self.execute_ready("foundation")
        self.execute_ready("goal")
        self.assertTrue(graph_api.status(self.project)["accepted"])
        revision = {
            "expected_revision": 1,
            "reason": "replace a previously verified foundation",
            "add_nodes": [self.node("replacement")],
            "dependency_updates": [],
            "supersede": [{"node_id": "foundation", "replacement_ids": ["replacement"]}],
            "invalidate": [],
        }
        path = self.root / "supersede-verified.json"
        path.write_text(json.dumps(revision), encoding="utf-8")
        graph_api.revise(self.project, path)
        current = graph_api.require_graph(self.project)
        self.assertEqual("superseded", current["nodes"]["foundation"]["status"])
        self.assertEqual("stale", current["nodes"]["goal"]["status"])
        self.assertFalse(graph_api.status(self.project)["accepted"])

    def test_resume_commits_durable_verifier_result_without_rerun(self):
        self.initialize([self.node("recoverable")], ["recoverable"])
        lease = graph_api.claim(self.project, "agent-a", 60, "recoverable")
        attempt = graph_api.start_attempt(
            self.project, "recoverable", "agent-a", lease["lease"]["token"],
            self.good, self.author(), 0.01, ["fixture"],
        )
        attempt_dir = graph_api.attempt_path(self.project, attempt["attempt_id"]).parent
        result_dir = attempt_dir / "results"
        result_dir.mkdir()
        (result_dir / "result.json").write_text(
            json.dumps({"hard_pass": True}), encoding="utf-8")
        graph_api.finish_attempt_record(self.project, attempt["attempt_id"], {
            "status": "verifier_finished",
            "command": ["simulated", "already-paid-call"],
            "verifier_id": "fixture-verifier-v1",
            "exit_code": 0,
            "timed_out": False,
            "elapsed_sec": 0.25,
        })
        resumed = graph_api.resume(self.project)
        self.assertEqual([attempt["attempt_id"]], resumed["finalized_attempt_ids"])
        self.assertTrue(resumed["status"]["accepted"])
        second = graph_api.resume(self.project)
        self.assertEqual([], second["finalized_attempt_ids"])
        audit = graph_api.audit(self.project)
        self.assertTrue(audit["valid"], audit["errors"])

    def test_revision_refuses_to_rewire_active_work(self):
        self.initialize([self.node("active"), self.node("goal", ["active"])], ["goal"])
        graph_api.claim(self.project, "agent-a", 60, "active")
        revision = {
            "expected_revision": 1,
            "reason": "should not silently invalidate work already in progress",
            "add_nodes": [],
            "dependency_updates": [{"node_id": "active", "dependencies": []}],
            "supersede": [],
            "invalidate": [],
        }
        path = self.root / "active-revision.json"
        path.write_text(json.dumps(revision), encoding="utf-8")
        with self.assertRaisesRegex(graph_api.GraphError, "active work"):
            graph_api.revise(self.project, path)
        self.assertEqual(1, graph_api.status(self.project)["blueprint_revision"])

    def test_audit_detects_verified_asset_tampering(self):
        self.initialize([self.node("goal")], ["goal"])
        completed = self.execute_ready("goal")
        asset = completed["asset"]
        (self.project / asset["artifact_path"]).write_text("tampered", encoding="utf-8")
        audit = graph_api.audit(self.project)
        self.assertFalse(audit["valid"])
        self.assertFalse(audit["accepted"])
        self.assertTrue(any("asset hash mismatch" in error for error in audit["errors"]))


if __name__ == "__main__":
    unittest.main()
