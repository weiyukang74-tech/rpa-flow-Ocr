"""Small, deterministic JSON workflow engine for the local RPA studio."""

from __future__ import annotations

import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class WorkflowValidationError(ValueError):
    pass


class WorkflowExecutionError(RuntimeError):
    pass


class WorkflowCancelled(WorkflowExecutionError):
    pass


@dataclass(frozen=True)
class ModuleDefinition:
    type: str
    name: str
    category: str
    description: str
    fields: tuple[dict[str, Any], ...]
    handler: Callable[["ExecutionContext", dict[str, Any]], Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "fields": deepcopy(list(self.fields)),
        }


@dataclass
class ExecutionContext:
    workflow_name: str
    variables: dict[str, Any]
    output_dir: Path
    emit_callback: Callable[[str, str, dict[str, Any] | None], None]
    cancel_event: threading.Event
    state: dict[str, Any] = field(default_factory=dict)

    def emit(
        self,
        level: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.emit_callback(level, message, data)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise WorkflowCancelled("任务已由用户取消")

    def wait(self, seconds: float) -> None:
        if self.cancel_event.wait(max(0.0, seconds)):
            raise WorkflowCancelled("任务已由用户取消")


VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


def variable_value(variables: dict[str, Any], path: str) -> Any:
    current: Any = variables
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise WorkflowExecutionError(f"未提供变量：{path}")
        current = current[part]
    return current


def resolve_value(value: Any, variables: dict[str, Any]) -> Any:
    """Resolve ${variable} recursively while preserving exact-value types."""

    if isinstance(value, list):
        return [resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, variables) for key, item in value.items()}
    if not isinstance(value, str):
        return value

    exact = VARIABLE_PATTERN.fullmatch(value)
    if exact:
        return deepcopy(variable_value(variables, exact.group(1)))

    def replace(match: re.Match[str]) -> str:
        return str(variable_value(variables, match.group(1)))

    return VARIABLE_PATTERN.sub(replace, value)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ModuleDefinition] = {}

    def register(self, definition: ModuleDefinition) -> None:
        if definition.type in self._definitions:
            raise ValueError(f"模块重复注册：{definition.type}")
        self._definitions[definition.type] = definition

    def get(self, module_type: str) -> ModuleDefinition:
        try:
            return self._definitions[module_type]
        except KeyError as exc:
            raise WorkflowValidationError(f"未知模块：{module_type}") from exc

    def public_modules(self) -> list[dict[str, Any]]:
        return [
            definition.public_dict()
            for definition in sorted(
                self._definitions.values(),
                key=lambda item: (item.category, item.name),
            )
        ]


def validate_workflow(config: Any, registry: WorkflowRegistry) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise WorkflowValidationError("工作流必须是 JSON 对象")
    name = str(config.get("name") or "").strip()
    if not name:
        raise WorkflowValidationError("工作流名称不能为空")
    steps = config.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowValidationError("工作流至少需要一个步骤")

    seen_ids: set[str] = set()
    normalized_steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raise WorkflowValidationError(f"第 {index} 步不是对象")
        step = deepcopy(raw_step)
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            step_id = f"step-{uuid.uuid4().hex[:10]}"
        if step_id in seen_ids:
            raise WorkflowValidationError(f"步骤 ID 重复：{step_id}")
        seen_ids.add(step_id)
        module_type = str(step.get("type") or "").strip()
        definition = registry.get(module_type)
        params = step.get("params", {})
        if not isinstance(params, dict):
            raise WorkflowValidationError(f"第 {index} 步的参数必须是对象")
        step.update(
            {
                "id": step_id,
                "type": module_type,
                "name": str(step.get("name") or definition.name),
                "enabled": bool(step.get("enabled", True)),
                "params": params,
            }
        )
        normalized_steps.append(step)

    normalized = deepcopy(config)
    normalized["schemaVersion"] = 1
    normalized["name"] = name
    normalized["steps"] = normalized_steps
    normalized.setdefault("variables", {})
    normalized.setdefault("settings", {})
    if not isinstance(normalized["variables"], dict):
        raise WorkflowValidationError("variables 必须是对象")
    if not isinstance(normalized["settings"], dict):
        raise WorkflowValidationError("settings 必须是对象")
    return normalized


class WorkflowRunner:
    def __init__(self, registry: WorkflowRegistry, studio_root: Path) -> None:
        self.registry = registry
        self.studio_root = studio_root.resolve()

    def run(
        self,
        raw_config: dict[str, Any],
        runtime_variables: dict[str, Any] | None,
        emit: Callable[[str, str, dict[str, Any] | None], None],
        until_step_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        config = validate_workflow(raw_config, self.registry)
        variables = deepcopy(config.get("variables", {}))
        variables.update(runtime_variables or {})
        output_setting = str(
            config.get("settings", {}).get("outputDir") or "output"
        )
        output_dir = (self.studio_root / output_setting).resolve()
        allowed_output_root = self.studio_root
        try:
            output_dir.relative_to(allowed_output_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                f"输出目录必须位于独立项目目录 {allowed_output_root} 内"
            ) from exc
        output_dir.mkdir(parents=True, exist_ok=True)

        context = ExecutionContext(
            workflow_name=config["name"],
            variables=variables,
            output_dir=output_dir,
            emit_callback=emit,
            cancel_event=cancel_event or threading.Event(),
        )
        context.state["run_started_at"] = time.time()
        context.state["timestamp"] = time.strftime("%Y%m%d_%H%M%S")
        context.state["screenshots"] = []
        context.state["marked_screenshots"] = []

        enabled_steps = [step for step in config["steps"] if step["enabled"]]
        emit("info", f"开始执行：{config['name']}，共 {len(enabled_steps)} 个启用步骤", None)
        for index, step in enumerate(enabled_steps, start=1):
            context.check_cancelled()
            definition = self.registry.get(step["type"])
            params = resolve_value(step["params"], variables)
            emit(
                "step",
                f"[{index}/{len(enabled_steps)}] {step['name']}",
                {"stepId": step["id"], "moduleType": step["type"]},
            )
            started = time.monotonic()
            try:
                result = definition.handler(context, params)
            except WorkflowCancelled:
                raise
            except Exception as exc:
                raise WorkflowExecutionError(
                    f"步骤“{step['name']}”执行失败：{exc}"
                ) from exc
            elapsed = time.monotonic() - started
            context.check_cancelled()
            context.state.setdefault("step_results", {})[step["id"]] = result
            emit("success", f"完成：{step['name']}（{elapsed:.2f}s）", None)
            if until_step_id and step["id"] == until_step_id:
                emit("info", "已运行到选定步骤，后续步骤未执行", None)
                break

        return {
            "workflow": config["name"],
            "outputDir": str(output_dir),
            "screenshots": [str(path) for path in context.state["screenshots"]],
            "markedScreenshots": [
                str(path) for path in context.state["marked_screenshots"]
            ],
            "stepResults": context.state.get("step_results", {}),
        }


class RunManager:
    """Owns one background interactive-desktop run at a time."""

    def __init__(self, runner: WorkflowRunner) -> None:
        self.runner = runner
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "status": "idle",
            "runId": None,
            "logs": [],
            "result": None,
            "error": None,
        }
        self._cancel_event = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._snapshot)

    def start(
        self,
        config: dict[str, Any],
        variables: dict[str, Any] | None = None,
        until_step_id: str | None = None,
    ) -> str:
        validate_workflow(config, self.runner.registry)
        with self._lock:
            if self._snapshot["status"] in {"running", "cancelling"}:
                raise WorkflowExecutionError("已有任务正在运行")
            run_id = uuid.uuid4().hex
            self._snapshot = {
                "status": "running",
                "runId": run_id,
                "startedAt": time.time(),
                "finishedAt": None,
                "logs": [],
                "result": None,
                "error": None,
            }
            self._cancel_event = threading.Event()

        thread = threading.Thread(
            target=self._run_thread,
            args=(run_id, deepcopy(config), deepcopy(variables or {}), until_step_id),
            name=f"rpa-run-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return run_id

    def cancel(self) -> bool:
        with self._lock:
            if self._snapshot["status"] not in {"running", "cancelling"}:
                return False
            self._cancel_event.set()
            self._snapshot["status"] = "cancelling"
            run_id = self._snapshot["runId"]
        self._emit(run_id, "warning", "已收到取消请求，正在安全停止当前步骤", None)
        return True

    def _emit(
        self,
        run_id: str,
        level: str,
        message: str,
        data: dict[str, Any] | None,
    ) -> None:
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "data": data,
        }
        with self._lock:
            if self._snapshot["runId"] == run_id:
                self._snapshot["logs"].append(entry)
                self._snapshot["logs"] = self._snapshot["logs"][-500:]

    def _run_thread(
        self,
        run_id: str,
        config: dict[str, Any],
        variables: dict[str, Any],
        until_step_id: str | None,
    ) -> None:
        pythoncom = None
        try:
            try:
                import pythoncom as pythoncom_module

                pythoncom = pythoncom_module
                pythoncom.CoInitialize()
            except ImportError:
                pass

            result = self.runner.run(
                config,
                variables,
                lambda level, message, data: self._emit(
                    run_id, level, message, data
                ),
                until_step_id,
                self._cancel_event,
            )
            with self._lock:
                if self._snapshot["runId"] == run_id:
                    self._snapshot.update(
                        {
                            "status": "succeeded",
                            "finishedAt": time.time(),
                            "result": result,
                        }
                    )
        except WorkflowCancelled as exc:
            self._emit(run_id, "warning", str(exc), None)
            with self._lock:
                if self._snapshot["runId"] == run_id:
                    self._snapshot.update(
                        {
                            "status": "cancelled",
                            "finishedAt": time.time(),
                            "error": None,
                        }
                    )
        except Exception as exc:
            self._emit(run_id, "error", str(exc), None)
            with self._lock:
                if self._snapshot["runId"] == run_id:
                    self._snapshot.update(
                        {
                            "status": "failed",
                            "finishedAt": time.time(),
                            "error": str(exc),
                        }
                    )
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()
