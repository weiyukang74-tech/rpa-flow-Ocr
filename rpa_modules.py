"""Built-in modules that bridge JSON steps to the verified HIS RPA functions."""

from __future__ import annotations

import ctypes
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageGrab

STUDIO_ROOT = Path(__file__).resolve().parent

import his_automation as his
import ocr_marker as marker
from visual_shape_detection import find_checkbox_square, find_input_rectangle
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


def visual_page_field(default: str = "current-page") -> dict[str, Any]:
    return field(
        "visualPage",
        "视觉页面标识",
        "text",
        default,
    )


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
    hwnd = int(getattr(window, "handle", 0) or 0)
    if not hwnd:
        raise WorkflowExecutionError("目标窗口没有可用的顶层句柄")

    user32 = ctypes.windll.user32
    was_maximized = bool(user32.IsZoomed(hwnd))
    if not was_maximized:
        # 最大化属于顶层窗口状态，不依赖 UIA 类型、标题栏样式或按钮位置。
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        context.wait(float(params.get("layoutWaitSeconds", 0.6)))
        if not user32.IsZoomed(hwnd):
            raise WorkflowExecutionError(
                "Windows 已收到最大化命令，但目标顶层窗口没有进入最大化状态"
            )
        context.emit("info", "HIS 窗口已最大化", None)
    else:
        context.emit("info", "HIS 窗口已经是最大化状态", None)
    rect = window.rectangle()
    return {
        "wasAlreadyMaximized": was_maximized,
        "hwnd": hwnd,
        "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
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


VISUAL_MAP_ROOT = STUDIO_ROOT / "configs" / "visual-maps"


def visual_page_id(params: dict[str, Any]) -> str:
    """Return a user-defined logical page key without assuming any application."""

    page = str(params.get("visualPage") or "current-page").strip()
    if not page:
        page = "current-page"
    return page[:80]


def visual_window_signature(window: Any, page: str) -> dict[str, Any]:
    rect = window.rectangle()
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise WorkflowExecutionError("目标窗口没有有效的屏幕区域")
    return {
        "locatorVersion": 4,
        "page": page,
        "windowTitle": his.normalize_text(window.window_text()),
        "className": str(getattr(window.element_info, "class_name", "") or ""),
        "width": width,
        "height": height,
        "dpiScale": round(float(his.window_dpi_scale(window)), 3),
    }


def visual_map_path(signature: dict[str, Any]) -> Path:
    identity = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return VISUAL_MAP_ROOT / f"page-{digest}" / "layout.json"


def visual_reference_path(signature: dict[str, Any]) -> Path:
    return visual_map_path(signature).with_name("reference.png")


def load_visual_targets(signature: dict[str, Any]) -> dict[str, list[int]]:
    path = visual_map_path(signature)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("signature") != signature or not isinstance(value.get("targets"), dict):
        return {}
    targets: dict[str, list[int]] = {}
    for key, point in value["targets"].items():
        if (
            isinstance(key, str)
            and isinstance(point, list)
            and len(point) == 2
            and all(isinstance(item, int) for item in point)
        ):
            targets[key] = point
    return targets


def save_visual_targets(page_map: dict[str, Any]) -> None:
    path = visual_map_path(page_map["signature"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    raw_ocr = [
        {
            "id": f"text-{index:04d}",
            "text": item.text,
            "rawText": item.raw_text,
            "score": round(float(item.score), 5),
            "bounds": [item.left, item.top, item.right, item.bottom],
        }
        for index, item in enumerate(page_map.get("ocrItems") or [])
    ]
    payload = {
        "schemaVersion": 4,
        "signature": page_map["signature"],
        "targets": page_map["targets"],
        "targetDetails": page_map.get("targetDetails", {}),
        "elements": page_map.get("elements", []),
        "ambiguousTargets": page_map.get("ambiguousTargets", {}),
        "rawOcr": raw_ocr,
        "regions": page_map.get("regions", []),
        "relationships": page_map.get("relationships", []),
        "visualCandidates": page_map.get("visualCandidates", []),
        "unclassified": page_map.get("unclassified", []),
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    visual_image = page_map.get("visualImage")
    reference = visual_reference_path(page_map["signature"])
    if isinstance(visual_image, Image.Image):
        visual_image.save(reference)


def load_visual_model(signature: dict[str, Any]) -> dict[str, Any]:
    path = visual_map_path(signature)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("signature") != signature:
        return {}
    return value


def build_ocr_regions(items: list[Any]) -> list[dict[str, Any]]:
    """Keep coarse text bands so later classifiers retain page structure."""

    if not items:
        return []
    item_ids = {id(item): f"text-{index:04d}" for index, item in enumerate(items)}
    ordered = sorted(items, key=lambda item: (item.top, item.left))
    bands: list[list[Any]] = []
    for item in ordered:
        if not bands or item.top > max(row.bottom for row in bands[-1]) + 35:
            bands.append([item])
        else:
            bands[-1].append(item)
    return [
        {
            "id": f"region-{index:03d}",
            "bounds": [
                min(item.left for item in band),
                min(item.top for item in band),
                max(item.right for item in band),
                max(item.bottom for item in band),
            ],
            "textIds": [item_ids[id(item)] for item in band],
        }
        for index, band in enumerate(bands)
    ]


def build_ocr_relationships(items: list[Any]) -> list[dict[str, Any]]:
    """Record nearest spatial neighbors without assigning business semantics."""

    relationships: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        center_x = (item.left + item.right) / 2
        center_y = (item.top + item.bottom) / 2
        candidates: dict[str, tuple[float, int]] = {}
        for other_index, other in enumerate(items):
            if other_index == index:
                continue
            other_x = (other.left + other.right) / 2
            other_y = (other.top + other.bottom) / 2
            dx, dy = other_x - center_x, other_y - center_y
            if abs(dx) >= abs(dy):
                direction = "right" if dx > 0 else "left"
            else:
                direction = "below" if dy > 0 else "above"
            distance = (dx * dx + dy * dy) ** 0.5
            if direction not in candidates or distance < candidates[direction][0]:
                candidates[direction] = (distance, other_index)
        for direction, (distance, other_index) in candidates.items():
            relationships.append(
                {
                    "from": f"text-{index:04d}",
                    "to": f"text-{other_index:04d}",
                    "relation": direction,
                    "distance": round(distance, 2),
                }
            )
    return relationships


def classify_visual_page(page_map: dict[str, Any]) -> None:
    """Classify the complete OCR page once and persist reusable control targets.

    Classification stays application-neutral. A painted rectangle linked to a
    label is recorded as a field candidate that may be used by either an input
    or select action; the workflow action supplies the final semantic intent.
    Every OCR occurrence is retained, including duplicate labels that cannot be
    safely collapsed into one click target.
    """

    items = page_map.get("ocrItems")
    image = page_map.get("visualImage")
    if not isinstance(items, list) or not isinstance(image, Image.Image):
        return

    elements: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    occurrences: dict[str, list[int]] = {}
    resolved_text_ids: set[str] = set()

    for index, item in enumerate(items):
        element_id = f"text-{index:04d}"
        normalized = marker.normalize_ocr_text(item.text)
        occurrences.setdefault(normalized, []).append(index)
        elements.append(
            {
                "id": element_id,
                "kind": "text",
                "text": item.text,
                "rawText": item.raw_text,
                "bounds": [item.left, item.top, item.right, item.bottom],
                "center": [
                    (item.left + item.right) // 2,
                    (item.top + item.bottom) // 2,
                ],
                "confidence": round(float(item.score), 5),
                "possibleRoles": ["text", "button", "tab", "menu-item", "link"],
            }
        )

    ambiguous: dict[str, list[str]] = {}
    for normalized, indexes in occurrences.items():
        if not normalized:
            continue
        ids = [f"text-{index:04d}" for index in indexes]
        if len(indexes) > 1:
            ambiguous[normalized] = ids
            continue
        index = indexes[0]
        item = items[index]
        point = [(item.left + item.right) // 2, (item.top + item.bottom) // 2]
        key = f"button:exact:{item.text}"
        page_map["targets"].setdefault(key, point)
        page_map["targetDetails"].setdefault(
            key,
            {
                "role": "text-action",
                "possibleRoles": ["button", "tab", "menu-item", "link", "text"],
                "name": item.text,
                "bounds": [item.left, item.top, item.right, item.bottom],
                "clickPoint": point,
                "matchMode": "exact",
                "confidence": round(float(item.score), 5),
                "source": "ocr-page-classifier",
            },
        )

    for index, label in enumerate(items):
        normalized = marker.normalize_ocr_text(label.text)
        if not normalized:
            continue
        label_id = f"text-{index:04d}"
        unique_label = len(occurrences.get(normalized, [])) == 1

        rectangle = find_input_rectangle(image, label)
        if rectangle is not None:
            candidate_id = f"field-{len(candidates):04d}"
            candidate = {
                "id": candidate_id,
                "kind": "field-rectangle",
                "possibleRoles": ["input", "select"],
                "labelText": label.text,
                "labelId": label_id,
                "bounds": rectangle["bounds"],
                "center": rectangle["center"],
                "relation": rectangle["relation"],
                "score": rectangle["score"],
            }
            candidates.append(candidate)
            elements.append(candidate)
            resolved_text_ids.add(label_id)
            if unique_label:
                for role in ("input", "select"):
                    key = f"{role}:{label.text}"
                    page_map["targets"].setdefault(key, list(rectangle["center"]))
                    page_map["targetDetails"].setdefault(
                        key,
                        {
                            "role": "field",
                            "possibleRoles": ["input", "select"],
                            "name": label.text,
                            "bounds": rectangle["bounds"],
                            "clickPoint": rectangle["center"],
                            "labelBounds": [
                                label.left,
                                label.top,
                                label.right,
                                label.bottom,
                            ],
                            "relation": rectangle["relation"],
                            "confidence": rectangle["score"],
                            "source": "ocr-page-classifier",
                        },
                    )

        square = find_checkbox_square(image, label)
        if square is not None:
            candidate_id = f"checkbox-{len(candidates):04d}"
            candidate = {
                "id": candidate_id,
                "kind": "checkbox",
                "possibleRoles": ["checkbox", "toggle"],
                "labelText": label.text,
                "labelId": label_id,
                "bounds": square["bounds"],
                "center": square["center"],
                "relation": square["relation"],
                "score": square["score"],
            }
            candidates.append(candidate)
            elements.append(candidate)
            resolved_text_ids.add(label_id)
            if unique_label:
                key = f"checkbox:{label.text}"
                page_map["targets"].setdefault(key, list(square["center"]))
                page_map["targetDetails"].setdefault(
                    key,
                    {
                        "role": "checkbox",
                        "possibleRoles": ["checkbox", "toggle"],
                        "name": label.text,
                        "bounds": square["bounds"],
                        "clickPoint": square["center"],
                        "labelBounds": [
                            label.left,
                            label.top,
                            label.right,
                            label.bottom,
                        ],
                        "relation": square["relation"],
                        "confidence": square["score"],
                        "source": "ocr-page-classifier",
                    },
                )

    page_map["elements"] = elements
    page_map["ambiguousTargets"] = ambiguous
    page_map["visualCandidates"] = candidates
    page_map["unclassified"] = [
        f"text-{index:04d}"
        for index in range(len(items))
        if f"text-{index:04d}" not in resolved_text_ids
    ]


def get_visual_page_map(
    context: ExecutionContext,
    window: Any,
    page: str = "current-page",
) -> dict[str, Any]:
    signature = visual_window_signature(window, page)
    maps = context.state.setdefault("visual_page_maps", {})
    cache_key = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    page_map = maps.get(cache_key)
    if isinstance(page_map, dict):
        return page_map
    persisted = load_visual_model(signature)
    persisted_details = persisted.get("targetDetails", {})
    persisted_elements = persisted.get("elements", [])
    persisted_ambiguous = persisted.get("ambiguousTargets", {})
    persisted_regions = persisted.get("regions", [])
    persisted_relationships = persisted.get("relationships", [])
    persisted_candidates = persisted.get("visualCandidates", [])
    persisted_unclassified = persisted.get("unclassified", [])
    persisted_raw_ocr = persisted.get("rawOcr", [])
    page_map = {
        "signature": signature,
        "targets": load_visual_targets(signature),
        "targetDetails": persisted_details if isinstance(persisted_details, dict) else {},
        "elements": persisted_elements if isinstance(persisted_elements, list) else [],
        "ambiguousTargets": persisted_ambiguous if isinstance(persisted_ambiguous, dict) else {},
        "ocrItems": None,
        "visualImage": None,
        "regions": persisted_regions if isinstance(persisted_regions, list) else [],
        "relationships": persisted_relationships if isinstance(persisted_relationships, list) else [],
        "visualCandidates": persisted_candidates if isinstance(persisted_candidates, list) else [],
        "unclassified": persisted_unclassified if isinstance(persisted_unclassified, list) else [],
        "persistedRawOcr": persisted_raw_ocr if isinstance(persisted_raw_ocr, list) else [],
    }
    maps[cache_key] = page_map
    return page_map


def ensure_visual_ocr(
    context: ExecutionContext,
    window: Any,
    page_map: dict[str, Any],
) -> list[Any]:
    items = page_map.get("ocrItems")
    visual_image = page_map.get("visualImage")
    if isinstance(items, list) and isinstance(visual_image, Image.Image):
        return items

    persisted_raw = page_map.get("persistedRawOcr")
    reference_path = visual_reference_path(page_map["signature"])
    if isinstance(persisted_raw, list) and persisted_raw and reference_path.is_file():
        restored: list[Any] = []
        for raw in persisted_raw:
            if not isinstance(raw, dict):
                continue
            bounds = raw.get("bounds")
            if not (
                isinstance(bounds, list)
                and len(bounds) == 4
                and all(isinstance(value, int) for value in bounds)
            ):
                continue
            restored.append(
                marker.OcrItem(
                    text=str(raw.get("text") or ""),
                    score=float(raw.get("score") or 0),
                    left=bounds[0],
                    top=bounds[1],
                    right=bounds[2],
                    bottom=bounds[3],
                    raw_text=str(raw.get("rawText") or ""),
                )
            )
        if restored:
            with Image.open(reference_path) as reference:
                page_map["visualImage"] = reference.convert("RGB")
            page_map["ocrItems"] = restored
            if not page_map.get("elements"):
                classify_visual_page(page_map)
                save_visual_targets(page_map)
            return restored

    rect = window.rectangle()
    screenshot_path = context.output_dir / (
        f".visual_map_{context.state['timestamp']}_{time.time_ns()}.png"
    )
    try:
        with ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom)
        ) as screenshot:
            visual_image = screenshot.convert("RGB")
            visual_image.save(screenshot_path)
        engine = context.state.get("ocr_engine")
        if engine is None:
            engine = marker.create_ocr_engine()
            context.state["ocr_engine"] = engine
        items = marker.run_ocr(engine, screenshot_path)
    finally:
        try:
            screenshot_path.unlink()
        except FileNotFoundError:
            pass

    page_map["ocrItems"] = items
    page_map["visualImage"] = visual_image
    page_map["regions"] = build_ocr_regions(items)
    page_map["relationships"] = build_ocr_relationships(items)
    classify_visual_page(page_map)
    save_visual_targets(page_map)
    context.emit(
        "info",
        (
            f"已识别当前页面并完成整页控件预分类：{len(items)} 段文字，"
            f"{len(page_map.get('visualCandidates', []))} 个形状控件候选"
        ),
        None,
    )
    return items


def local_point_to_screen(window: Any, point: list[int]) -> tuple[int, int]:
    rect = window.rectangle()
    return rect.left + int(point[0]), rect.top + int(point[1])


def visual_text_point(
    context: ExecutionContext,
    window: Any,
    target_name: str,
    contains: bool = False,
    page: str = "current-page",
    role: str = "button",
) -> tuple[int, int] | None:
    """Resolve visible text from a reusable per-page visual element map."""

    page_map = get_visual_page_map(context, window, page)
    target_key = f"{role}:{'contains' if contains else 'exact'}:{target_name}"
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        return local_point_to_screen(window, cached)

    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(target_name)
    if not target:
        return None
    exact_matches = [item for item in items if item.text == target]
    matches = exact_matches
    if not matches and contains:
        matches = [item for item in items if target in item.text]
    if not matches:
        return None

    match = min(
        matches,
        key=lambda item: (len(item.text), -item.score, item.top, item.left),
    )
    local_point = [
        (match.left + match.right) // 2,
        (match.top + match.bottom) // 2,
    ]
    page_map["targets"][target_key] = local_point
    page_map["targetDetails"][target_key] = {
        "role": role,
        "name": target_name,
        "bounds": [match.left, match.top, match.right, match.bottom],
        "clickPoint": local_point,
        "matchMode": "contains" if contains else "exact",
        "confidence": round(float(match.score), 5),
        "source": "ocr",
    }
    save_visual_targets(page_map)
    return local_point_to_screen(window, local_point)


def visual_input_point(
    context: ExecutionContext,
    window: Any,
    field_name: str,
    page: str = "current-page",
) -> tuple[int, int] | None:
    """Infer a painted input area below its label and cache the relative point."""

    page_map = get_visual_page_map(context, window, page)
    target_key = f"input:{field_name}"
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        return local_point_to_screen(window, cached)

    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(field_name)
    labels = [item for item in items if item.text == target]
    if not labels:
        return None

    visual_image = page_map.get("visualImage")
    if not isinstance(visual_image, Image.Image):
        return None
    detected: list[tuple[Any, dict[str, Any]]] = []
    for label in labels:
        rectangle = find_input_rectangle(visual_image, label)
        if rectangle is not None:
            detected.append((label, rectangle))
    if not detected:
        return None
    label, rectangle = max(detected, key=lambda pair: pair[1]["score"])
    local_point = list(rectangle["center"])
    page_map["targets"][target_key] = local_point
    page_map["targetDetails"][target_key] = {
        "role": "input",
        "name": field_name,
        "bounds": rectangle["bounds"],
        "clickPoint": local_point,
        "labelBounds": [label.left, label.top, label.right, label.bottom],
        "relation": rectangle["relation"],
        "confidence": rectangle["score"],
        "source": "ocr+shape",
    }
    page_map["visualCandidates"].append(
        {
            "id": f"rectangle-{len(page_map['visualCandidates']):04d}",
            "kind": "light-rectangle",
            "bounds": rectangle["bounds"],
            "score": rectangle["score"],
        }
    )
    save_visual_targets(page_map)
    return local_point_to_screen(window, local_point)


def visual_select_point(
    context: ExecutionContext,
    window: Any,
    field_name: str,
    page: str = "current-page",
) -> tuple[int, int] | None:
    """Locate a visually rendered select/combo box by its label."""

    page_map = get_visual_page_map(context, window, page)
    target_key = f"select:{field_name}"
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        return local_point_to_screen(window, cached)
    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(field_name)
    labels = [item for item in items if item.text == target]
    visual_image = page_map.get("visualImage")
    if not labels or not isinstance(visual_image, Image.Image):
        return None
    detected: list[tuple[Any, dict[str, Any]]] = []
    for label in labels:
        rectangle = find_input_rectangle(visual_image, label)
        if rectangle is not None:
            detected.append((label, rectangle))
    if not detected:
        return None
    label, rectangle = max(detected, key=lambda pair: pair[1]["score"])
    local_point = list(rectangle["center"])
    page_map["targets"][target_key] = local_point
    page_map["targetDetails"][target_key] = {
        "role": "select",
        "name": field_name,
        "bounds": rectangle["bounds"],
        "clickPoint": local_point,
        "labelBounds": [label.left, label.top, label.right, label.bottom],
        "relation": rectangle["relation"],
        "confidence": rectangle["score"],
        "source": "ocr+shape",
    }
    save_visual_targets(page_map)
    return local_point_to_screen(window, local_point)


def visual_checkbox_point(
    context: ExecutionContext,
    window: Any,
    checkbox_name: str,
    page: str = "current-page",
) -> tuple[int, int] | None:
    """Locate a painted checkbox around its label without assuming direction."""

    page_map = get_visual_page_map(context, window, page)
    target_key = f"checkbox:{checkbox_name}"
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        return local_point_to_screen(window, cached)

    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(checkbox_name)
    labels = [item for item in items if item.text == target]
    if not labels:
        return None

    visual_image = page_map.get("visualImage")
    if not isinstance(visual_image, Image.Image):
        return None
    detected: list[tuple[Any, dict[str, Any]]] = []
    for label in labels:
        square = find_checkbox_square(visual_image, label)
        if square is not None:
            detected.append((label, square))
    if not detected:
        return None
    label, square = max(detected, key=lambda pair: pair[1]["score"])
    local_point = list(square["center"])
    page_map["targets"][target_key] = local_point
    page_map["targetDetails"][target_key] = {
        "role": "checkbox",
        "name": checkbox_name,
        "bounds": square["bounds"],
        "clickPoint": local_point,
        "labelBounds": [label.left, label.top, label.right, label.bottom],
        "relation": square["relation"],
        "confidence": square["score"],
        "source": "ocr+shape",
    }
    page_map["visualCandidates"].append(
        {
            "id": f"square-{len(page_map['visualCandidates']):04d}",
            "kind": "square",
            "bounds": square["bounds"],
            "score": square["score"],
        }
    )
    save_visual_targets(page_map)
    return local_point_to_screen(window, local_point)


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
    point = visual_input_point(
        context,
        window,
        field_name,
        page=visual_page_id(params),
    )
    if point is not None:
        mouse.click(button="left", coords=point)
        locator_mode = "visual-map"
    else:
        control = find_his_edit_control(window, field_name, timeout)
        if control is None:
            raise WorkflowExecutionError(
                f"视觉页面布局中未找到字段“{field_name}”，UIA 也无法识别该字段"
            )
        control.click_input()
        locator_mode = "uia-control"
        rect = control.rectangle()
        point = (
            rect.left + (rect.right - rect.left) // 2,
            rect.top + (rect.bottom - rect.top) // 2,
        )

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
    page = visual_page_id(params)
    point = visual_select_point(context, window, field_name, page=page)
    if point is not None:
        mouse.click(button="left", coords=point)
        context.wait(0.25)
        option_page = f"{page}:expanded:{field_name}"[:80]
        option_point = visual_text_point(
            context,
            window,
            option_text,
            contains=False,
            page=option_page,
            role="option",
        )
        if option_point is not None:
            mouse.click(button="left", coords=option_point)
            locator_mode = "visual-map"
        else:
            send_keys("{HOME}", pause=0.06)
            send_keys(option_text, pause=0.08)
            send_keys("{ENTER}", pause=0.06)
            locator_mode = "visual-map-keyboard"
    else:
        control = find_named_his_control(window, field_name, {"ComboBox"}, timeout)
        if control is None:
            raise WorkflowExecutionError(
                f"视觉页面布局中未找到下拉框“{field_name}”，UIA 也无法识别该控件"
            )
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
    point = visual_checkbox_point(
        context,
        window,
        checkbox_name,
        page=visual_page_id(params),
    )
    if point is not None:
        locator_mode = "visual-map"
        current_state = visual_checkbox_state(point)
        should_click = (
            target_state == "切换"
            or (target_state == "勾选" and not current_state)
            or (target_state == "取消勾选" and current_state)
        )
        if should_click:
            mouse.click(button="left", coords=point)
    else:
        control = find_named_his_control(window, checkbox_name, {"CheckBox"}, timeout)
        if control is None:
            raise WorkflowExecutionError(
                f"视觉页面布局中未找到勾选框“{checkbox_name}”，UIA 也无法识别该控件"
            )
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
    point = visual_text_point(
        context,
        window,
        target_name,
        contains=match_mode == "包含文字",
        page=visual_page_id(params),
    )
    if point is not None:
        mouse.click(button="left", coords=point)
        locator_mode = "visual-map"
    else:
        control = find_named_his_control(
            window,
            target_name,
            control_types,
            timeout,
            contains=match_mode == "包含文字",
        )
        if control is None:
            raise WorkflowExecutionError(
                f"视觉页面布局中未找到对象“{target_name}”，UIA 也无法识别该对象"
            )
        control.click_input()
        rect = control.rectangle()
        point = (rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        locator_mode = "uia-control"

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
    point = visual_input_point(
        context,
        window,
        "住院号",
        page=visual_page_id(params),
    )
    if point is not None:
        mouse.click(button="left", coords=point)
        locator_mode = "visual-map"
    else:
        control = find_his_edit_control(window, "住院号", 5.0)
        if control is None:
            raise WorkflowExecutionError(
                "视觉页面布局中未找到住院号输入框，UIA 也无法识别该字段"
            )
        control.click_input()
        rect = control.rectangle()
        point = (rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        locator_mode = "uia-control"
    time.sleep(0.15)
    send_keys("^a", pause=0.06)
    send_keys("{BACKSPACE}", pause=0.06)
    send_keys(number, pause=0.08)
    time.sleep(0.2)
    send_keys("{ENTER}", pause=0.08)
    context.emit(
        "info",
        f"已填写住院号并按回车；定位方式={locator_mode}",
        None,
    )
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


ELLIPSIS_SUFFIXES = ("...", "..", "…", "．．．", "···", "。。。")


def ocr_item_looks_truncated(
    item: marker.OcrItem,
    column_right: int,
    edge_margin_pixels: int,
) -> bool:
    """Detect an ellipsis, with a right-edge fallback for OCR engines that drop dots."""

    raw_text = "".join(str(getattr(item, "raw_text", "") or item.text).split())
    if raw_text.endswith(ELLIPSIS_SUFFIXES):
        return True
    # OCR 偶尔把界面上的三个点只识别成一个点。此时必须同时满足文字框
    # 已贴近列右边界，避免把“每周3次”这类恰好较长但完整的值误判。
    return raw_text.endswith((".", "。")) and item.right >= column_right - edge_margin_pixels


def cluster_ocr_rows(
    items: list[marker.OcrItem],
    tolerance: int = 18,
) -> list[list[marker.OcrItem]]:
    """Cluster screenshot OCR boxes into visual rows from top to bottom."""

    rows: list[list[marker.OcrItem]] = []
    for item in sorted(items, key=lambda entry: (entry.center_y, entry.left)):
        for row in rows:
            row_center = sum(entry.center_y for entry in row) / len(row)
            if abs(item.center_y - row_center) <= tolerance:
                row.append(item)
                break
        else:
            rows.append([item])
    for row in rows:
        row.sort(key=lambda item: item.left)
    return sorted(rows, key=lambda row: min(item.center_y for item in row))


def find_gray_column_resize_handle(
    image: Image.Image,
    divider_x: int,
    header_top: int,
    header_bottom: int,
) -> tuple[int, int, bool]:
    """Locate the short gray resize bar inside a column-header separator."""

    pixels = image.load()
    best: tuple[int, int, int] | None = None
    search_top = max(0, header_top)
    search_bottom = min(image.height - 1, header_bottom)
    for x in range(max(0, divider_x - 7), min(image.width - 1, divider_x + 7) + 1):
        matching_y: list[int] = []
        for y in range(search_top, search_bottom + 1):
            red, green, blue = pixels[x, y]
            brightness = (red + green + blue) / 3
            if 155 <= brightness <= 232 and max(red, green, blue) - min(red, green, blue) <= 22:
                matching_y.append(y)

        runs: list[list[int]] = []
        for y in matching_y:
            if not runs or y - runs[-1][-1] > 1:
                runs.append([y])
            else:
                runs[-1].append(y)
        if not runs:
            continue
        longest = max(runs, key=len)
        candidate = (len(longest), x, round((longest[0] + longest[-1]) / 2))
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None or best[0] < 8:
        return divider_x, round((header_top + header_bottom) / 2), False
    return best[1], best[2], True


def analyze_table_columns_width(
    engine: Any,
    screenshot_path: Path,
    table_title: str,
    table_right_ratio: float,
    header_search_height: int,
    edge_margin_pixels: int,
) -> dict[str, Any]:
    """Use only screenshot OCR and grid lines to find every clipped visible column."""

    items = marker.run_ocr(engine, screenshot_path)
    with Image.open(screenshot_path) as source:
        image = source.convert("RGB")

    main_table_right = round(image.width * table_right_ratio)
    title = marker.locate_table_title(items, table_title)
    if title is None:
        raise RuntimeError(f"OCR 未找到配置的表格标题“{table_title}”")

    search_top = max(title.bottom + 1, 0)
    search_bottom = title.bottom + header_search_height
    candidates = [
        item
        for item in items
        if search_top <= item.center_y <= search_bottom
        and item.left < main_table_right
    ]
    rows = [row for row in cluster_ocr_rows(candidates) if len(row) >= 2]
    if not rows:
        raise RuntimeError(f"OCR 未找到表格“{table_title}”的表头行")

    # 表头是标题下方第一条包含多个文字框的水平行。这里不读取 DOM/UIA
    # 中的完整值，后续所有判断都只使用截图里实际可见的 OCR 框。
    header_items = min(rows, key=lambda row: min(item.center_y for item in row))
    header_top = max(0, min(item.top for item in header_items) - 8)
    header_bottom = max(item.bottom for item in header_items) + 7
    data_bottom = marker.infer_data_bottom(
        items,
        header_bottom,
        image.height,
        main_table_right,
    )
    boundaries = sorted(
        set(
            marker.find_vertical_grid_lines(
                image,
                header_top,
                data_bottom,
                main_table_right,
            )
        )
    )
    if len(boundaries) < 2:
        raise RuntimeError(f"未检测到表格“{table_title}”的列分隔线")

    first_header_left = min(item.left for item in header_items)
    last_header_right = max(item.right for item in header_items)
    if boundaries[0] > first_header_left:
        boundaries.insert(0, max(0, first_header_left - 12))
    if boundaries[-1] < last_header_right:
        boundaries.append(min(main_table_right, last_header_right + 12))

    data_items = [
        item
        for item in items
        if item.top >= header_bottom
        and item.bottom <= data_bottom
        and item.left < main_table_right
    ]
    columns: list[dict[str, Any]] = []
    for index, (column_left, column_right) in enumerate(
        zip(boundaries, boundaries[1:]),
        start=1,
    ):
        if column_right - column_left < 18:
            continue
        column_headers = [
            item
            for item in header_items
            if column_left < item.center_x < column_right
        ]
        if not column_headers:
            continue
        column_data = [
            item
            for item in data_items
            if column_left < item.center_x < column_right
        ]
        truncated_items = [
            item
            for item in column_data
            if ocr_item_looks_truncated(item, column_right, edge_margin_pixels)
        ]
        raw_header = "".join(
            str(getattr(item, "raw_text", "") or item.text)
            for item in sorted(column_headers, key=lambda entry: entry.left)
        ).strip()
        header_text = raw_header or f"第{index}列"
        # OCR 标题可能在列宽变化后由“医嘱...”变成“医嘱名称”。列身份必须
        # 与显示文字解耦，否则成功拓宽后会被误判成原列消失。
        column_key = f"column-{index}"
        handle_x, handle_y, handle_found = find_gray_column_resize_handle(
            image,
            column_right,
            header_top,
            header_bottom,
        )
        columns.append(
            {
                "key": column_key,
                "headerText": header_text,
                "columnLeft": column_left,
                "columnRight": column_right,
                "width": column_right - column_left,
                "resizeHandleX": handle_x,
                "resizeHandleY": handle_y,
                "resizeHandleFound": handle_found,
                "dataItemCount": len(column_data),
                "truncatedTexts": [
                    str(getattr(item, "raw_text", "") or item.text)
                    for item in truncated_items
                ],
            }
        )

    return {
        "headerTop": header_top,
        "dataBottom": data_bottom,
        "titleRect": [title.left, title.top, title.right, title.bottom],
        "headerCenterY": round(
            sum(item.center_y for item in header_items) / len(header_items)
        ),
        "imageWidth": image.width,
        "columns": columns,
        "truncatedColumns": [
            column for column in columns if column["truncatedTexts"]
        ],
    }


def mark_truncated_columns(
    screenshot_path: Path,
    analysis: dict[str, Any],
) -> Path:
    """Draw red boxes around columns judged clipped from the rendered screenshot."""

    with Image.open(screenshot_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    header_top = int(analysis["headerTop"])
    data_bottom = int(analysis["dataBottom"])
    for column in analysis["truncatedColumns"]:
        left = int(column["columnLeft"])
        right = int(column["columnRight"])
        handle_x = int(column.get("resizeHandleX", right))
        handle_y = int(column.get("resizeHandleY", analysis["headerCenterY"]))
        draw.rectangle(
            (left + 1, header_top, right - 1, data_bottom),
            outline=marker.MARK_COLOR,
            width=marker.MARK_WIDTH,
        )
        # 蓝色小圆标出鼠标实际按下点：对应列名右侧的灰色短竖杠中心。
        draw.ellipse(
            (handle_x - 7, handle_y - 7, handle_x + 7, handle_y + 7),
            outline=(0, 102, 255),
            width=3,
        )
    marked_path = screenshot_path.with_name(f"{screenshot_path.stem}_marked.png")
    image.save(marked_path)
    return marked_path


def is_panel_border_pixel(color: tuple[int, int, int]) -> bool:
    red, green, blue = color
    brightness = (red + green + blue) / 3
    return 185 <= brightness <= 245 and max(color) - min(color) <= 20


def find_table_region(
    screenshot_path: Path,
    analysis: dict[str, Any],
    table_right_ratio: float,
) -> tuple[int, int, int, int]:
    """Find the outer panel that owns the configured table title."""

    with Image.open(screenshot_path) as source:
        image = source.convert("RGB")
    pixels = image.load()
    width, height = image.size
    title_left, title_top, _title_right, title_bottom = analysis["titleRect"]
    vertical_top = max(0, int(title_top) - 20)
    vertical_bottom = max(vertical_top + 1, height - 55)

    def vertical_score(x: int) -> int:
        return sum(
            1
            for y in range(vertical_top, vertical_bottom + 1)
            if is_panel_border_pixel(pixels[x, y])
            and sum(pixels[x, y]) / 3 <= 225
        )

    left_candidates = range(0, max(2, min(int(title_left), round(width * 0.12))))
    left = max(left_candidates, key=vertical_score)
    expected_right = round(width * table_right_ratio)
    right_start = max(left + 100, expected_right - 45)
    right_end = min(width - 2, expected_right + 20)
    right = max(range(right_start, right_end + 1), key=vertical_score)

    def horizontal_score(y: int) -> int:
        return sum(
            1
            for x in range(left, right + 1, 2)
            if is_panel_border_pixel(pixels[x, y])
        )

    top_range = range(max(0, int(title_top) - 30), int(title_top) + 1)
    top_scores = [(horizontal_score(y), y) for y in top_range]
    best_top_score = max(score for score, _y in top_scores)
    top = min(y for score, y in top_scores if score >= best_top_score * 0.9)

    bottom_start = min(height - 2, max(int(title_bottom) + 80, int(analysis["dataBottom"]) + 20))
    bottom_end = max(bottom_start, height - 30)
    bottom_scores = [
        (horizontal_score(y), y) for y in range(bottom_start, bottom_end + 1)
    ]
    best_bottom_score = max(score for score, _y in bottom_scores)
    bottom = max(
        y for score, y in bottom_scores if score >= best_bottom_score * 0.9
    )
    return left, top, right, bottom


def mark_table_region(
    screenshot_path: Path,
    analysis: dict[str, Any],
    table_right_ratio: float,
) -> tuple[Path, tuple[int, int, int, int]]:
    """Draw a red outline around the whole panel selected by the table title."""

    region = find_table_region(screenshot_path, analysis, table_right_ratio)
    with Image.open(screenshot_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = region
    draw.rectangle(
        (left + 1, top + 1, right - 1, bottom - 1),
        outline=marker.MARK_COLOR,
        width=marker.MARK_WIDTH,
    )
    marked_path = screenshot_path.with_name(
        f"{screenshot_path.stem}_table_region_marked.png"
    )
    image.save(marked_path)
    return marked_path, region


def current_cursor_handle() -> int | None:
    """Read the current Windows cursor handle without assuming a cursor type."""

    try:
        import ctypes
        from ctypes import wintypes

        class CursorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE),
                ("ptScreenPos", wintypes.POINT),
            ]

        info = CursorInfo()
        info.cbSize = ctypes.sizeof(CursorInfo)
        if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(info)):
            return None
        return int(info.hCursor) if info.hCursor else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def find_column_resize_hotspot(
    screen_x: int,
    screen_y: int,
    column_width: int,
) -> tuple[int, int, bool]:
    """Find the handle by detecting a cursor-style change near the OCR divider."""

    # 只在目标列内部、远离右侧分隔线的位置建立普通光标基准。不能到分隔线
    # 右侧取样，因为那里已经属于相邻列，窄列时还可能落到另一个拖动手柄上。
    inside_distance = max(12, min(30, max(1, column_width // 2)))
    baseline_handles: list[int] = []
    for baseline_x in (
        screen_x - inside_distance,
        screen_x - max(8, inside_distance - 6),
    ):
        mouse.move(coords=(baseline_x, screen_y))
        time.sleep(0.08)
        handle = current_cursor_handle()
        if handle is not None:
            baseline_handles.append(handle)
    if not baseline_handles:
        return screen_x, screen_y, False
    baseline_handle = max(set(baseline_handles), key=baseline_handles.count)

    # 先查最可能的中心位置，再逐像素向左右及上下扩展。网页可以使用
    # 任意 CSS 光标，只要句柄不同于普通表头光标，就视为命中拖动手柄。
    x_offsets = (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, -8, 8)
    y_offsets = (0, -2, 2, -4, 4)
    for y_offset in y_offsets:
        for x_offset in x_offsets:
            candidate_x = screen_x + x_offset
            candidate_y = screen_y + y_offset
            mouse.move(coords=(candidate_x, candidate_y))
            time.sleep(0.06)
            handle = current_cursor_handle()
            if handle is not None and handle != baseline_handle:
                return candidate_x, candidate_y, True
    return screen_x, screen_y, False


def drag_column_resize_handle(
    start: tuple[int, int],
    end: tuple[int, int],
    duration_seconds: float,
) -> None:
    """Keep LEFTDOWN active for the entire native Windows drag movement."""

    import ctypes

    user32 = ctypes.windll.user32
    mouse_event_left_down = 0x0002
    mouse_event_left_up = 0x0004
    user32.SetCursorPos(start[0], start[1])
    time.sleep(0.1)
    # 这里只发送一次 LEFTDOWN；整个移动循环中绝不发送 LEFTUP。
    user32.mouse_event(mouse_event_left_down, 0, 0, 0, 0)
    try:
        # 按住后稍停，让网页表格进入列宽调整状态。
        time.sleep(0.3)
        steps = max(40, round(duration_seconds / 0.025))
        for step in range(1, steps + 1):
            x = round(start[0] + (end[0] - start[0]) * step / steps)
            y = round(start[1] + (end[1] - start[1]) * step / steps)
            user32.SetCursorPos(x, y)
            time.sleep(duration_seconds / steps)
        user32.SetCursorPos(end[0], end[1])
        time.sleep(0.2)
    finally:
        # 到达终点后才发送唯一一次 LEFTUP。
        user32.mouse_event(mouse_event_left_up, 0, 0, 0, 0)


def run_his_expand_table_column(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Scan all visible table columns and expand every visually clipped column."""

    table_title = str(params.get("tableTitle") or "医嘱明细").strip()
    if not table_title:
        raise WorkflowExecutionError("表格标题不能为空")

    expand_pixels = int(params.get("expandPixels", 80))
    max_expand_pixels = int(params.get("maxExpandPixels", 320))
    edge_margin_pixels = int(params.get("edgeMarginPixels", 12))
    configured_table_right_ratio = float(params.get("tableRightRatio", 0.755))
    # 当前 HIS 的医嘱主表位于窗口左侧约 75.5%，右侧是独立的执行记录区域。
    # 旧步骤可能保存了 1.0，这里自动收紧，防止把右侧附属表误当主表。
    table_right_ratio = min(configured_table_right_ratio, 0.755)
    header_search_height = int(params.get("headerSearchHeight", 120))
    layout_wait_seconds = float(params.get("layoutWaitSeconds", 0.6))
    handle_hover_seconds = float(params.get("handleHoverSeconds", 0.4))
    drag_duration_seconds = float(params.get("dragDurationSeconds", 2.0))
    max_drag_count = int(params.get("maxDragCount", 30))
    min_visible_columns = int(params.get("minVisibleColumns", 4))
    save_diagnostics = bool(params.get("_saveDiagnostics", True))
    if not 10 <= expand_pixels <= 500:
        raise WorkflowExecutionError("单次拓宽像素必须在 10～500 之间")
    if not expand_pixels <= max_expand_pixels <= 1200:
        raise WorkflowExecutionError("最大拓宽像素必须不小于单次拓宽，且不超过 1200")
    if not 1 <= edge_margin_pixels <= 40:
        raise WorkflowExecutionError("贴边判定距离必须在 1～40 像素之间")
    if not 0.2 <= configured_table_right_ratio <= 1.0:
        raise WorkflowExecutionError("表格右边界比例必须在 0.2～1.0 之间")
    if not 30 <= header_search_height <= 2000:
        raise WorkflowExecutionError("标题下方查找范围必须在 30～2000 像素之间")
    if not 0 <= layout_wait_seconds <= 10:
        raise WorkflowExecutionError("拖动后等待时间必须在 0～10 秒之间")
    if not 0 <= handle_hover_seconds <= 3:
        raise WorkflowExecutionError("手柄停留时间必须在 0～3 秒之间")
    if not 0.5 <= drag_duration_seconds <= 10:
        raise WorkflowExecutionError("按住拖动时间必须在 0.5～10 秒之间")
    if not 1 <= max_drag_count <= 100:
        raise WorkflowExecutionError("最多拖动次数必须在 1～100 之间")
    if not 2 <= min_visible_columns <= 100:
        raise WorkflowExecutionError("最少可见列数必须在 2～100 之间")

    engine = context.state.get("ocr_engine")
    if engine is None:
        engine = marker.create_ocr_engine()
        context.state["ocr_engine"] = engine
    window = current_window(context)

    def probe() -> dict[str, Any]:
        context.check_cancelled()
        probe_serial = int(context.state.get("table_width_probe_serial", 0)) + 1
        context.state["table_width_probe_serial"] = probe_serial
        probe_name = (
            f"table_width_probe_{context.state['timestamp']}_{probe_serial:02d}.png"
            if save_diagnostics
            else f".rpa_table_width_probe_{context.state['timestamp']}_{probe_serial:02d}.png"
        )
        probe_path = context.output_dir / probe_name
        his.capture_his_window(window, probe_path)
        if save_diagnostics:
            context.state["last_table_width_probe"] = probe_path
            context.emit(
                "info",
                f"表格列宽 OCR 检测截图已保存：{probe_path}",
                {"path": str(probe_path)},
            )
        try:
            analysis = analyze_table_columns_width(
                engine,
                probe_path,
                table_title,
                table_right_ratio,
                header_search_height,
                edge_margin_pixels,
            )
        finally:
            if not save_diagnostics:
                try:
                    probe_path.unlink(missing_ok=True)
                except OSError:
                    pass
        analysis["probePath"] = str(probe_path)
        return analysis

    requested_by_column: dict[str, int] = {}
    initial_width_by_column: dict[str, int] = {}
    blocked_columns: set[str] = set()
    previous_drag: tuple[str, int] | None = None
    drag_count = 0
    while True:
        try:
            analysis = probe()
        except RuntimeError as exc:
            raise WorkflowExecutionError(f"自动扫描表格列失败：{exc}") from exc

        visible_columns = list(analysis["columns"])
        merged_headers = [
            str(column["headerText"])
            for column in visible_columns
            if len(marker.normalize_ocr_text(column["headerText"])) > 12
        ]
        if len(visible_columns) < min_visible_columns or merged_headers:
            details = (
                f"仅识别到 {len(visible_columns)} 列"
                if len(visible_columns) < min_visible_columns
                else "检测到疑似被 OCR 合并的表头：" + "，".join(merged_headers)
            )
            diagnostic_hint = (
                f"请查看检测截图：{analysis['probePath']}"
                if save_diagnostics
                else "请检查配置的表格标题和主表右边界"
            )
            raise WorkflowExecutionError(
                f"表格区域定位不可靠（{details}），已停止拖动；{diagnostic_hint}"
            )

        if save_diagnostics:
            marked_path = mark_truncated_columns(Path(analysis["probePath"]), analysis)
            table_region_path, table_region = mark_table_region(
                Path(analysis["probePath"]),
                analysis,
                table_right_ratio,
            )
            truncated_names = [
                str(column["headerText"]) for column in analysis["truncatedColumns"]
            ]
            context.emit(
                "info",
                f"显示不全列标注图已保存：{marked_path}；"
                f"识别列：{('，'.join(truncated_names) if truncated_names else '无')}",
                {"path": str(marked_path), "columns": truncated_names},
            )
            context.emit(
                "info",
                f"配置表“{table_title}”区域标注图已保存：{table_region_path}",
                {"path": str(table_region_path), "region": list(table_region)},
            )
        columns_by_key = {column["key"]: column for column in visible_columns}
        for column in visible_columns:
            initial_width_by_column.setdefault(column["key"], int(column["width"]))

        if previous_drag is not None:
            previous_key, previous_width = previous_drag
            current = columns_by_key.get(previous_key)
            if current is None or int(current["width"]) <= previous_width + 2:
                name = str(current["headerText"]) if current else previous_key
                raise WorkflowExecutionError(
                    f"已定位“{name}”列分隔线，但拖动后列宽没有变化；"
                    "请确认该表格允许鼠标调整列宽"
                )
            previous_drag = None

        truncated_columns = list(analysis["truncatedColumns"])
        if not truncated_columns:
            expanded_columns = {
                str(column["headerText"]): max(
                    0,
                    int(column["width"])
                    - initial_width_by_column.get(column["key"], int(column["width"])),
                )
                for column in analysis["columns"]
                if requested_by_column.get(column["key"], 0) > 0
            }
            message = (
                "大表可见列均未发现省略号或右边界截断，无需拓宽"
                if not expanded_columns
                else "已完成表格列拓宽并复检："
                + "，".join(
                    f"{name}约{pixels}像素"
                    for name, pixels in expanded_columns.items()
                )
            )
            context.emit("info", message, None)
            return {
                "expanded": bool(expanded_columns),
                "expandedColumns": expanded_columns,
                "complete": True,
                "visibleColumnCount": len(analysis["columns"]),
            }

        eligible = [
            column
            for column in truncated_columns
            if column["key"] not in blocked_columns
            and requested_by_column.get(column["key"], 0) < max_expand_pixels
        ]
        if not eligible or drag_count >= max_drag_count:
            unresolved = [str(column["headerText"]) for column in truncated_columns]
            context.emit(
                "warning",
                "下列可见列仍疑似显示不全，但已达到拖动限制或屏幕空间不足："
                + "，".join(unresolved),
                {"columns": unresolved},
            )
            return {
                "expanded": bool(requested_by_column),
                "expandedColumns": requested_by_column,
                "complete": False,
                "unresolvedColumns": unresolved,
            }

        rect = window.rectangle()
        # 从最右侧开始处理，这样拓宽右侧列不会改变左侧分隔线坐标。
        target = max(eligible, key=lambda column: int(column["columnRight"]))
        column_key = str(target["key"])
        column_name = str(target["headerText"])
        current_right = int(target["columnRight"])
        requested = requested_by_column.get(column_key, 0)
        remaining = max_expand_pixels - requested
        drag_pixels = min(
            expand_pixels,
            remaining,
            rect.right - rect.left - current_right - 20,
        )
        if drag_pixels < 10:
            blocked_columns.add(column_key)
            context.emit(
                "warning",
                f"“{column_name}”列右侧屏幕空间不足，跳过后继续检查其他列",
                None,
            )
            continue
        handle_x = int(target.get("resizeHandleX", current_right))
        handle_y = int(target.get("resizeHandleY", analysis["headerCenterY"]))
        screen_x = rect.left + handle_x
        screen_y = rect.top + handle_y
        if not bool(target.get("resizeHandleFound", False)):
            context.emit(
                "warning",
                f"未在“{column_name}”列右侧检测到灰色短竖杠，停止拖动以避免误点",
                {"point": [screen_x, screen_y]},
            )
            blocked_columns.add(column_key)
            continue
        hotspot_x, hotspot_y, cursor_changed = find_column_resize_hotspot(
            screen_x,
            screen_y,
            int(target["width"]),
        )
        if not cursor_changed:
            context.emit(
                "warning",
                f"已找到“{column_name}”列右侧灰色短竖杠，但附近鼠标样式没有变化，停止拖动",
                {"point": [screen_x, screen_y]},
            )
            blocked_columns.add(column_key)
            continue
        context.emit(
            "info",
            f"“{column_name}”列检测到 {len(target['truncatedTexts'])} 个疑似截断单元格，"
            f"鼠标样式已在灰色短竖杠 ({hotspot_x}, {hotspot_y}) 发生变化，"
            f"向右拓宽 {drag_pixels} 像素",
            {"texts": target["truncatedTexts"], "point": [hotspot_x, hotspot_y]},
        )
        mouse.move(coords=(hotspot_x, hotspot_y))
        context.wait(handle_hover_seconds)
        previous_drag = (column_key, int(target["width"]))
        drag_column_resize_handle(
            (hotspot_x, hotspot_y),
            (hotspot_x + drag_pixels, hotspot_y),
            drag_duration_seconds,
        )
        requested_by_column[column_key] = requested + drag_pixels
        drag_count += 1
        context.wait(layout_wait_seconds)


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


def run_his_expand_capture_full_table(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Expand clipped columns on both horizontal views and capture both sides."""

    scroll_wait_seconds = float(params.get("scrollLayoutWaitSeconds", 0.5))
    if not 0 <= scroll_wait_seconds <= 10:
        raise WorkflowExecutionError("横向滚动后等待时间必须在 0～10 秒之间")

    prefix = safe_file_component(
        params.get("filePrefix")
        or f"his_inpatient_{context.variables.get('hospitalization_number', 'unknown')}"
    )
    window = current_window(context)
    timestamp = context.state["timestamp"]
    left_path = context.output_dir / f"{prefix}_current_{timestamp}.png"
    right_path = context.output_dir / f"{prefix}_rightmost_{timestamp}.png"
    expand_params = dict(params)
    # 组合模块最终只保留左右两张成品图；OCR 探测图使用后立即删除。
    expand_params["_saveDiagnostics"] = False

    context.emit("info", "组合操作 [1/6]：拓宽当前位置显示不全的表格列", None)
    left_expand_result = run_his_expand_table_column(context, expand_params)

    context.check_cancelled()
    context.emit("info", "组合操作 [2/6]：截取当前位置", None)
    his.capture_his_window(window, left_path)
    context.state["current_screenshot"] = left_path
    context.state["screenshots"].append(left_path)
    context.emit("info", f"当前位置截图：{left_path}", {"path": str(left_path)})

    context.check_cancelled()
    context.emit("info", "组合操作 [3/6]：检测横向滚动条并拖到最右端", None)
    scrollbar = his.detect_horizontal_scrollbar(window, left_path)
    if scrollbar is None:
        context.emit(
            "warning",
            "未检测到横向滚动条，已保存当前位置截图；右侧拓宽、右侧截图和复位无需执行",
            None,
        )
        return {
            "capturedLeft": True,
            "capturedRight": False,
            "leftPath": str(left_path),
            "leftExpansion": left_expand_result,
            "restoredLeft": True,
        }

    current_position, rightmost_position = scrollbar
    moved_right = False
    restored_left = False
    right_expand_result: dict[str, Any] | None = None
    try:
        his.drag_scrollbar(current_position, rightmost_position)
        moved_right = True
        context.wait(scroll_wait_seconds)

        context.check_cancelled()
        context.emit("info", "组合操作 [4/6]：拓宽最右侧视图中显示不全的表格列", None)
        right_expand_result = run_his_expand_table_column(context, expand_params)

        context.check_cancelled()
        context.emit("info", "组合操作 [5/6]：截取最右侧当前位置", None)
        # 拓宽列会增加表格总宽度，因此先截图检测新的滑块范围；若产生了新的
        # 右侧空间，则继续拖到新的最右端，再覆盖保存最终截图。
        his.capture_his_window(window, right_path)
        right_state = his.detect_horizontal_scrollbar(window, right_path)
        if right_state is not None and right_state[0] != right_state[1]:
            his.drag_scrollbar(right_state[0], right_state[1])
            context.wait(scroll_wait_seconds)
            his.capture_his_window(window, right_path)
        context.state["screenshots"].append(right_path)
        context.emit("info", f"最右端截图：{right_path}", {"path": str(right_path)})
    finally:
        if moved_right:
            context.emit("info", "组合操作 [6/6]：将横向滚动条拖回最左端", None)
            restore_source: Path | None = right_path if right_path.is_file() else None
            if restore_source is None:
                # 即使右侧拓宽阶段报错，也用第二张成品图判断滑块位置并复位；
                # 不额外留下恢复用或 OCR 诊断截图。
                try:
                    his.capture_his_window(window, right_path)
                    restore_source = right_path
                except Exception:
                    restore_source = None
            restore_state = (
                his.detect_horizontal_scrollbar(window, restore_source)
                if restore_source is not None
                else None
            )
            if restore_state is not None:
                rect = window.rectangle()
                leftmost_position = (rect.left + 1, restore_state[0][1])
                his.drag_scrollbar(restore_state[0], leftmost_position)
                time.sleep(0.35)
                restored_left = True
                context.emit("info", "横向滚动条已恢复到最左端", None)
            else:
                context.emit(
                    "warning",
                    "未能重新识别横向滚动条，无法确认滑块是否已恢复到最左端",
                    None,
                )

    return {
        "capturedLeft": True,
        "capturedRight": right_path.is_file(),
        "leftPath": str(left_path),
        "rightPath": str(right_path),
        "leftExpansion": left_expand_result,
        "rightExpansion": right_expand_result,
        "restoredLeft": restored_left,
    }


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


def run_visual_map_page(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Build or load a reusable visual model for the current application page."""

    window = current_window(context)
    page = visual_page_id(params)
    page_map = get_visual_page_map(context, window, page)
    if bool(params.get("forceRefresh", False)):
        page_map.update(
            {
                "targets": {},
                "targetDetails": {},
                "elements": [],
                "ambiguousTargets": {},
                "ocrItems": None,
                "visualImage": None,
                "regions": [],
                "relationships": [],
                "visualCandidates": [],
                "unclassified": [],
                "persistedRawOcr": [],
            }
        )
    items = ensure_visual_ocr(context, window, page_map)
    path = visual_map_path(page_map["signature"])
    return {
        "page": page,
        "layoutPath": str(path),
        "textCount": len(items),
        "elementCount": len(page_map.get("elements", [])),
        "controlCandidateCount": len(page_map.get("visualCandidates", [])),
        "reusableTargetCount": len(page_map.get("targets", {})),
        "ambiguousTargetCount": len(page_map.get("ambiguousTargets", {})),
        "unclassifiedCount": len(page_map.get("unclassified", [])),
        "regionCount": len(page_map.get("regions", [])),
        "relationshipCount": len(page_map.get("relationships", [])),
    }


def run_visual_hover_object(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Hover a named object and capture the resulting menu/popover page state."""

    target_name = str(params.get("targetName") or "").strip()
    if not target_name:
        raise WorkflowExecutionError("悬浮对象名称不能为空")
    timeout = float(params.get("timeoutSeconds", 5))
    wait_seconds = float(params.get("hoverWaitSeconds", 0.5))
    if not 0 <= wait_seconds <= 10:
        raise WorkflowExecutionError("悬浮后等待时间必须在 0～10 秒之间")
    source_page = visual_page_id(params)
    hover_page = str(
        params.get("hoverPage") or f"{source_page}:hover:{target_name}"
    ).strip()[:80]
    window = current_window(context)
    point = visual_text_point(
        context,
        window,
        target_name,
        page=source_page,
        role="hover-trigger",
    )
    if point is not None:
        locator_mode = "visual-map"
    else:
        control = find_named_his_control(window, target_name, None, timeout)
        if control is None:
            raise WorkflowExecutionError(
                f"无法在视觉页面布局或 UIA 中找到悬浮对象“{target_name}”"
            )
        rect = control.rectangle()
        point = (rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        locator_mode = "uia-control"
    mouse.move(coords=point)
    context.wait(wait_seconds)
    hover_map = get_visual_page_map(context, window, hover_page)
    hover_items = ensure_visual_ocr(context, window, hover_map)
    return {
        "targetName": target_name,
        "point": list(point),
        "locatorMode": locator_mode,
        "hoverPage": hover_page,
        "layoutPath": str(visual_map_path(hover_map["signature"])),
        "visibleTextCount": len(hover_items),
    }


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
            "visual.map_page",
            "识别并保存当前页面布局",
            "视觉",
            "首次截图并建立通用页面模型；已有匹配模型时直接加载。",
            (
                visual_page_field(),
                field("forceRefresh", "强制重新识别", "boolean", False),
            ),
            run_visual_map_page,
        ),
        ModuleDefinition(
            "visual.hover_object",
            "悬浮对象并识别展开布局",
            "视觉",
            "悬浮到命名对象上，并将出现的菜单或弹出层保存为独立页面状态。",
            (
                field("targetName", "悬浮对象名称", "text", ""),
                visual_page_field(),
                field("hoverPage", "展开状态页面标识", "text", ""),
                field("hoverWaitSeconds", "悬浮后等待（秒）", "number", 0.5, min=0, max=10, step=0.1),
                field("timeoutSeconds", "UIA 查找超时（秒）", "number", 5, min=0, max=60, step=1),
            ),
            run_visual_hover_object,
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
                visual_page_field(),
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
                visual_page_field(),
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
                visual_page_field(),
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
                visual_page_field(),
                field("timeoutSeconds", "控件查找超时（秒）", "number", 5, min=0, max=60, step=1),
            ),
            run_his_click_object,
        ),
        ModuleDefinition(
            "his.input_hospitalization",
            "填写住院号并回车",
            "HIS",
            "优先使用 UIA，失败时沿用已验证的相对布局定位。",
            (
                field("value", "住院号/变量", "text", "${hospitalization_number}"),
                visual_page_field(),
            ),
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
            "his.expand_table_column",
            "自动拓宽显示不全的列",
            "HIS",
            "OCR 扫描大表所有可见列，检测被右边界截断的值并拖动对应列分隔线。",
            (
                field("tableTitle", "表格标题/定位文字", "text", "医嘱明细"),
                field("expandPixels", "每次向右拓宽（像素）", "number", 80, min=10, max=500, step=10),
                field("maxExpandPixels", "每列最大拓宽（像素）", "number", 320, min=10, max=1200, step=10),
                field("edgeMarginPixels", "OCR 文字贴近右边界距离（像素）", "number", 12, min=1, max=40, step=1),
                field("headerSearchHeight", "标题下方查找范围（像素）", "number", 120, min=30, max=2000, step=10),
                field("tableRightRatio", "主表右边界（窗口宽度比例）", "number", 0.755, min=0.2, max=1, step=0.001),
                field("handleHoverSeconds", "按下前在灰色短竖杠停留（秒）", "number", 0.4, min=0, max=3, step=0.1),
                field("dragDurationSeconds", "按住向右拖动时间（秒）", "number", 2.0, min=0.5, max=10, step=0.1),
                field("layoutWaitSeconds", "拖动后等待（秒）", "number", 0.6, min=0, max=10, step=0.1),
                field("maxDragCount", "最多拖动次数", "number", 30, min=1, max=100, step=1),
                field("minVisibleColumns", "安全校验最少可见列数", "number", 4, min=2, max=100, step=1),
            ),
            run_his_expand_table_column,
        ),
        ModuleDefinition(
            "his.expand_capture_full_table",
            "拓宽并截取完整表格",
            "HIS",
            "依次拓宽左侧可见列、截图、滚到最右侧、拓宽右侧可见列、截图并恢复到最左侧。",
            (
                field("tableTitle", "表格标题/定位文字", "text", "医嘱明细"),
                field("filePrefix", "截图文件名前缀", "text", "his_inpatient_${hospitalization_number}"),
                field("expandPixels", "每次向右拓宽（像素）", "number", 80, min=10, max=500, step=10),
                field("maxExpandPixels", "每列最大拓宽（像素）", "number", 320, min=10, max=1200, step=10),
                field("edgeMarginPixels", "OCR 文字贴近右边界距离（像素）", "number", 12, min=1, max=40, step=1),
                field("headerSearchHeight", "标题下方查找范围（像素）", "number", 120, min=30, max=2000, step=10),
                field("tableRightRatio", "主表右边界（窗口宽度比例）", "number", 0.755, min=0.2, max=1, step=0.001),
                field("handleHoverSeconds", "按下前在灰色短竖杠停留（秒）", "number", 0.4, min=0, max=3, step=0.1),
                field("dragDurationSeconds", "按住向右拖动时间（秒）", "number", 2.0, min=0.5, max=10, step=0.1),
                field("layoutWaitSeconds", "拓宽后等待（秒）", "number", 0.6, min=0, max=10, step=0.1),
                field("scrollLayoutWaitSeconds", "横向滚动后等待（秒）", "number", 0.5, min=0, max=10, step=0.1),
                field("maxDragCount", "每侧最多拖动次数", "number", 30, min=1, max=100, step=1),
                field("minVisibleColumns", "安全校验最少可见列数", "number", 4, min=2, max=100, step=1),
            ),
            run_his_expand_capture_full_table,
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
