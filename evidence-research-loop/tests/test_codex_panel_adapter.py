from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
import uuid
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = (
    SKILL_ROOT / "assets" / "task-templates" / "scientist-runtime" /
    "codex_panel_adapter.py"
)
SPEC = importlib.util.spec_from_file_location("codex_panel_adapter", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def assert_strict_objects(test: unittest.TestCase, schema: object) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            test.assertIs(False, schema.get("additionalProperties"))
        for value in schema.values():
            assert_strict_objects(test, value)
    elif isinstance(schema, list):
        for value in schema:
            assert_strict_objects(test, value)


class CodexPanelAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        base = SKILL_ROOT.parent / ".test-tmp-panel-adapter"
        base.mkdir(parents=True, exist_ok=True)
        self.project = base / uuid.uuid4().hex
        self.project.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.project, ignore_errors=True)

    def test_bundled_config_is_valid(self) -> None:
        source = ADAPTER_PATH.parent / "panel-config.json"
        shutil.copyfile(source, self.project / "panel-config.json")
        config = adapter.load_config(self.project)
        self.assertEqual(10, config["absolute_max_model_calls"])
        self.assertEqual(
            {"ideation", "literature", "experiment", "analysis", "review"},
            set(config["stages"]),
        )

    def test_default_schemas_are_strict(self) -> None:
        for stage in ("ideation", "literature", "experiment", "analysis", "review"):
            assert_strict_objects(self, adapter.schema_for(stage))

    def test_frozen_input_boundary_and_size(self) -> None:
        (self.project / "context.md").write_text("bounded evidence", encoding="utf-8")
        (self.project / "research-plan.json").write_text(json.dumps({
            "frozen_input_files": ["context.md"]
        }), encoding="utf-8")
        rows = adapter.load_frozen_inputs(self.project, {"max_frozen_input_bytes": 100})
        self.assertEqual("context.md", rows[0]["path"])
        self.assertEqual("bounded evidence", rows[0]["content"])

        (self.project / "research-plan.json").write_text(json.dumps({
            "frozen_input_files": ["../outside.txt"]
        }), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            adapter.load_frozen_inputs(self.project, {"max_frozen_input_bytes": 100})

    def test_usage_parser(self) -> None:
        events = "\n".join([
            json.dumps({"usage": {"input_tokens": 12, "output_tokens": 3}}),
            json.dumps({"usage": {"input_tokens": 20, "output_tokens": 5}}),
        ])
        self.assertEqual((20, 5), adapter.token_usage(events))

    def test_failed_or_interrupted_call_start_consumes_budget(self) -> None:
        attempt = self.project / "scientist-runs" / "literature" / "attempt-001"
        attempt.mkdir(parents=True)
        (attempt / "codex-panel-01-prompt.txt").write_text(
            "durable start", encoding="utf-8"
        )
        (self.project / "scientist-state.json").write_text(
            json.dumps({"model_calls": 0}), encoding="utf-8"
        )
        self.assertEqual(1, adapter.durable_model_calls(self.project))

        (self.project / "scientist-state.json").write_text(
            json.dumps({"model_calls": 3}), encoding="utf-8"
        )
        self.assertEqual(3, adapter.durable_model_calls(self.project))


if __name__ == "__main__":
    unittest.main()
