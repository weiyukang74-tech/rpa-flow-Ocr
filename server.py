"""Local-only HTTP server for configuring and running desktop RPA workflows."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from rpa_modules import build_registry
from workflow_engine import (
    RunManager,
    WorkflowExecutionError,
    WorkflowRunner,
    WorkflowValidationError,
    validate_workflow,
)


ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
WEB_ROOT = ROOT / "web"
CONFIG_ROOT = ROOT / "configs"
DEFAULT_CONFIG_PATH = CONFIG_ROOT / "default.json"
CUSTOM_MODULES_PATH = CONFIG_ROOT / "custom-modules.json"
HOST = "127.0.0.1"
PORT = int(os.getenv("RPA_STUDIO_PORT", "8765"))
MAX_REQUEST_BYTES = 2 * 1024 * 1024

REGISTRY = build_registry()
RUNNER = WorkflowRunner(REGISTRY, ROOT)
RUN_MANAGER = RunManager(RUNNER)


CONFIG_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def config_path(config_id: str) -> Path:
    if not CONFIG_ID_PATTERN.fullmatch(config_id):
        raise WorkflowValidationError("配置 ID 无效")
    return CONFIG_ROOT / f"{config_id}.json"


def list_configs() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in CONFIG_ROOT.glob("*.json"):
        if path.name == CUSTOM_MODULES_PATH.name:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                config = validate_workflow(json.load(handle), REGISTRY)
        except Exception:
            continue
        result.append(
            {
                "id": path.stem,
                "name": config["name"],
                "stepCount": len(config["steps"]),
                "updatedAt": path.stat().st_mtime,
            }
        )
    return sorted(result, key=lambda item: (-item["updatedAt"], item["name"]))


def read_config(config_id: str = "default") -> dict[str, Any]:
    path = config_path(config_id)
    with path.open("r", encoding="utf-8") as handle:
        return validate_workflow(json.load(handle), REGISTRY)


def write_config(config: dict[str, Any], config_id: str | None = None) -> tuple[str, dict[str, Any]]:
    normalized = validate_workflow(config, REGISTRY)
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    resolved_id = config_id or f"workflow-{uuid.uuid4().hex[:12]}"
    path = config_path(resolved_id)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(normalized, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)
    return resolved_id, normalized


def delete_config(config_id: str) -> None:
    configs = list_configs()
    if len(configs) <= 1:
        raise WorkflowValidationError("至少需要保留一个工作流配置")
    path = config_path(config_id)
    if not path.is_file():
        raise FileNotFoundError(path)
    path.unlink()


def read_custom_modules() -> list[dict[str, Any]]:
    if not CUSTOM_MODULES_PATH.is_file():
        return []
    with CUSTOM_MODULES_PATH.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise WorkflowValidationError("自定义模块文件格式无效")
    return [item for item in value if isinstance(item, dict)]


def write_custom_modules(modules: list[dict[str, Any]]) -> None:
    temporary = CUSTOM_MODULES_PATH.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(modules, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(CUSTOM_MODULES_PATH)


def save_custom_module(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowValidationError("自定义模块必须是对象")
    module_id = str(raw.get("id") or f"module-{uuid.uuid4().hex[:12]}")
    if not CONFIG_ID_PATTERN.fullmatch(module_id):
        raise WorkflowValidationError("自定义模块 ID 无效")
    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or "").strip()
    base_type = str(raw.get("baseType") or "").strip()
    params = raw.get("params", {})
    if not name:
        raise WorkflowValidationError("自定义模块名称不能为空")
    REGISTRY.get(base_type)
    if not isinstance(params, dict):
        raise WorkflowValidationError("自定义模块参数必须是对象")
    normalized = {
        "id": module_id,
        "name": name[:80],
        "description": description[:300],
        "baseType": base_type,
        "params": params,
    }
    modules = read_custom_modules()
    index = next((i for i, item in enumerate(modules) if item.get("id") == module_id), None)
    if index is None:
        modules.append(normalized)
    else:
        modules[index] = normalized
    write_custom_modules(modules)
    return normalized


def delete_custom_module(module_id: str) -> None:
    modules = read_custom_modules()
    filtered = [item for item in modules if item.get("id") != module_id]
    if len(filtered) == len(modules):
        raise FileNotFoundError(module_id)
    write_custom_modules(filtered)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "RPAFlowStudio/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WorkflowValidationError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise WorkflowValidationError("请求体为空或过大")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowValidationError("请求不是有效的 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise WorkflowValidationError("请求必须是 JSON 对象")
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/bootstrap":
                configs = list_configs()
                initial_id = "default" if any(item["id"] == "default" for item in configs) else configs[0]["id"]
                self.send_json(
                    {
                        "ok": True,
                        "modules": REGISTRY.public_modules(),
                        "customModules": read_custom_modules(),
                        "configs": configs,
                        "configId": initial_id,
                        "config": read_config(initial_id),
                        "run": RUN_MANAGER.snapshot(),
                    }
                )
                return
            if path == "/api/configs":
                self.send_json({"ok": True, "configs": list_configs()})
                return
            if path == "/api/config":
                config_id = str(query.get("id", ["default"])[0])
                self.send_json({"ok": True, "id": config_id, "config": read_config(config_id)})
                return
            if path == "/api/custom-modules":
                self.send_json({"ok": True, "modules": read_custom_modules()})
                return
            if path == "/api/run":
                self.send_json({"ok": True, "run": RUN_MANAGER.snapshot()})
                return
            self.serve_static(path)
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "资源不存在")
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/config":
                config_id, config = write_config(payload.get("config"), payload.get("id"))
                self.send_json({"ok": True, "id": config_id, "config": config, "configs": list_configs()})
                return
            if path == "/api/custom-modules":
                module = save_custom_module(payload.get("module"))
                self.send_json({"ok": True, "module": module, "modules": read_custom_modules()})
                return
            if path == "/api/run/cancel":
                cancelled = RUN_MANAGER.cancel()
                if not cancelled:
                    raise WorkflowExecutionError("当前没有正在运行的任务")
                self.send_json({"ok": True, "run": RUN_MANAGER.snapshot()})
                return
            if path == "/api/run":
                config = payload.get("config")
                variables = payload.get("variables", {})
                if not isinstance(variables, dict):
                    raise WorkflowValidationError("variables 必须是对象")
                until_step_id = payload.get("untilStepId")
                if until_step_id is not None:
                    until_step_id = str(until_step_id)
                run_id = RUN_MANAGER.start(config, variables, until_step_id)
                self.send_json(
                    {"ok": True, "runId": run_id}, HTTPStatus.ACCEPTED
                )
                return
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        except (WorkflowValidationError, WorkflowExecutionError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            item_id = str(query.get("id", [""])[0])
            if path == "/api/config":
                delete_config(item_id)
                self.send_json({"ok": True, "configs": list_configs()})
                return
            if path == "/api/custom-modules":
                delete_custom_module(item_id)
                self.send_json({"ok": True, "modules": read_custom_modules()})
                return
            self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        except (WorkflowValidationError, WorkflowExecutionError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "目标不存在")
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error_json(HTTPStatus.FORBIDDEN, "禁止访问")
            return
        if not target.is_file():
            raise FileNotFoundError(target)
        content = target.read_bytes()
        content_type, _encoding = mimetypes.guess_type(target.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)


def main() -> int:
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_CONFIG_PATH.is_file():
        raise RuntimeError(f"缺少默认配置：{DEFAULT_CONFIG_PATH}")
    read_config()

    address = (HOST, PORT)
    httpd = ThreadingHTTPServer(address, StudioHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"流程配置器已启动：{url}")
    print("关闭本窗口或按 Ctrl+C 即可停止。")
    if os.getenv("RPA_STUDIO_NO_BROWSER") != "1":
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止流程配置器……")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
