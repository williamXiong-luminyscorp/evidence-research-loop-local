from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scientist_runtime", SKILL_ROOT / "scripts" / "scientist_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


WORKER = r'''
import json
import os
import sys
from pathlib import Path

stage, request_path, response_path, attempt_dir_text = sys.argv[1:]
attempt_dir = Path(attempt_dir_text)
request = json.loads(Path(request_path).read_text(encoding="utf-8"))

fail_stage = os.environ.get("SCIENTIST_FAIL_ONCE_STAGE")
marker_text = os.environ.get("SCIENTIST_FAIL_MARKER")
if fail_stage == stage and marker_text:
    marker = Path(marker_text)
    if not marker.exists():
        marker.write_text("failed once", encoding="utf-8")
        print("intentional transient failure", file=sys.stderr)
        raise SystemExit(7)

usage = {"model": "fixture-command", "model_calls": 1, "input_tokens": 10,
         "output_tokens": 20, "known_cost_usd": 0.01}
if stage == "ideation":
    response = {
        "stage": stage,
        "ideas": [{"idea_id": "idea-1", "hypothesis": "the intervention improves the metric",
                   "falsifiable_prediction": "metric delta is positive",
                   "minimum_experiment": "run the frozen workload"}],
        "usage": usage,
    }
elif stage == "literature":
    if os.environ.get("SCIENTIST_EMPTY_LITERATURE") == "1":
        sources = []
    else:
        sources = [{"source_id": "source-1", "title": "Primary result",
                    "locator": "https://example.invalid/primary", "source_type": "paper",
                    "supports": ["idea-1"], "refutes": []}]
    response = {"stage": stage, "sources": sources,
                "novelty_assessment": {"status": "supported_distinct",
                                       "reason": "fixture response"}, "usage": usage}
elif stage == "experiment":
    if os.environ.get("SCIENTIST_EXPERIMENT_FAIL") == "1":
        experiments = [{"experiment_id": "exp-1", "hypothesis_id": "idea-1",
                        "status": "failed", "metrics": {}, "evidence_files": []}]
    else:
        evidence = attempt_dir / "raw-observations.json"
        evidence.write_text(json.dumps({"delta": 1.25}), encoding="utf-8")
        experiments = [{"experiment_id": "exp-1", "hypothesis_id": "idea-1",
                        "status": "passed", "metrics": {"delta": 1.25},
                        "evidence_files": [evidence.name]}]
    response = {"stage": stage, "experiments": experiments, "usage": usage}
elif stage == "analysis":
    response = {"stage": stage,
                "claims": [{"claim_id": "claim-1", "claim_type": "empirical",
                            "statement": "the observed metric delta is positive",
                            "status": "supported", "evidence_ids": ["exp-1"]}],
                "usage": usage}
elif stage == "review":
    decision = "reject" if os.environ.get("SCIENTIST_REVIEW_REJECT") == "1" else "accept"
    response = {"stage": stage, "decision": decision,
                "claim_reviews": [{"claim_id": "claim-1", "decision": decision,
                                   "reason": "independent review decision"}],
                "usage": usage}
else:
    raise SystemExit(9)

Path(response_path).write_text(json.dumps(response), encoding="utf-8")
'''


class ScientistRuntimeTests(unittest.TestCase):
    def setUp(self):
        override = os.environ.get("ERL_TEST_TMP")
        if override:
            base = Path(override)
            base.mkdir(parents=True, exist_ok=True)
            self.root = base / f"scientist-{uuid.uuid4().hex}"
            self.root.mkdir()
        else:
            # Some managed Windows environments expose %TEMP% as readable but
            # deny writes from subprocesses.  Keep the fixture beside the
            # source tree unless the caller supplies ERL_TEST_TMP.
            base = SKILL_ROOT.parent / ".test-tmp-scientist"
            base.mkdir(parents=True, exist_ok=True)
            self.root = base / f"scientist-{uuid.uuid4().hex}"
            self.root.mkdir()
        self.worker = self.root / "worker.py"
        self.worker.write_text(textwrap.dedent(WORKER), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_project(self, name: str = "project") -> Path:
        project = self.root / name
        project.mkdir()
        command = [sys.executable, str(self.worker), "{stage}", "{request}",
                   "{response}", "{attempt_dir}"]
        stages = {}
        for stage in runtime.STAGES:
            stages[stage] = {
                "enabled": True,
                "instructions": f"Execute the {stage} contract using recorded evidence.",
                "max_attempts": 2,
                "backend": {
                    "kind": "command",
                    "command": command,
                    "timeout_sec": 30,
                    "env_allowlist": [
                        "SCIENTIST_FAIL_ONCE_STAGE", "SCIENTIST_FAIL_MARKER",
                        "SCIENTIST_EMPTY_LITERATURE", "SCIENTIST_EXPERIMENT_FAIL",
                        "SCIENTIST_REVIEW_REJECT",
                    ],
                },
            }
        plan = {
            "schema_version": 1,
            "plan_version": "test-v1",
            "research_question": "Does the intervention improve the frozen metric?",
            "claim_boundary": "Only the fixture workload and recorded evidence.",
            "frozen_input_files": [],
            "budget": {"max_stage_calls": 12, "max_wall_time_sec": 120,
                       "max_known_cost_usd": 1.0},
            "acceptance": {"required_claim_ids": ["claim-1"]},
            "stages": stages,
        }
        (project / "research-plan.json").write_text(
            json.dumps(plan, indent=2), encoding="utf-8")
        return project

    def test_real_command_pipeline_records_evidence_and_audits(self):
        project = self.make_project()
        report = runtime.execute(project)
        self.assertEqual("completed", report["execution_status"])
        self.assertTrue(report["acceptance"]["accepted"])
        self.assertEqual(5, report["usage"]["stage_calls_completed"])
        self.assertEqual(5, report["usage"]["model_calls"])
        self.assertTrue((project / "literature-ledger.jsonl").is_file())
        self.assertTrue((project / "experiment-ledger.jsonl").is_file())
        self.assertTrue((project / "claim-evidence-matrix.json").is_file())
        self.assertTrue((project / "reviews").is_dir())
        audit = runtime.audit(project)
        self.assertTrue(audit["valid"], audit)
        self.assertTrue(audit["accepted"], audit)

    def test_failed_command_is_preserved_and_resume_retries(self):
        project = self.make_project()
        marker = self.root / "failure.marker"
        env = {
            "SCIENTIST_FAIL_ONCE_STAGE": "literature",
            "SCIENTIST_FAIL_MARKER": str(marker),
        }
        with patch.dict(os.environ, env, clear=False):
            first = runtime.execute(project)
            self.assertEqual("paused_on_failure", first["execution_status"])
            state = runtime.read_json(runtime.state_path(project))
            self.assertEqual("failed", state["stages"]["literature"]["attempts"][0]["status"])
            second = runtime.execute(project)
        self.assertEqual("completed", second["execution_status"])
        attempts = runtime.read_json(runtime.state_path(project))["stages"]["literature"]["attempts"]
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0]["id"], attempts[1]["retry_of"])
        self.assertTrue((project / attempts[0]["path"] / "attempt-result.json").is_file())
        self.assertTrue(runtime.audit(project)["accepted"])

    def test_empty_literature_cannot_claim_novelty(self):
        project = self.make_project()
        with patch.dict(os.environ, {"SCIENTIST_EMPTY_LITERATURE": "1"}, clear=False):
            report = runtime.execute(project)
        self.assertEqual("completed", report["execution_status"])
        self.assertEqual("unknown", report["acceptance"]["novelty_status"])
        self.assertFalse(report["acceptance"]["novelty_claim_allowed"])
        novelty = json.loads((project / "novelty-assessment.json").read_text(encoding="utf-8"))
        self.assertEqual("unknown", novelty["status"])

    def test_failed_experiment_cannot_support_claim_or_pass_review(self):
        project = self.make_project()
        with patch.dict(os.environ, {"SCIENTIST_EXPERIMENT_FAIL": "1"}, clear=False):
            report = runtime.execute(project)
        self.assertEqual("completed", report["execution_status"])
        self.assertFalse(report["acceptance"]["accepted"])
        matrix = json.loads((project / "claim-evidence-matrix.json").read_text(encoding="utf-8"))
        self.assertEqual("inconclusive", matrix["claims"][0]["status"])
        audit = runtime.audit(project)
        self.assertTrue(audit["valid"], audit)
        self.assertFalse(audit["accepted"])

    def test_audit_rejects_tampered_derived_evidence(self):
        project = self.make_project()
        runtime.execute(project)
        matrix = json.loads((project / "claim-evidence-matrix.json").read_text(encoding="utf-8"))
        matrix["claims"][0]["statement"] = "tampered claim"
        (project / "claim-evidence-matrix.json").write_text(
            json.dumps(matrix), encoding="utf-8")
        audit = runtime.audit(project)
        self.assertFalse(audit["valid"])
        self.assertFalse(audit["accepted"])
        self.assertTrue(any("claim-evidence-matrix" in error for error in audit["errors"]))

    def test_protocol_lock_anchor_rejects_input_and_lock_rewrite(self):
        project = self.make_project()
        frozen = project / "frozen.txt"
        frozen.write_text("original\n", encoding="utf-8")
        plan_path = project / "research-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["frozen_input_files"] = ["frozen.txt"]
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        runtime.execute(project)
        frozen.write_text("replacement\n", encoding="utf-8")
        lock_path = project / "protocol-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["frozen_inputs"][0]["sha256"] = runtime.sha256_file(frozen)
        runtime.write_json(lock_path, lock)
        audit = runtime.audit(project)
        self.assertFalse(audit["valid"])
        self.assertFalse(audit["accepted"])
        self.assertTrue(any("protocol lock" in error for error in audit["errors"]))

    def test_audit_binds_output_to_attempt_result_and_event(self):
        project = self.make_project()
        runtime.execute(project)
        state_file = runtime.state_path(project)
        state = runtime.read_json(state_file)
        output_path = project / state["stages"]["analysis"]["output_path"]
        output = runtime.read_json(output_path)
        output["claims"][0]["statement"] = "replacement claim"
        runtime.write_json(output_path, output)
        state["stages"]["analysis"]["output_hash"] = runtime.sha256_file(output_path)
        runtime.write_json(state_file, state)
        runtime.write_json(project / "analysis-results.json", output)
        matrix = runtime.read_json(project / "claim-evidence-matrix.json")
        matrix["claims"] = output["claims"]
        matrix["response_hash"] = state["stages"]["analysis"]["output_hash"]
        runtime.write_json(project / "claim-evidence-matrix.json", matrix)
        audit = runtime.audit(project)
        self.assertFalse(audit["valid"])
        self.assertTrue(any("normalized response hash" in error or
                            "finished event" in error or
                            "bound to one completed attempt" in error
                            for error in audit["errors"]), audit)

    def test_audit_rejects_tampered_raw_experiment_evidence(self):
        project = self.make_project()
        runtime.execute(project)
        state = runtime.read_json(runtime.state_path(project))
        attempt = state["stages"]["experiment"]["attempts"][0]
        raw = project / attempt["path"] / "raw-observations.json"
        raw.write_text(json.dumps({"delta": -999}), encoding="utf-8")
        audit = runtime.audit(project)
        self.assertFalse(audit["valid"])
        self.assertTrue(any("experiment evidence" in error for error in audit["errors"]), audit)

    def test_independent_claim_rejection_blocks_acceptance(self):
        project = self.make_project()
        with patch.dict(os.environ, {"SCIENTIST_REVIEW_REJECT": "1"}, clear=False):
            report = runtime.execute(project)
        self.assertEqual("completed", report["execution_status"])
        self.assertFalse(report["acceptance"]["accepted"])
        audit = runtime.audit(project)
        self.assertTrue(audit["valid"], audit)
        self.assertFalse(audit["accepted"])

    def test_project_lock_blocks_a_second_writer(self):
        project = self.make_project()
        with runtime.project_lock(project, timeout_sec=0.05):
            with self.assertRaisesRegex(RuntimeError, "timed out acquiring"):
                with runtime.project_lock(project, timeout_sec=0.05):
                    self.fail("a second writer acquired the project lock")


if __name__ == "__main__":
    unittest.main()
