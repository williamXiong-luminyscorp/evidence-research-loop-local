from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evolution_controller", SKILL_ROOT / "scripts" / "evolution_controller.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_search", SKILL_ROOT / "scripts" / "audit_search.py")
auditor = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(auditor)
ENGINE_SPEC = importlib.util.spec_from_file_location(
    "solution_evolver", SKILL_ROOT / "scripts" / "solution_evolver.py")
engine = importlib.util.module_from_spec(ENGINE_SPEC)
ENGINE_SPEC.loader.exec_module(engine)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.deterministic_test_mode = patch.dict(
            os.environ, {controller.DETERMINISTIC_TEST_ENV: "1"})
        self.deterministic_test_mode.start()
        # ERL_TEST_TMP lets restricted Windows runs keep generated commands
        # below the legacy MAX_PATH limit while staying in an allowed root.
        override = os.environ.get("ERL_TEST_TMP")
        if override:
            base = Path(override)
            base.mkdir(parents=True, exist_ok=True)
            self.root = base / f"erl-{uuid.uuid4().hex}"
            self.root.mkdir()
        else:
            self.root = Path(tempfile.mkdtemp(prefix=f"erl-{uuid.uuid4().hex[:8]}-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        self.deterministic_test_mode.stop()

    def copy_task(self, template="python-code"):
        target = self.root / template
        shutil.copytree(SKILL_ROOT / "assets" / "task-templates" / template, target)
        return target

    def test_deterministic_generator_requires_explicit_test_mode(self):
        project = self.copy_task()
        config = json.loads((project / "task.json").read_text(encoding="utf-8"))
        with patch.dict(os.environ, {controller.DETERMINISTIC_TEST_ENV: ""}):
            with self.assertRaisesRegex(SystemExit, "test-only"):
                controller.validate_config(project, config)

    def test_codex_generator_requires_authentication(self):
        project = self.copy_task()
        config = json.loads((project / "task.json").read_text(encoding="utf-8"))
        config["generator"] = {
            "backend": "codex",
            "model": "test-model",
            "timeout_sec": 30,
            "roles": ["conservative", "mechanism", "repair"],
        }
        failed = {
            "return_code": 1,
            "stdout": "Not logged in",
            "stderr": "",
            "timed_out": False,
        }
        with patch.object(controller, "run_process", return_value=failed):
            with self.assertRaisesRegex(SystemExit, "Codex authentication is required"):
                controller.validate_config(project, config)

    def test_codex_login_status_on_stderr_is_accepted(self):
        project = self.copy_task()
        config = json.loads((project / "task.json").read_text(encoding="utf-8"))
        config["generator"] = {
            "backend": "codex",
            "model": "test-model",
            "timeout_sec": 30,
            "roles": ["conservative", "mechanism", "repair"],
        }
        logged_in = {
            "return_code": 0,
            "stdout": "",
            "stderr": "Logged in using personal access token\n",
            "timed_out": False,
        }
        with patch.object(controller, "run_process", return_value=logged_in):
            controller.validate_config(project, config)

    def test_multigeneration_budget_duplicate_failure_and_holdout(self):
        project = self.copy_task()
        report = controller.execute_search(project)
        state = report["search_state"]
        events = controller.load_events(project)
        self.assertEqual("completed", state["status"])
        self.assertEqual("generation_budget_exhausted", state["stop_reason"])
        self.assertEqual(6, state["generator_calls_started"])
        self.assertEqual(6, state["generator_calls_completed"])
        self.assertEqual(2, sum(row["type"] == "generation_closed" for row in events))
        self.assertEqual(1, sum(row["type"] == "generation_duplicate" for row in events))
        self.assertGreaterEqual(sum(row["type"] == "generation_evaluated" for row in events), 4)
        self.assertGreaterEqual(sum(row["type"] == "holdout_finished" for row in events), 2)
        archive = json.loads((project / "search-archive.json").read_text(encoding="utf-8"))
        self.assertTrue(archive["failures"])
        self.assertTrue(archive["behavior_similar_groups"])

    def test_requirement_change_can_start_from_failing_baseline(self):
        project = self.copy_task()
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        cfg["artifact"]["baseline"] = "fixtures/wrong_order.py"
        cfg["generator"]["deterministic_plan"] = [
            {
                "generation": 1,
                "slot": 1,
                "artifact": "fixtures/optimized.py",
                "hypothesis": "repair a baseline that fails a changed requirement",
                "mechanism": "restore sorting while keeping set-based deduplication",
            }
        ]
        cfg["generator"]["roles"] = ["repair"]
        cfg["budget"].update({
            "max_generations": 1,
            "candidates_per_generation": 1,
            "max_generator_calls": 1,
            "max_wall_time_sec": 120,
        })
        cfg["selection"].update({
            "allow_failing_baseline": True,
            "freeze_comparators": 0,
            "holdout_repeats": 1,
        })
        controller.save_json(project / "task.json", cfg)

        report = controller.execute_search(project)
        state = report["search_state"]
        search_archive = controller.load_json(project / "search-archive.json")
        events = controller.load_events(project)
        self.assertFalse(state["baseline_hard_pass"])
        self.assertTrue(state["allow_failing_baseline"])
        self.assertEqual("completed", state["status"])
        self.assertNotEqual(state["baseline_id"], state["selected_candidate_id"])
        self.assertIn(state["baseline_id"], {
            row["candidate_id"] for row in search_archive["failures"]})
        initialized = next(row for row in events if row["type"] == "search_initialized")
        self.assertTrue(initialized["allow_failing_baseline"])
        self.assertTrue(auditor.audit(project, False)["valid"])

    def test_failing_baseline_without_repair_stops_without_freezing_it(self):
        project = self.copy_task()
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        cfg["artifact"]["baseline"] = "fixtures/wrong_order.py"
        cfg["generator"]["deterministic_plan"] = [
            {
                "generation": 1,
                "slot": 1,
                "artifact": "fixtures/wrong_order.py",
                "hypothesis": "unchanged failing proposal",
                "mechanism": "duplicate the failing baseline",
            }
        ]
        cfg["generator"]["roles"] = ["repair"]
        cfg["budget"].update({
            "max_generations": 1,
            "candidates_per_generation": 1,
            "max_generator_calls": 1,
            "max_wall_time_sec": 120,
        })
        cfg["selection"].update({
            "allow_failing_baseline": True,
            "freeze_comparators": 0,
            "holdout_repeats": 1,
        })
        controller.save_json(project / "task.json", cfg)

        report = controller.execute_search(project)
        state = report["search_state"]
        self.assertEqual("no_eligible_candidate", state["status"])
        self.assertIsNone(state["selected_candidate_id"])
        self.assertIsNone(state["freeze_id"])
        self.assertTrue(any(
            row["type"] == "search_no_eligible_candidate"
            for row in controller.load_events(project)))
        audit = auditor.audit(project, False)
        self.assertTrue(audit["valid"])
        self.assertFalse(audit["accepted"])
        self.assertEqual(
            "no candidate passed the development hard gate",
            audit["holdout_completion"]["not_run_reason"],
        )
        terminal_count = sum(
            row["type"] == "search_no_eligible_candidate"
            for row in controller.load_events(project)
        )
        with patch.object(
                sys, "argv", ["evolution_controller.py", "resume", str(project)]), \
                patch("builtins.print"):
            with self.assertRaises(SystemExit) as stopped:
                controller.main()
        self.assertEqual(2, stopped.exception.code)
        resumed = controller.load_json(project / "search-state.json")
        self.assertEqual("no_eligible_candidate", resumed["status"])
        self.assertEqual(
            terminal_count,
            sum(
                row["type"] == "search_no_eligible_candidate"
                for row in controller.load_events(project)
            ),
        )
        self.assertTrue(auditor.audit(project, False)["valid"])

    def test_failing_baseline_is_never_a_frozen_comparator(self):
        project = self.copy_task()
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        cfg["artifact"]["baseline"] = "fixtures/wrong_order.py"
        cfg["generator"]["deterministic_plan"] = [
            {
                "generation": 1,
                "slot": 1,
                "artifact": "fixtures/optimized.py",
                "hypothesis": "repair sorting with a set",
                "mechanism": "built-in set plus sorted",
            },
            {
                "generation": 1,
                "slot": 2,
                "artifact": "fixtures/manual_set.py",
                "hypothesis": "repair sorting with explicit accumulation",
                "mechanism": "manual set accumulation then sort",
            },
        ]
        cfg["generator"]["roles"] = ["mechanism", "conservative"]
        cfg["budget"].update({
            "max_generations": 1,
            "candidates_per_generation": 2,
            "max_generator_calls": 2,
            "max_wall_time_sec": 120,
        })
        cfg["selection"].update({
            "allow_failing_baseline": True,
            "freeze_comparators": 1,
            "holdout_repeats": 1,
        })
        controller.save_json(project / "task.json", cfg)

        report = controller.execute_search(project)
        state = report["search_state"]
        self.assertEqual("completed", state["status"])
        self.assertNotIn(state["baseline_id"], state["comparison_candidate_ids"])
        self.assertEqual(2, len(state["comparison_candidate_ids"]))
        archive = controller.load_json(project / "archive.json")
        by_id = {row["id"]: row for row in archive["candidates"]}
        self.assertTrue(all(
            controller.latest_dev(by_id[candidate_id])["hard_pass"]
            for candidate_id in state["comparison_candidate_ids"]
        ))
        audit = auditor.audit(project, False)
        self.assertTrue(audit["valid"])
        self.assertTrue(audit["accepted"])

    def test_scientist_mode_template_selects_predefined_full_structure(self):
        project = self.copy_task("scientist-mode")
        report = controller.execute_search(project)
        self.assertEqual("completed", report["search_state"]["status"])
        selected_id = report["selected_candidate_id"]
        archive = json.loads((project / "archive.json").read_text(encoding="utf-8"))
        by_id = {row["id"]: row for row in archive["candidates"]}
        selected_config = json.loads(
            (project / by_id[selected_id]["artifact_path"]).read_text(encoding="utf-8")
        )
        self.assertTrue(all(selected_config.values()))

        observed = {}
        for candidate in archive["candidates"]:
            config = json.loads((project / candidate["artifact_path"]).read_text(encoding="utf-8"))
            disabled = [name for name, enabled in config.items() if not enabled]
            if not disabled:
                label = "full"
            elif len(disabled) == len(config):
                label = "baseline"
            elif len(disabled) == 1:
                label = f"no_{disabled[0]}"
            else:
                continue
            observed[label] = candidate["evaluations"]["dev"][0]["metrics"]["mean_structural_coverage_score"]
        self.assertEqual(
            {
                "baseline": 0.0,
                "full": 1.0,
                "no_ideation": 0.0,
                "no_literature": 0.25,
                "no_tree_manager": 0.25,
                "no_analysis": 0.5,
                "no_paper_review": 0.75,
            },
            observed,
        )
        audit = auditor.audit(project, check_manifest=False)
        self.assertTrue(audit["valid"])
        self.assertTrue(audit["accepted"])

    def test_each_stop_condition(self):
        project = self.copy_task()
        config = controller.load_config(project)
        base = {
            "current_generation": 1, "generator_calls_started": 0,
            "accumulated_active_sec": 0.0, "known_cost_usd": 0.0,
            "no_improvement_generations": 0,
        }
        cases = [
            ({"current_generation": config["budget"]["max_generations"] + 1},
             "generation_budget_exhausted"),
            ({"generator_calls_started": config["budget"]["max_generator_calls"]},
             "generator_call_budget_exhausted"),
            ({"accumulated_active_sec": config["budget"]["max_wall_time_sec"]},
             "wall_time_budget_exhausted"),
            ({"known_cost_usd": config["budget"]["max_known_cost_usd"]},
             "known_cost_budget_exhausted"),
            ({"no_improvement_generations": config["budget"]["no_improvement_generations"]},
             "no_improvement_stop"),
        ]
        for changes, expected in cases:
            state = {**base, **changes}
            self.assertEqual(expected, controller.should_stop(project, config, state))

    def test_generator_timeout_is_a_recorded_failure(self):
        project = self.copy_task()
        slow = project / "slow.py"
        slow.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        cfg["public_context_files"].append("slow.py")
        cfg["contract"]["frozen_input_files"].append("slow.py")
        cfg["generator"] = {
            "backend": "command", "command": [sys.executable, "public-context/slow.py"],
            "timeout_sec": 0.05, "counts_as_model_call": False, "roles": ["exploratory"]}
        cfg["budget"].update({"max_generations": 1, "candidates_per_generation": 1,
                              "max_generator_calls": 1, "max_wall_time_sec": 30})
        (project / "task.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        report = controller.execute_search(project)
        events = controller.load_events(project)
        failures = [row for row in events if row["type"] == "generation_failed"]
        self.assertEqual("completed", report["search_state"]["status"])
        self.assertEqual(1, len(failures))
        self.assertEqual("generator_timeout", failures[0]["failure_class"])
        self.assertEqual(1, report["search_state"]["generator_calls_completed"])
        self.assertEqual(0, report["search_state"]["model_calls_completed"])

    def test_unknown_started_call_is_not_retried(self):
        project = self.copy_task()
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        marker = project / "must-not-run.txt"
        cfg["generator"] = {
            "backend": "command", "command": [sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('called')"],
            "timeout_sec": 5, "counts_as_model_call": True, "roles": ["conservative"]}
        cfg["budget"].update({"max_generations": 1, "candidates_per_generation": 1,
                              "max_generator_calls": 1, "max_wall_time_sec": 30})
        (project / "task.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        config = controller.load_config(project)
        state = controller.initialize_project(project, config)
        parent = state["baseline_id"]
        request_id, workspace = controller.prepare_workspace(project, config, 1, 1, "conservative", parent)
        state["generator_calls_started"] = 1
        state["model_calls_started"] = 1
        controller.save_json(controller.state_path(project), state)
        controller.append_event(project, "generation_started", request_id=request_id, generation=1,
                                slot=1, role="conservative", parent_id=parent,
                                workspace=str(workspace.relative_to(project)), backend="command",
                                counts_as_model_call=True,
                                prompt_sha256=controller.sha256_file(workspace / "prompt.txt"))
        controller.run_slot(project, config, state, 1, 1, "conservative")
        events = controller.request_events(project, request_id)
        self.assertFalse(marker.exists())
        self.assertEqual("generation_abandoned_unknown_outcome", events[-1]["type"])
        saved = json.loads(controller.state_path(project).read_text(encoding="utf-8"))
        self.assertEqual(1, saved["generator_calls_started"])
        self.assertEqual(0, saved["generator_calls_completed"])

    def test_completed_call_result_is_consumed_once_on_resume(self):
        project = self.copy_task()
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        cfg["generator"] = {"backend": "command", "command": ["missing-command"],
                            "timeout_sec": 5, "counts_as_model_call": True,
                            "response_contains_artifact": True, "roles": ["conservative"]}
        cfg["budget"].update({"max_generations": 1, "candidates_per_generation": 1,
                              "max_generator_calls": 1, "max_wall_time_sec": 30})
        (project / "task.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        config = controller.load_config(project)
        state = controller.initialize_project(project, config)
        parent = state["baseline_id"]
        request_id, workspace = controller.prepare_workspace(project, config, 1, 1, "conservative", parent)
        optimized = (project / "fixtures" / "optimized.py").read_text(encoding="utf-8")
        controller.save_json(workspace / "response.json", {
            "hypothesis": "recover completed output", "mechanism": "set plus sorted", "summary": "fixture",
            "artifact": optimized})
        call_result = {"return_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                       "duration_sec": 0.2, "command": ["already-finished"], "model_call": True,
                       "usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": None}
        controller.save_json(workspace / "call-result.json", call_result)
        state["generator_calls_started"] = 1
        state["model_calls_started"] = 1
        controller.save_json(controller.state_path(project), state)
        controller.append_event(project, "generation_started", request_id=request_id, generation=1,
                                slot=1, role="conservative", parent_id=parent,
                                workspace=str(workspace.relative_to(project)), backend="command",
                                counts_as_model_call=True,
                                prompt_sha256=controller.sha256_file(workspace / "prompt.txt"))
        controller.run_slot(project, config, state, 1, 1, "conservative")
        controller.run_slot(project, config, json.loads(controller.state_path(project).read_text()),
                            1, 1, "conservative")
        events = controller.request_events(project, request_id)
        self.assertEqual(1, sum(row["type"] == "generation_call_finished" for row in events))
        self.assertEqual(1, sum(row["type"] == "generation_evaluated" for row in events))
        saved = json.loads(controller.state_path(project).read_text(encoding="utf-8"))
        self.assertEqual(1, saved["generator_calls_completed"])
        self.assertEqual(1, saved["model_calls_completed"])
        self.assertEqual(1, saved["unknown_cost_calls"])

    def make_single_slot_task(self, name="python-code", max_calls=1):
        project = self.copy_task(name)
        cfg = json.loads((project / "task.json").read_text(encoding="utf-8"))
        cfg["budget"].update({"max_generations": 1, "candidates_per_generation": 1,
                              "max_generator_calls": max_calls, "max_wall_time_sec": 120})
        cfg["generator"]["roles"] = ["conservative"]
        cfg["selection"]["holdout_repeats"] = 1
        (project / "task.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return project, controller.load_config(project)

    def save_completed_pending_call(self, project, cfg):
        state = controller.initialize_project(project, cfg)
        request_id, workspace = controller.prepare_workspace(
            project, cfg, 1, 1, "conservative", state["baseline_id"])
        shutil.copy2(project / "fixtures" / "optimized.py", workspace / "candidate.py")
        controller.save_json(workspace / "response.json", {
            "hypothesis": "recover durable response", "mechanism": "set plus sorted",
            "summary": "fault fixture"})
        call = {"return_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                "duration_sec": 0.1, "command": ["already-finished"], "model_call": False,
                "usage": None, "cost_usd": 0.0}
        controller.save_json(workspace / "call-result.json", call)
        controller.append_event(
            project, "generation_started", request_id=request_id, generation=1, slot=1,
            role="conservative", parent_id=state["baseline_id"],
            workspace=str(workspace.relative_to(project)), backend="deterministic",
            counts_as_model_call=False, prompt_sha256=controller.sha256_file(workspace / "prompt.txt"))
        controller.reconcile_call_accounting(project, state)
        return state, request_id, workspace, call

    def test_resume_consumes_durable_result_before_call_cap(self):
        project, cfg = self.make_single_slot_task()
        _, request_id, _, _ = self.save_completed_pending_call(project, cfg)
        with patch.object(controller, "invoke_generator", side_effect=AssertionError("must not generate")):
            report = controller.execute_search(project)
        event_types = [row["type"] for row in controller.request_events(project, request_id)]
        self.assertEqual("completed", report["search_state"]["status"])
        self.assertEqual(1, report["search_state"]["generator_calls_completed"])
        self.assertIn("generation_evaluated", event_types)
        self.assertEqual(2, len(controller.load_json(project / "archive.json")["candidates"]))
        self.assertTrue(auditor.audit(project, False)["valid"])

    def test_holdout_launch_failure_is_incomplete_and_resume_retries(self):
        project, _ = self.make_single_slot_task()
        original = controller.engine_call

        def fail_holdout(path, *args, **kwargs):
            if args and args[0] == "run" and "holdout" in args:
                raise RuntimeError("injected launch failure before run.json")
            return original(path, *args, **kwargs)

        with patch.object(controller, "engine_call", side_effect=fail_holdout):
            first = controller.execute_search(project)
        first_audit = auditor.audit(project, False)
        self.assertEqual("holdout_incomplete", first["search_state"]["status"])
        self.assertFalse(first_audit["valid"])
        self.assertEqual(0, first_audit["holdout_run_count"])
        resumed = controller.execute_search(project)
        resumed_audit = auditor.audit(project, False)
        self.assertEqual("completed", resumed["search_state"]["status"])
        self.assertTrue(resumed_audit["valid"])
        self.assertEqual(2, resumed_audit["holdout_run_count"])

    def test_event_first_accounting_recovers_from_state_save_crash(self):
        project, cfg = self.make_single_slot_task(max_calls=2)
        state, request_id, workspace, call = self.save_completed_pending_call(project, cfg)
        original_save = controller.save_json
        failed = {"done": False}

        def fail_state_save(path, value):
            if path == controller.state_path(project) and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("injected crash after event append, before state snapshot")
            return original_save(path, value)

        with patch.object(controller, "save_json", side_effect=fail_state_save):
            with self.assertRaises(RuntimeError):
                controller.account_finished_call(project, state, request_id, 1, call, workspace)
        report = controller.execute_search(project)
        events = controller.load_events(project)
        self.assertEqual(1, report["search_state"]["generator_calls_started"])
        self.assertEqual(1, report["search_state"]["generator_calls_completed"])
        self.assertEqual(1, sum(row["type"] == "generation_call_finished" for row in events))
        self.assertTrue(auditor.audit(project, False)["valid"])

    def test_total_wall_time_is_hard_and_includes_generator(self):
        project, cfg = self.make_single_slot_task()
        slow = project / "slow.py"
        slow.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
        cfg["public_context_files"].append("slow.py")
        cfg["contract"]["frozen_input_files"].append("slow.py")
        cfg["generator"] = {"backend": "command", "command": [sys.executable, "public-context/slow.py"],
                            "timeout_sec": 5, "counts_as_model_call": False,
                            "roles": ["conservative"]}
        cfg["budget"]["max_wall_time_sec"] = 1
        controller.save_json(project / "task.json", cfg)
        started = time.perf_counter()
        report = controller.execute_search(project)
        elapsed = time.perf_counter() - started
        call = controller.load_json(project / "generator-workspaces" / "g001-s001" / "call-result.json")
        self.assertTrue(call["timed_out"], call)
        self.assertLess(call["duration_sec"], 1.5)
        # One-time protocol initialization and baseline calibration are outside
        # max_wall_time_sec.  Their host-dependent process startup cost is not
        # part of the hard search budget; keep only a broad anti-hang assertion.
        self.assertLess(elapsed, 10.0)
        self.assertLess(report["search_state"]["accumulated_active_sec"], 1.5)
        self.assertEqual("wall_time_budget_exhausted", report["search_state"]["stop_reason"])
        self.assertEqual("holdout_incomplete", report["search_state"]["status"])
        self.assertFalse(auditor.audit(project, False)["valid"])

    def test_freeze_timeout_is_incomplete_and_resume_retries(self):
        project, _ = self.make_single_slot_task()
        original = controller.engine_call
        injected = {"done": False}

        def fail_freeze(path, *args, **kwargs):
            if args and args[0] == "freeze" and not injected["done"]:
                injected["done"] = True
                raise TimeoutError("injected timeout at freeze boundary")
            return original(path, *args, **kwargs)

        with patch.object(controller, "engine_call", side_effect=fail_freeze):
            first = controller.execute_search(project)
        self.assertEqual("holdout_incomplete", first["search_state"]["status"])
        self.assertIsNone(first["search_state"].get("freeze_id"))
        self.assertTrue(any(row["type"] == "freeze_controller_error"
                            for row in controller.load_events(project)))
        self.assertFalse(auditor.audit(project, False)["valid"])

        resumed = controller.execute_search(project)
        self.assertEqual("completed", resumed["search_state"]["status"])
        self.assertTrue(auditor.audit(project, False)["valid"])

    def test_mid_holdout_interruption_is_terminal_and_retry_is_linked(self):
        project, _ = self.make_single_slot_task()
        original_engine_call = controller.engine_call
        original_append = engine.append_jsonl
        injected = {"done": False}

        def interrupt_after_holdout_start(path, *args, **kwargs):
            if not injected["done"] and args and args[0] == "run" and "holdout" in args:
                injected["done"] = True
                parsed = engine.build_parser().parse_args(list(args))

                def crash_after_start(log_path, row):
                    original_append(log_path, row)
                    if row.get("event") == "started":
                        raise RuntimeError("injected interruption after run.json and access start")

                with patch.object(engine, "append_jsonl", side_effect=crash_after_start):
                    engine.cmd_run(parsed)
            return original_engine_call(path, *args, **kwargs)

        with patch.object(controller, "engine_call", side_effect=interrupt_after_holdout_start):
            first = controller.execute_search(project)
        self.assertEqual("holdout_incomplete", first["search_state"]["status"])
        first_audit = auditor.audit(project, False)
        self.assertFalse(first_audit["valid"])
        self.assertEqual(1, first_audit["terminal_attempt_count"])

        resumed = controller.execute_search(project)
        audited = auditor.audit(project, False)
        self.assertEqual("completed", resumed["search_state"]["status"])
        self.assertTrue(audited["valid"])
        self.assertTrue(audited["accepted"])
        self.assertEqual(2, audited["holdout_completion"]["completed_count"])
        self.assertEqual(1, audited["terminal_attempt_count"])
        self.assertIn(audited["terminal_attempts"][0]["retried_by_run_id"],
                      {row["run_id"] for row in audited["runs"]})

    def test_wall_timeout_terminates_descendant_process_tree(self):
        project, cfg = self.make_single_slot_task()
        marker = project / "child-survived.txt"
        child = ("import time; from pathlib import Path; time.sleep(2); "
                 f"Path({str(marker)!r}).write_text('child completed after timeout')")
        source = ("import subprocess,sys,time\n"
                  f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                  "time.sleep(4)\n")
        (project / "parent.py").write_text(source, encoding="utf-8")
        cfg["public_context_files"].append("parent.py")
        cfg["contract"]["frozen_input_files"].append("parent.py")
        cfg["generator"] = {
            "backend": "command", "command": [sys.executable, "public-context/parent.py"],
            "timeout_sec": 5, "counts_as_model_call": False, "roles": ["conservative"]}
        cfg["budget"]["max_wall_time_sec"] = 1
        controller.save_json(project / "task.json", cfg)
        report = controller.execute_search(project)
        call = controller.load_json(project / "generator-workspaces" / "g001-s001" / "call-result.json")
        time.sleep(2.2)
        self.assertTrue(call["timed_out"])
        self.assertTrue(call["process_tree_terminated"], call)
        self.assertLess(call["duration_sec"], 1.3)
        self.assertLess(report["search_state"]["accumulated_active_sec"], 1.3)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
