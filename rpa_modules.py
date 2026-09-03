"""Built-in modules that bridge JSON steps to the verified HIS RPA functions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PIL import ImageGrab

STUDIO_ROOT = Path(__file__).resolve().parent

import his_automation as his
import ocr_marker as marker
from pywinauto import mouse
from pywinauto.keyboard import send_keys

from workflow_engine import (
    ExecutionContext,
    ModuleDefinition,
    WorkflowExecutionError,
    WorkflowRegistry,
)


def field(
    name: str,
    label: str,
    field_type: str,
    default: Any,
    **extra: Any,
) -> dict[str, Any]:
    result = {"name": name, "label": label, "type": field_type, "default": default}
    result.update(extra)
    return result


def current_window(context: ExecutionContext) -> Any:
    window = his.activate_his_window()
    context.state["window"] = window
    return window


def run_window_activate(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    title = str(params.get("windowTitle") or his.WINDOW_TITLE).strip()
    if not title:
        raise WorkflowExecutionError("窗口标题不能为空")
    his.WINDOW_TITLE = title
    marker.base.WINDOW_TITLE = title
    window = his.activate_his_window()
    context.state["window"] = window
    return {"windowTitle": title}


def run_window_maximize(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    window = current_window(context)
    try:
        maximized = bool(window.is_maximized())
    except Exception:
        maximized = False
    if not maximized:
        window.maximize()
        time.sleep(float(params.get("layoutWaitSeconds", 0.6)))
        context.emit("info", "HIS 窗口已最大化", None)
    else:
        context.emit("info", "HIS 窗口已经是最大化状态", None)
    return {"wasAlreadyMaximized": maximized}


PATIENT_QUERY_GRID: dict[str, tuple[int, int]] = {
    "卡类型": (5, 1),
    "卡号": (1, 2),
    "登记号": (2, 2),
    "姓名": (3, 2),
    "性别": (4, 2),
    "出生日期": (5, 2),
    "身份证": (1, 3),
    "住院号": (2, 3),
    "科室": (3, 3),
    "病区": (4, 3),
    "医生": (5, 3),
    "就诊号": (1, 4),
    "诊断": (2, 4),
    "年龄": (3, 4),
    "病历": (4, 4),
}

PATIENT_QUERY_ACTIONS: dict[str, tuple[int, int]] = {
    "读卡": (1, 1),
    "打印": (2, 1),
    "查询": (1, 2),
    "导出": (2, 2),
    "清屏": (1, 3),
}

PATIENT_QUERY_CHECKBOXES: dict[str, int] = {
    "门急诊患者": 1,
    "住院患者": 2,
    "出院患者": 3,
}


def find_his_edit_control(window: Any, field_name: str, timeout: float = 5.0) -> Any | None:
    """Find one visible edit by its accessible field name."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            matches = [
                item
                for item in window.descendants(control_type="Edit")
                if his.normalize_text(getattr(item.element_info, "name", ""))
                == field_name
                and item.is_visible()
                and item.is_enabled()
            ]
        except Exception:
            matches = []
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.2)
    return None


def find_named_his_control(
    window: Any,
    name: str,
    control_types: set[str] | None,
    timeout: float,
    contains: bool = False,
) -> Any | None:
    """Find a visible HIS control by accessible name and optional control types."""

    target = his.normalize_text(name)
    deadline = time.monotonic() + max(0, timeout)
    while True:
        try:
            matches: list[Any] = []
            for item in window.descendants():
                item_name = his.normalize_text(getattr(item.element_info, "name", ""))
                item_type = str(getattr(item.element_info, "control_type", "") or "")
                name_matches = target in item_name if contains else item_name == target
                if (
                    name_matches
                    and (not control_types or item_type in control_types)
                    and item.is_visible()
                    and item.is_enabled()
                ):
                    matches.append(item)
        except Exception:
            matches = []
        if matches:
            type_priority = {
                "Button": 0,
                "CheckBox": 0,
                "ComboBox": 0,
                "Hyperlink": 1,
                "TabItem": 1,
                "MenuItem": 1,
                "DataItem": 2,
                "ListItem": 2,
                "Text": 3,
            }
            return min(
                matches,
                key=lambda item: (
                    type_priority.get(
                        str(getattr(item.element_info, "control_type", "") or ""), 9
                    ),
                    item.rectangle().top,
                    item.rectangle().left,
                ),
            )
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def patient_query_row_step(window: Any, scale: float) -> float:
    rect = window.rectangle()
    logical_height = (rect.bottom - rect.top) / scale
    return (25 if logical_height <= 820 else 30) * scale


def patient_query_field_point(window: Any, field_name: str) -> tuple[int, int, float]:
    """Calculate a configured patient-query field from the responsive grid."""

    if field_name not in PATIENT_QUERY_GRID:
        raise WorkflowExecutionError(
            f"字段“{field_name}”没有可用的布局坐标，且 UIA 未识别到该控件"
        )
    column, row = PATIENT_QUERY_GRID[field_name]
    hospitalization_x, hospitalization_y, scale = his.hospitalization_field_point(window)
    rect = window.rectangle()
    logical_width = (rect.right - rect.left) / scale
    logical_height = (rect.bottom - rect.top) / scale

    if logical_width <= 1500:
        gap, fixed_action_width = 7, 78 * 2
    else:
        gap, fixed_action_width = 11, 84 * 2
    grid_inner_width = logical_width - 20
    flexible_column_width = (
        grid_inner_width - fixed_action_width - gap * 7
    ) / 6
    column_step = (flexible_column_width + gap) * scale
    row_step = (25 if logical_height <= 820 else 30) * scale

    x = round(hospitalization_x + (column - 2) * column_step)
    y = round(hospitalization_y + (row - 3) * row_step)
    if not (rect.left < x < rect.right and rect.top < y < rect.bottom):
        raise WorkflowExecutionError(f"字段“{field_name}”的坐标超出 HIS 窗口")
    return x, y, scale


def patient_query_action_point(window: Any, action_name: str) -> tuple[int, int]:
    """Locate the five standard action buttons in the patient query panel."""

    if action_name not in PATIENT_QUERY_ACTIONS:
        raise WorkflowExecutionError(
            f"对象“{action_name}”未被 UIA 识别，也没有可用的患者查询布局坐标"
        )
    action_column, action_row = PATIENT_QUERY_ACTIONS[action_name]
    _hospitalization_x, hospitalization_y, scale = patient_query_field_point(
        window, "住院号"
    )
    rect = window.rectangle()
    x_offset = 180 if action_column == 1 else 70
    x = round(rect.right - x_offset * scale)
    y = round(hospitalization_y + (action_row - 3) * patient_query_row_step(window, scale))
    return x, y


def patient_query_checkbox_point(window: Any, checkbox_name: str) -> tuple[int, int]:
    """Locate a patient-type checkbox beside the patient query form."""

    if checkbox_name not in PATIENT_QUERY_CHECKBOXES:
        raise WorkflowExecutionError(
            f"勾选框“{checkbox_name}”未被 UIA 识别，也没有可用的布局坐标"
        )
    row = PATIENT_QUERY_CHECKBOXES[checkbox_name]
    _hospitalization_x, hospitalization_y, scale = patient_query_field_point(
        window, "住院号"
    )
    rect = window.rectangle()
    width = rect.right - rect.left
    x = round(rect.left + width * 0.739)
    y = round(hospitalization_y + (row - 3) * patient_query_row_step(window, scale))
    return x, y


def visual_checkbox_state(point: tuple[int, int]) -> bool:
    """Detect the HIS blue checked state around a fallback checkbox point."""

    x, y = point
    with ImageGrab.grab(bbox=(x - 9, y - 9, x + 10, y + 10)) as image:
        rgb = image.convert("RGB")
        blue_pixels = sum(
            1
            for red, green, blue in rgb.getdata()
            if red < 100 and green > 80 and blue > 130 and blue > red + 60
        )
    return blue_pixels >= 18


def run_his_input_field(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    field_name = str(params.get("fieldName") or "").strip()
    value = str(params.get("value") or "").strip()
    timeout = float(params.get("timeoutSeconds", 5))
    submit = bool(params.get("pressEnter", True))
    if not field_name:
        raise WorkflowExecutionError("目标字段名称不能为空")
    if not value:
        raise WorkflowExecutionError(f"字段“{field_name}”的填写内容不能为空")

    window = current_window(context)
    control = find_his_edit_control(window, field_name, timeout)
    if control is not None:
        control.click_input()
        locator_mode = "uia-control"
        rect = control.rectangle()
        point = (
            rect.left + (rect.right - rect.left) // 2,
            rect.top + (rect.bottom - rect.top) // 2,
        )
    else:
        x, y, _scale = patient_query_field_point(window, field_name)
        mouse.click(button="left", coords=(x, y))
        locator_mode = "responsive-grid"
        point = (x, y)

    time.sleep(0.15)
    send_keys("^a", pause=0.06)
    send_keys("{BACKSPACE}", pause=0.06)
    send_keys(value, pause=0.08)
    time.sleep(0.2)
    if submit:
        send_keys("{ENTER}", pause=0.08)
    context.emit(
        "info",
        f"已填写 HIS 字段“{field_name}”{('并按回车' if submit else '')}；定位方式={locator_mode}",
        None,
    )
    if field_name == "卡号" and his.HIS_URL.startswith("http://127.0.0.1:51321"):
        context.emit(
            "warning",
            "当前模拟 HIS 的卡号输入框未接入患者查询条件；模块可完成填写，但 demo 不会按卡号过滤结果",
            None,
        )
    return {
        "fieldName": field_name,
        "value": value,
        "pressedEnter": submit,
        "locatorMode": locator_mode,
        "point": list(point),
    }


def run_his_select_option(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    field_name = str(params.get("fieldName") or "").strip()
    option_text = str(params.get("optionText") or "").strip()
    timeout = float(params.get("timeoutSeconds", 5))
    if not field_name:
        raise WorkflowExecutionError("下拉框字段名称不能为空")
    if not option_text:
        raise WorkflowExecutionError(f"下拉框“{field_name}”的目标选项不能为空")

    window = current_window(context)
    control = find_named_his_control(window, field_name, {"ComboBox"}, timeout)
    if control is not None:
        try:
            control.select(option_text)
        except Exception:
            control.click_input()
            send_keys("{HOME}", pause=0.06)
            send_keys(option_text, pause=0.08)
            send_keys("{ENTER}", pause=0.06)
        locator_mode = "uia-control"
        rect = control.rectangle()
        point = (rect.left + rect.width() // 2, rect.top + rect.height() // 2)
    else:
        x, y, _scale = patient_query_field_point(window, field_name)
        mouse.click(button="left", coords=(x, y))
        send_keys("{HOME}", pause=0.06)
        send_keys(option_text, pause=0.08)
        send_keys("{ENTER}", pause=0.06)
        locator_mode = "responsive-grid"
        point = (x, y)

    context.emit(
        "info",
        f"已在下拉框“{field_name}”选择“{option_text}”；定位方式={locator_mode}",
        None,
    )
    return {
        "fieldName": field_name,
        "optionText": option_text,
        "locatorMode": locator_mode,
        "point": list(point),
    }


def run_his_set_checkbox(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    checkbox_name = str(params.get("checkboxName") or "").strip()
    target_state = str(params.get("targetState") or "勾选").strip()
    timeout = float(params.get("timeoutSeconds", 5))
    if not checkbox_name:
        raise WorkflowExecutionError("勾选框名称不能为空")
    if target_state not in {"勾选", "取消勾选", "切换"}:
        raise WorkflowExecutionError("勾选框目标状态无效")

    window = current_window(context)
    control = find_named_his_control(window, checkbox_name, {"CheckBox"}, timeout)
    if control is not None:
        try:
            current_state = bool(control.get_toggle_state())
        except Exception:
            current_state = bool(control.iface_toggle.CurrentToggleState)
        rect = control.rectangle()
        point = (rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        locator_mode = "uia-control"
        should_click = (
            target_state == "切换"
            or (target_state == "勾选" and not current_state)
            or (target_state == "取消勾选" and current_state)
        )
        if should_click:
            control.click_input()
    else:
        point = patient_query_checkbox_point(window, checkbox_name)
        current_state = visual_checkbox_state(point)
        locator_mode = "visual-responsive-grid"
        should_click = (
            target_state == "切换"
            or (target_state == "勾选" and not current_state)
            or (target_state == "取消勾选" and current_state)
        )
        if should_click:
            mouse.click(button="left", coords=point)

    final_state = not current_state if should_click else current_state
    context.emit(
        "info",
        f"勾选框“{checkbox_name}”已设为{('勾选' if final_state else '未勾选')}；定位方式={locator_mode}",
        None,
    )
    return {
        "checkboxName": checkbox_name,
        "targetState": target_state,
        "finalChecked": final_state,
        "clicked": should_click,
        "locatorMode": locator_mode,
        "point": list(point),
    }


def run_his_click_object(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    target_name = str(params.get("targetName") or "").strip()
    control_type = str(params.get("controlType") or "自动").strip()
    match_mode = str(params.get("matchMode") or "精确匹配").strip()
    timeout = float(params.get("timeoutSeconds", 5))
    if not target_name:
        raise WorkflowExecutionError("点击对象名称不能为空")
    allowed_types = {
        "Button",
        "Hyperlink",
        "TabItem",
        "MenuItem",
        "Text",
        "DataItem",
        "ListItem",
    }
    if control_type != "自动" and control_type not in allowed_types:
        raise WorkflowExecutionError("点击对象控件类型无效")
    if match_mode not in {"精确匹配", "包含文字"}:
        raise WorkflowExecutionError("点击对象匹配方式无效")

    window = current_window(context)
    control_types = allowed_types if control_type == "自动" else {control_type}
    control = find_named_his_control(
        window,
        target_name,
        control_types,
        timeout,
        contains=match_mode == "包含文字",
    )
    if control is not None:
        control.click_input()
        rect = control.rectangle()
        point = (rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        locator_mode = "uia-control"
    else:
        point = patient_query_action_point(window, target_name)
        mouse.click(button="left", coords=point)
        locator_mode = "responsive-grid"

    context.emit(
        "info",
        f"已点击 HIS 对象“{target_name}”；定位方式={locator_mode}",
        None,
    )
    return {
        "targetName": target_name,
        "controlType": control_type,
        "matchMode": match_mode,
        "locatorMode": locator_mode,
        "point": list(point),
    }


def run_his_input_number(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    number = str(params.get("value") or "").strip()
    if not number:
        raise WorkflowExecutionError("住院号不能为空")
    his.HOSPITALIZATION_NUMBER = number
    marker.base.HOSPITALIZATION_NUMBER = number
    context.variables["hospitalization_number"] = number
    window = current_window(context)
    point, locator_mode = his.enter_hospitalization_number(window)
    return {"value": number, "locatorMode": locator_mode, "point": list(point)}


def run_his_wait_query(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    timeout = float(params.get("timeoutSeconds", 30))
    client = his.HisApiClient(his.HIS_URL)
    client.post("/api/session/bootstrap")
    projection = client.get("/api/view")
    if his.normalize_text((projection.get("session") or {}).get("status")) != "active":
        client.post("/api/command", {"type": "session.login"})
    deadline = time.monotonic() + timeout
    last_query: dict[str, Any] = {}
    while time.monotonic() < deadline:
        context.check_cancelled()
        projection = client.get("/api/view")
        last_query = ((projection.get("query") or {}).get("patient") or {})
        rows = projection.get("patientEncounterRows") or []
        if (
            his.normalize_text(last_query.get("hospitalizationNumber"))
            == his.HOSPITALIZATION_NUMBER
            and isinstance(rows, list)
            and rows
            and all(
                his.normalize_text(row.get("hospitalizationNumber"))
                == his.HOSPITALIZATION_NUMBER
                for row in rows
            )
        ):
            return {"rowCount": len(rows)}
        context.wait(0.5)
    raise WorkflowExecutionError(
        f"{timeout:g} 秒内未等到住院号 {his.HOSPITALIZATION_NUMBER} 的查询结果；最后条件={last_query}"
    )


def run_his_click_result(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    row = int(params.get("row", 1))
    if row != 1:
        raise WorkflowExecutionError("当前模块只支持点击第 1 条结果")
    locator_mode = his.click_query_result(current_window(context))
    return {"row": row, "locatorMode": locator_mode}


def run_his_wait_navigation(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    timeout = float(params.get("timeoutSeconds", 30))
    client = his.HisApiClient(his.HIS_URL)
    client.post("/api/session/bootstrap")
    projection = client.get("/api/view")
    if his.normalize_text((projection.get("session") or {}).get("status")) != "active":
        client.post("/api/command", {"type": "session.login"})
    deadline = time.monotonic() + timeout
    last_selected: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        context.check_cancelled()
        projection = client.get("/api/view")
        selected = projection.get("selectedEncounter")
        last_selected = selected if isinstance(selected, dict) else None
        if (
            last_selected is not None
            and his.normalize_text(last_selected.get("hospitalizationNumber"))
            == his.HOSPITALIZATION_NUMBER
        ):
            orders = projection.get("orders") or []
            context.wait(0.8)
            return {"orderCount": len(orders)}
        context.wait(0.5)
    raise WorkflowExecutionError(
        f"{timeout:g} 秒内未进入住院号 {his.HOSPITALIZATION_NUMBER} 的医嘱页面；最后住院次={last_selected}"
    )


def safe_file_component(value: object) -> str:
    text = str(value or "capture").strip()
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in text)
    return cleaned[:100] or "capture"


def run_capture_current(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    prefix = safe_file_component(
        params.get("filePrefix")
        or f"his_inpatient_{context.variables.get('hospitalization_number', 'unknown')}"
    )
    path = context.output_dir / f"{prefix}_current_{context.state['timestamp']}.png"
    his.capture_his_window(current_window(context), path)
    context.state["current_screenshot"] = path
    context.state["screenshots"].append(path)
    context.emit("info", f"当前位置截图：{path}", {"path": str(path)})
    return {"path": str(path)}


def run_capture_rightmost(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    current = context.state.get("current_screenshot")
    if not isinstance(current, Path) or not current.is_file():
        raise WorkflowExecutionError("请先执行“截取当前位置”模块")
    window = current_window(context)
    scrollbar = his.detect_horizontal_scrollbar(window, current)
    if scrollbar is None:
        context.emit("info", "未检测到横向滚动条，不生成最右端截图", None)
        return {"captured": False}

    original_position, rightmost_position = scrollbar
    his.drag_scrollbar(original_position, rightmost_position)
    prefix = safe_file_component(
        params.get("filePrefix")
        or f"his_inpatient_{context.variables.get('hospitalization_number', 'unknown')}"
    )
    path = context.output_dir / f"{prefix}_rightmost_{context.state['timestamp']}.png"
    try:
        time.sleep(float(params.get("layoutWaitSeconds", 0.5)))
        his.capture_his_window(window, path)
        context.state["screenshots"].append(path)
        context.emit("info", f"最右端截图：{path}", {"path": str(path)})
    finally:
        if bool(params.get("restorePosition", True)) and path.is_file():
            right_state = his.detect_horizontal_scrollbar(window, path)
            if right_state is not None:
                his.drag_scrollbar(right_state[0], original_position)
                time.sleep(0.35)
    return {"captured": True, "path": str(path)}


def run_ocr_mark(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    columns = params.get("columns", list(marker.TARGET_HEADERS))
    if isinstance(columns, str):
        columns = [part.strip() for part in columns.split(",") if part.strip()]
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise WorkflowExecutionError("标注列名必须是字符串列表")
    columns = list(dict.fromkeys(item.strip() for item in columns if item.strip()))
    if not columns:
        raise WorkflowExecutionError("至少需要配置一个标注列名")

    locate_mode = str(params.get("locateMode") or "按表格标题").strip()
    if locate_mode not in {"按表格标题", "按目标列自动定位"}:
        raise WorkflowExecutionError("OCR 定位方式无效")
    table_title = str(params.get("tableTitle") or "").strip()
    if locate_mode == "按表格标题" and not table_title:
        raise WorkflowExecutionError("按表格标题定位时，表格标题不能为空")
    if locate_mode == "按目标列自动定位":
        table_title = ""

    table_right_ratio = float(params.get("tableRightRatio", 0.74))
    if not 0.2 <= table_right_ratio <= 1.0:
        raise WorkflowExecutionError("主表右边界比例必须在 0.2～1.0 之间")
    header_search_height = int(params.get("headerSearchHeight", 90))
    if not 30 <= header_search_height <= 2000:
        raise WorkflowExecutionError("表头向下查找范围必须在 30～2000 像素之间")

    screenshots = list(context.state.get("screenshots", []))
    if not screenshots:
        raise WorkflowExecutionError("没有可供 OCR 标注的截图")
    screenshot_scope = str(params.get("screenshotScope") or "全部原始截图").strip()
    if screenshot_scope == "仅当前位置截图":
        screenshots = [path for path in screenshots if "_current_" in Path(path).name]
    elif screenshot_scope == "仅最右端截图":
        screenshots = [path for path in screenshots if "_rightmost_" in Path(path).name]
    elif screenshot_scope == "仅最新一张截图":
        screenshots = screenshots[-1:]
    elif screenshot_scope != "全部原始截图":
        raise WorkflowExecutionError("截图选择方式无效")
    if not screenshots:
        raise WorkflowExecutionError(f"没有符合“{screenshot_scope}”的截图")
    engine = context.state.get("ocr_engine")
    if engine is None:
        engine = marker.create_ocr_engine()
        context.state["ocr_engine"] = engine

    all_found: set[str] = set()
    outputs: list[str] = []
    for screenshot in screenshots:
        context.check_cancelled()
        marked_path, found = marker.annotate_screenshot(
            engine,
            Path(screenshot),
            columns,
            table_title,
            table_right_ratio,
            header_search_height,
        )
        context.state["marked_screenshots"].append(marked_path)
        all_found.update(found)
        outputs.append(str(marked_path))
        context.emit(
            "info",
            f"标框截图：{marked_path}；识别字段：{', '.join(found) if found else '无'}",
            {"path": str(marked_path), "found": found},
        )

    missing = [column for column in columns if column not in all_found]
    if missing:
        context.emit("warning", f"两张图中未找到：{', '.join(missing)}", None)
    return {"paths": outputs, "found": sorted(all_found), "missing": missing}


def run_wait(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    seconds = float(params.get("seconds", 1.0))
    if not 0 <= seconds <= 120:
        raise WorkflowExecutionError("等待时间必须在 0～120 秒之间")
    context.wait(seconds)
    return {"seconds": seconds}


def run_keyboard(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    keys = str(params.get("keys") or "").strip()
    if not keys or len(keys) > 100:
        raise WorkflowExecutionError("按键内容不能为空且不能超过 100 个字符")
    current_window(context).set_focus()
    send_keys(keys, pause=float(params.get("pauseSeconds", 0.08)))
    return {"keys": keys}


def run_relative_click(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    x_ratio = float(params.get("xRatio", 0.5))
    y_ratio = float(params.get("yRatio", 0.5))
    if not 0 <= x_ratio <= 1 or not 0 <= y_ratio <= 1:
        raise WorkflowExecutionError("相对坐标必须在 0～1 之间")
    window = current_window(context)
    rect = window.rectangle()
    x = round(rect.left + (rect.right - rect.left) * x_ratio)
    y = round(rect.top + (rect.bottom - rect.top) * y_ratio)
    mouse.click(button=str(params.get("button") or "left"), coords=(x, y))
    return {"point": [x, y]}


def run_uia_click(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip()
    control_type = str(params.get("controlType") or "Button").strip()
    timeout = float(params.get("timeoutSeconds", 5))
    if not name:
        raise WorkflowExecutionError("控件名称不能为空")
    window = current_window(context)
    control = window.child_window(
        title=name,
        control_type=control_type,
    )
    if not control.exists(timeout=timeout):
        raise WorkflowExecutionError(f"未找到控件：{name} / {control_type}")
    control.wrapper_object().click_input()
    return {"name": name, "controlType": control_type}


def build_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()

    definitions = [
        ModuleDefinition(
            "window.activate",
            "连接窗口",
            "窗口",
            "按标题连接并激活目标 Windows 窗口。",
            (field("windowTitle", "窗口标题", "text", "医院信息系统（HIS）"),),
            run_window_activate,
        ),
        ModuleDefinition(
            "window.maximize",
            "最大化窗口",
            "窗口",
            "仅在窗口尚未最大化时执行最大化。",
            (field("layoutWaitSeconds", "界面重排等待（秒）", "number", 0.6, min=0, max=10, step=0.1),),
            run_window_maximize,
        ),
        ModuleDefinition(
            "his.input_field",
            "填写 HIS 字段并回车",
            "HIS",
            "字段名称和填写内容均可配置；优先 UIA，失败时按患者查询网格定位。",
            (
                field("fieldName", "目标字段名称", "text", "卡号"),
                field("value", "填写内容/变量", "text", ""),
                field("pressEnter", "填写后按回车", "boolean", True),
                field("timeoutSeconds", "控件查找超时（秒）", "number", 5, min=0, max=60, step=1),
            ),
            run_his_input_field,
        ),
        ModuleDefinition(
            "his.select_option",
            "HIS 选择下拉项",
            "HIS",
            "字段名称和目标选项均可配置；优先按 ComboBox 控件定位。",
            (
                field("fieldName", "下拉框字段名称", "text", "性别"),
                field("optionText", "选择内容/变量", "text", "全部"),
                field("timeoutSeconds", "控件查找超时（秒）", "number", 5, min=0, max=60, step=1),
            ),
            run_his_select_option,
        ),
        ModuleDefinition(
            "his.set_checkbox",
            "HIS 设置勾选框",
            "HIS",
            "按名称设置勾选或取消勾选；执行前读取当前状态，避免错误反选。",
            (
                field("checkboxName", "勾选框名称", "text", "出院患者"),
                field(
                    "targetState",
                    "目标状态",
                    "select",
                    "勾选",
                    options=["勾选", "取消勾选", "切换"],
                ),
                field("timeoutSeconds", "控件查找超时（秒）", "number", 5, min=0, max=60, step=1),
            ),
            run_his_set_checkbox,
        ),
        ModuleDefinition(
            "his.click_object",
            "HIS 点击对象",
            "HIS",
            "点击对象名称可配置，例如查询、清屏、导出、打印或其他 UIA 控件。",
            (
                field("targetName", "点击对象名称", "text", "查询"),
                field(
                    "controlType",
                    "控件类型",
                    "select",
                    "自动",
                    options=[
                        "自动",
                        "Button",
                        "Hyperlink",
                        "TabItem",
                        "MenuItem",
                        "Text",
                        "DataItem",
                        "ListItem",
                    ],
                ),
                field(
                    "matchMode",
                    "名称匹配方式",
                    "select",
                    "精确匹配",
                    options=["精确匹配", "包含文字"],
                ),
                field("timeoutSeconds", "控件查找超时（秒）", "number", 5, min=0, max=60, step=1),
            ),
            run_his_click_object,
        ),
        ModuleDefinition(
            "his.input_hospitalization",
            "填写住院号并回车",
            "HIS",
            "优先使用 UIA，失败时沿用已验证的相对布局定位。",
            (field("value", "住院号/变量", "text", "${hospitalization_number}"),),
            run_his_input_number,
        ),
        ModuleDefinition(
            "his.wait_query",
            "等待患者查询结果",
            "HIS",
            "等待页面确认住院号查询已经完成。",
            (field("timeoutSeconds", "超时（秒）", "number", 30, min=1, max=180, step=1),),
            run_his_wait_query,
        ),
        ModuleDefinition(
            "his.click_first_result",
            "点击查询结果",
            "HIS",
            "点击患者查询表中的第一条结果。",
            (field("row", "结果序号", "number", 1, min=1, max=1, step=1),),
            run_his_click_result,
        ),
        ModuleDefinition(
            "his.wait_navigation",
            "等待医嘱页面",
            "HIS",
            "确认已经进入目标住院次的医嘱费用查询页面。",
            (field("timeoutSeconds", "超时（秒）", "number", 30, min=1, max=180, step=1),),
            run_his_wait_navigation,
        ),
        ModuleDefinition(
            "capture.current",
            "截取当前位置",
            "截图",
            "截取当前 HIS 窗口并记录为 OCR 输入。",
            (field("filePrefix", "文件名前缀", "text", "his_inpatient_${hospitalization_number}"),),
            run_capture_current,
        ),
        ModuleDefinition(
            "capture.rightmost",
            "截取横向最右端",
            "截图",
            "检测横向滚动条，拖到最右端截图并恢复。",
            (
                field("filePrefix", "文件名前缀", "text", "his_inpatient_${hospitalization_number}"),
                field("restorePosition", "截图后恢复", "boolean", True),
                field("layoutWaitSeconds", "滚动后等待（秒）", "number", 0.5, min=0, max=10, step=0.1),
            ),
            run_capture_rightmost,
        ),
        ModuleDefinition(
            "ocr.mark_columns",
            "OCR 标注表格列",
            "OCR",
            "定位对象、截图范围和列名均按本步骤配置，不影响流程中的其他步骤。",
            (
                field(
                    "locateMode",
                    "定位方式",
                    "select",
                    "按表格标题",
                    options=["按表格标题", "按目标列自动定位"],
                ),
                field("tableTitle", "表格标题/定位文字", "text", "医嘱明细"),
                field(
                    "columns",
                    "目标列（每行一个）",
                    "stringList",
                    list(marker.TARGET_HEADERS),
                ),
                field(
                    "screenshotScope",
                    "处理哪些截图",
                    "select",
                    "全部原始截图",
                    options=[
                        "全部原始截图",
                        "仅当前位置截图",
                        "仅最右端截图",
                        "仅最新一张截图",
                    ],
                ),
                field(
                    "headerSearchHeight",
                    "标题下方查找范围（像素）",
                    "number",
                    90,
                    min=30,
                    max=2000,
                    step=10,
                ),
                field(
                    "tableRightRatio",
                    "表格右边界（窗口宽度比例）",
                    "number",
                    0.74,
                    min=0.2,
                    max=1,
                    step=0.01,
                ),
            ),
            run_ocr_mark,
        ),
        ModuleDefinition(
            "utility.wait",
            "等待",
            "高级（可选）",
            "在两个动作之间等待页面稳定。",
            (field("seconds", "等待秒数", "number", 1, min=0, max=120, step=0.1),),
            run_wait,
        ),
        ModuleDefinition(
            "keyboard.send",
            "发送按键",
            "高级（可选）",
            "向当前目标窗口发送 pywinauto 按键表达式。",
            (
                field("keys", "按键", "text", "{ENTER}"),
                field("pauseSeconds", "按键间隔（秒）", "number", 0.08, min=0, max=2, step=0.01),
            ),
            run_keyboard,
        ),
        ModuleDefinition(
            "mouse.click_relative",
            "按窗口比例点击",
            "高级（可选）",
            "按目标窗口宽高比例点击，作为控件定位的兜底方式。",
            (
                field("xRatio", "横向比例", "number", 0.5, min=0, max=1, step=0.01),
                field("yRatio", "纵向比例", "number", 0.5, min=0, max=1, step=0.01),
                field("button", "鼠标键", "select", "left", options=["left", "right"]),
            ),
            run_relative_click,
        ),
        ModuleDefinition(
            "uia.click",
            "点击 UIA 控件",
            "高级（可选）",
            "按控件名称与类型定位并点击。",
            (
                field("name", "控件名称", "text", ""),
                field("controlType", "控件类型", "text", "Button"),
                field("timeoutSeconds", "超时（秒）", "number", 5, min=1, max=60, step=1),
            ),
            run_uia_click,
        ),
    ]
    for definition in definitions:
        registry.register(definition)
    return registry
