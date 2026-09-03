from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workflow_engine import (
    ModuleDefinition,
    WorkflowRegistry,
    WorkflowRunner,
    WorkflowValidationError,
    resolve_value,
    validate_workflow,
)


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.registry = WorkflowRegistry()
        self.registry.register(
            ModuleDefinition(
                type="test.record",
                name="记录",
                category="测试",
                description="记录参数",
                fields=(),
                handler=lambda _context, params: self.calls.append(params),
            )
        )

    def test_resolve_value_preserves_exact_variable_type(self) -> None:
        variables = {"columns": ["医嘱名称", "频次"], "number": "ZY1"}
        self.assertEqual(resolve_value("${columns}", variables), variables["columns"])
        self.assertEqual(resolve_value("患者-${number}", variables), "患者-ZY1")

    def test_validate_rejects_unknown_module(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            validate_workflow(
                {"name": "错误", "steps": [{"type": "unknown", "params": {}}]},
                self.registry,
            )

    def test_runner_respects_step_order_and_disabled_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            studio = Path(temporary) / "studio"
            studio.mkdir()
            runner = WorkflowRunner(self.registry, studio)
            config = {
                "name": "测试流程",
                "variables": {"value": "A"},
                "settings": {"outputDir": "../output"},
                "steps": [
                    {"id": "one", "type": "test.record", "params": {"value": "${value}"}},
                    {"id": "skip", "type": "test.record", "enabled": False, "params": {"value": "B"}},
                    {"id": "three", "type": "test.record", "params": {"value": "C"}},
                ],
            }
            result = runner.run(config, {"value": "运行值"}, lambda *_args: None)
            self.assertEqual(self.calls, [{"value": "运行值"}, {"value": "C"}])
            self.assertEqual(result["workflow"], "测试流程")


if __name__ == "__main__":
    unittest.main()
