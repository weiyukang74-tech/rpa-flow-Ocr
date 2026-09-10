"""Built-in OCR-first modules for generic Windows desktop workflows."""

from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageGrab

STUDIO_ROOT = Path(__file__).resolve().parent

import his_automation as window_adapter
import ocr_marker as marker
from visual_shape_detection import (
    checkbox_bounds_match_label,
    detect_foreground_panel,
    detect_table_regions,
    detect_visual_control_candidates,
    find_checkbox_square,
    find_input_rectangle,
    input_bounds_match_label,
    match_checkbox_candidate,
    match_input_candidate,
)
from visual_state import (
    changed_region,
    descriptor_distance,
    is_interactive_overlay_change,
    is_local_change,
    structure_descriptor,
)
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
    window = context.state.get("window")
    if window is None:
        raise WorkflowExecutionError("尚未连接目标窗口，请先执行“连接窗口”步骤")
    return window


def run_window_activate(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    title = str(params.get("windowTitle") or window_adapter.WINDOW_TITLE).strip()
    if not title:
        raise WorkflowExecutionError("窗口标题不能为空")
    window_adapter.WINDOW_TITLE = title
    marker.base.WINDOW_TITLE = title
    window = window_adapter.activate_his_window()
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
        # 最大化属于顶层窗口状态，不依赖应用内部控件树或按钮位置。
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        context.wait(float(params.get("layoutWaitSeconds", 0.6)))
        if not user32.IsZoomed(hwnd):
            raise WorkflowExecutionError(
                "Windows 已收到最大化命令，但目标顶层窗口没有进入最大化状态"
            )
        context.emit("info", "目标窗口已最大化", None)
    else:
        context.emit("info", "目标窗口已经是最大化状态", None)
    rect = window.rectangle()
    return {
        "wasAlreadyMaximized": was_maximized,
        "hwnd": hwnd,
        "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
    }


VISUAL_MAP_ROOT = STUDIO_ROOT / "configs" / "visual-maps"


def visual_page_id(params: dict[str, Any]) -> str:
    """Return a user-defined logical page key without assuming any application."""

    page = str(params.get("visualPage") or "current-page").strip()
    if not page:
        page = "current-page"
    return page[:80]


def visual_window_signature(
    window: Any,
    page: str,
    state_fingerprint: str,
) -> dict[str, Any]:
    rect = window.rectangle()
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise WorkflowExecutionError("目标窗口没有有效的屏幕区域")
    return {
        "locatorVersion": 6,
        "page": page,
        "stateFingerprint": state_fingerprint,
        "windowTitle": window_adapter.normalize_text(window.window_text()),
        "className": str(getattr(window.element_info, "class_name", "") or ""),
        "width": width,
        "height": height,
        "dpiScale": round(float(window_adapter.window_dpi_scale(window)), 3),
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
        "schemaVersion": 6,
        "classificationVersion": page_map.get("classificationVersion", 0),
        "signature": page_map["signature"],
        "targets": page_map["targets"],
        "targetDetails": page_map.get("targetDetails", {}),
        "elements": page_map.get("elements", []),
        "ambiguousTargets": page_map.get("ambiguousTargets", {}),
        "structureDescriptor": page_map.get("structureDescriptor", {}),
        "parentSignature": page_map.get("parentSignature"),
        "changeRegion": page_map.get("changeRegion"),
        "rawOcr": raw_ocr,
        "regions": page_map.get("regions", []),
        "relationships": page_map.get("relationships", []),
        "visualCandidates": page_map.get("visualCandidates", []),
        "unclassified": page_map.get("unclassified", []),
        "performance": page_map.get("performance", {}),
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


VISUAL_CLASSIFICATION_VERSION = 3
VISUAL_CHECKBOX_LOCATOR_VERSION = 3


def classify_visual_page(page_map: dict[str, Any]) -> None:
    """Preclassify a new OCR page once and persist reusable semantic controls."""

    classification_started = time.perf_counter()
    items = page_map.get("ocrItems")
    image = page_map.get("visualImage")
    if not isinstance(items, list) or not isinstance(image, Image.Image):
        return

    elements: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    occurrences: dict[str, list[int]] = {}
    actionable_occurrences: dict[str, list[int]] = {}
    resolved_text_ids: set[str] = set()
    primitive_started = time.perf_counter()
    foreground = None if page_map.get("ocrBounds") else detect_foreground_panel(image)
    foreground_bounds = foreground.get("bounds") if foreground else None
    table_regions = [] if page_map.get("ocrBounds") else detect_table_regions(image)
    if foreground_bounds is not None:
        table_regions = [
            table
            for table in table_regions
            if point_is_inside_bounds(
                [
                    (table["bounds"][0] + table["bounds"][2]) // 2,
                    (table["bounds"][1] + table["bounds"][3]) // 2,
                ],
                foreground_bounds,
            )
        ]

    ocr_bounds = page_map.get("ocrBounds")
    if (
        isinstance(ocr_bounds, list)
        and len(ocr_bounds) == 4
        and all(isinstance(value, int) for value in ocr_bounds)
    ):
        control_inventory = detect_visual_control_candidates(
            image.crop(tuple(ocr_bounds))
        )
        for group in control_inventory.values():
            for candidate in group:
                candidate["bounds"] = [
                    candidate["bounds"][0] + ocr_bounds[0],
                    candidate["bounds"][1] + ocr_bounds[1],
                    candidate["bounds"][2] + ocr_bounds[0],
                    candidate["bounds"][3] + ocr_bounds[1],
                ]
                candidate["center"] = [
                    candidate["center"][0] + ocr_bounds[0],
                    candidate["center"][1] + ocr_bounds[1],
                ]
    else:
        control_inventory = detect_visual_control_candidates(image)
    primitive_seconds = time.perf_counter() - primitive_started

    def is_in_foreground(item: Any) -> bool:
        return point_is_inside_bounds(
            [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
            foreground_bounds,
        )

    def table_for_item(item: Any) -> tuple[int, dict[str, Any]] | None:
        center_x = (item.left + item.right) // 2
        center_y = (item.top + item.bottom) // 2
        for table_index, table in enumerate(table_regions):
            left, top, right, bottom = table["bounds"]
            if left <= center_x <= right and top <= center_y <= bottom:
                return table_index, table
        return None

    def candidate_allowed(candidate: dict[str, Any]) -> bool:
        if foreground_bounds is not None and not point_is_inside_bounds(
            candidate.get("center"), foreground_bounds
        ):
            return False
        return not any(
            point_is_inside_bounds(candidate.get("center"), table["bounds"])
            for table in table_regions
        )

    field_candidates = [
        candidate
        for candidate in control_inventory.get("fieldRectangles", [])
        if candidate_allowed(candidate)
    ]
    square_candidates = [
        candidate
        for candidate in control_inventory.get("squareControls", [])
        if candidate_allowed(candidate)
    ]
    candidates.extend(
        {
            "id": f"field-candidate-{index:04d}",
            "kind": "field-rectangle",
            "possibleRoles": ["input", "select", "date"],
            **candidate,
        }
        for index, candidate in enumerate(field_candidates)
    )
    candidates.extend(
        {
            "id": f"square-candidate-{index:04d}",
            "kind": "square-control",
            "possibleRoles": ["checkbox", "toggle", "icon"],
            **candidate,
        }
        for index, candidate in enumerate(square_candidates)
    )

    table_first_rows: dict[int, int] = {}
    for index, table in enumerate(table_regions):
        centers = [
            (item.top + item.bottom) // 2
            for item in items
            if table_for_item(item) is not None and table_for_item(item)[0] == index
        ]
        if centers:
            table_first_rows[index] = min(centers)

    for index, item in enumerate(items):
        element_id = f"text-{index:04d}"
        normalized = marker.normalize_ocr_text(item.text)
        occurrences.setdefault(normalized, []).append(index)
        table_match = table_for_item(item)
        if foreground_bounds is not None and not is_in_foreground(item):
            kind = "background-text"
            possible_roles = ["background-text"]
            resolved_text_ids.add(element_id)
        elif table_match is None:
            kind = "text"
            possible_roles = ["text", "button", "tab", "menu-item", "link"]
            actionable_occurrences.setdefault(normalized, []).append(index)
        else:
            table_index, _ = table_match
            center_y = (item.top + item.bottom) // 2
            first_y = table_first_rows.get(table_index, center_y)
            kind = "table-header" if center_y <= first_y + 12 else "table-cell-text"
            possible_roles = [kind]
            resolved_text_ids.add(element_id)
        elements.append(
            {
                "id": element_id,
                "kind": kind,
                "text": item.text,
                "rawText": item.raw_text,
                "bounds": [item.left, item.top, item.right, item.bottom],
                "center": [
                    (item.left + item.right) // 2,
                    (item.top + item.bottom) // 2,
                ],
                "confidence": round(float(item.score), 5),
                "possibleRoles": possible_roles,
            }
        )

    ambiguous: dict[str, list[str]] = {}
    for normalized, indexes in occurrences.items():
        if not normalized:
            continue
        ids = [f"text-{index:04d}" for index in indexes]
        if len(indexes) > 1:
            ambiguous[normalized] = ids
        actionable_indexes = actionable_occurrences.get(normalized, [])
        foreground_indexes = [
            index for index in actionable_indexes if is_in_foreground(items[index])
        ]
        if foreground_bounds is not None:
            if len(foreground_indexes) != 1:
                continue
            index = foreground_indexes[0]
        elif len(actionable_indexes) == 1:
            index = actionable_indexes[0]
        else:
            continue
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

    # Remove stale auto-detected controls before rebuilding the complete page.
    for key in list(page_map.get("targets", {})):
        if key.startswith(("input:", "select:", "checkbox:")):
            page_map["targets"].pop(key, None)
            page_map.get("targetDetails", {}).pop(key, None)

    for index, label in enumerate(items):
        if (
            table_for_item(label) is not None
            or (foreground_bounds is not None and not is_in_foreground(label))
        ):
            continue
        normalized = marker.normalize_ocr_text(label.text)
        if not normalized:
            continue
        label_id = f"text-{index:04d}"
        unique_label = len(actionable_occurrences.get(normalized, [])) == 1

        rectangle = match_input_candidate(field_candidates, label)
        if rectangle is not None and not any(
            point_is_inside_bounds(rectangle["center"], table["bounds"])
            for table in table_regions
        ):
            candidate = {
                "id": f"field-{len(candidates):04d}",
                "kind": "field-rectangle",
                "possibleRoles": ["input", "select"],
                "labelText": label.text,
                "labelId": label_id,
                **rectangle,
            }
            elements.append(candidate)
            resolved_text_ids.add(label_id)
            if unique_label:
                for role in ("input", "select"):
                    key = f"{role}:{label.text}"
                    page_map["targets"][key] = list(rectangle["center"])
                    page_map["targetDetails"][key] = {
                        "role": "field",
                        "possibleRoles": ["input", "select"],
                        "name": label.text,
                        "bounds": rectangle["bounds"],
                        "clickPoint": rectangle["center"],
                        "labelBounds": [label.left, label.top, label.right, label.bottom],
                        "relation": rectangle["relation"],
                        "confidence": rectangle["score"],
                        "source": "ocr-page-classifier",
                    }

        square = match_checkbox_candidate(square_candidates, label)
        if square is not None and not any(
            point_is_inside_bounds(square["center"], table["bounds"])
            for table in table_regions
        ):
            candidate = {
                "id": f"checkbox-{len(candidates):04d}",
                "kind": "checkbox",
                "possibleRoles": ["checkbox", "toggle"],
                "labelText": label.text,
                "labelId": label_id,
                **square,
            }
            elements.append(candidate)
            resolved_text_ids.add(label_id)
            if unique_label:
                key = f"checkbox:{label.text}"
                page_map["targets"][key] = list(square["center"])
                page_map["targetDetails"][key] = {
                    "role": "checkbox",
                    "possibleRoles": ["checkbox", "toggle"],
                    "name": label.text,
                    "bounds": square["bounds"],
                    "clickPoint": square["center"],
                    "labelBounds": [label.left, label.top, label.right, label.bottom],
                    "relation": square["relation"],
                    "confidence": square["score"],
                    "source": "ocr-page-classifier",
                    "locatorVersion": VISUAL_CHECKBOX_LOCATOR_VERSION,
                }

    for table_index, table in enumerate(table_regions):
        table_element = {"id": f"table-{table_index:03d}", **table}
        elements.append(table_element)
        candidates.append(table_element)

    base_regions = [
        region
        for region in page_map.get("regions", [])
        if region.get("type") not in {"table", "foreground-dialog"}
    ]
    if foreground is not None:
        base_regions.append(
            {
                "id": "foreground-dialog-000",
                "type": "foreground-dialog",
                **foreground,
            }
        )
    page_map["regions"] = base_regions + [
        {"id": f"table-region-{index:03d}", "type": "table", **table}
        for index, table in enumerate(table_regions)
    ]
    page_map["elements"] = elements
    page_map["ambiguousTargets"] = ambiguous
    page_map["visualCandidates"] = candidates
    page_map["unclassified"] = [
        f"text-{index:04d}"
        for index in range(len(items))
        if f"text-{index:04d}" not in resolved_text_ids
    ]
    page_map["classificationVersion"] = VISUAL_CLASSIFICATION_VERSION
    page_map.setdefault("performance", {}).update(
        {
            "primitiveDetectionSeconds": round(primitive_seconds, 4),
            "classificationSeconds": round(
                time.perf_counter() - classification_started,
                4,
            ),
            "fieldCandidateCount": len(field_candidates),
            "squareCandidateCount": len(square_candidates),
        }
    )


def capture_visual_window(window: Any) -> Image.Image:
    rect = window.rectangle()
    with ImageGrab.grab(
        bbox=(rect.left, rect.top, rect.right, rect.bottom)
    ) as screenshot:
        return screenshot.convert("RGB")


def capture_stable_visual_window(
    context: ExecutionContext,
    window: Any,
    timeout: float = 2.0,
    minimum_wait: float = 0.0,
    stable_samples: int = 1,
    content_sensitive: bool = False,
) -> Image.Image:
    """Wait briefly for rendering to settle before choosing a page state."""

    started = time.monotonic()
    previous = capture_visual_window(window)
    previous_descriptor = structure_descriptor(previous)
    deadline = time.monotonic() + max(0.2, timeout)
    stable_count = 0
    while time.monotonic() < deadline:
        context.wait(0.2)
        current = capture_visual_window(window)
        current_descriptor = structure_descriptor(current)
        structure_stable = descriptor_distance(previous_descriptor, current_descriptor) <= 0.008
        content_change = changed_region(previous, current, threshold=16, padding=0)
        content_stable = (
            not content_sensitive
            or content_change is None
            or float(content_change.get("changedPixelRatio", 1.0)) <= 0.0005
        )
        if structure_stable and content_stable:
            stable_count += 1
        else:
            stable_count = 0
        if (
            time.monotonic() - started >= max(0.0, minimum_wait)
            and stable_count >= max(1, stable_samples)
        ):
            return current
        previous, previous_descriptor = current, current_descriptor
    return previous


def descriptor_fingerprint(descriptor: dict[str, Any]) -> str:
    value = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def same_visual_page_family(
    candidate: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    keys = ("locatorVersion", "page", "windowTitle", "className", "width", "height", "dpiScale")
    return all(candidate.get(key) == expected.get(key) for key in keys)


def persisted_page_candidates(expected: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not VISUAL_MAP_ROOT.is_dir():
        return candidates
    paths = sorted(
        VISUAL_MAP_ROOT.glob("page-*/layout.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        signature = value.get("signature")
        descriptor = value.get("structureDescriptor")
        if (
            isinstance(signature, dict)
            and isinstance(descriptor, dict)
            and same_visual_page_family(signature, expected)
        ):
            candidates.append(value)
    return candidates


def visual_page_image(page_map: dict[str, Any]) -> Image.Image | None:
    """Return a comparable full-window image for a memory or persisted page map."""

    for key in ("currentSnapshot", "visualImage"):
        image = page_map.get(key)
        if isinstance(image, Image.Image):
            return image
    signature = page_map.get("signature")
    if not isinstance(signature, dict):
        return None
    reference_path = visual_reference_path(signature)
    if not reference_path.is_file():
        return None
    try:
        with Image.open(reference_path) as reference:
            return reference.convert("RGB")
    except OSError:
        return None


def mapped_foreground_bounds(page_map: dict[str, Any]) -> list[int] | None:
    """Return the foreground panel recorded by a map, never its live snapshot."""

    for region in page_map.get("regions", []):
        if not isinstance(region, dict) or region.get("type") != "foreground-dialog":
            continue
        bounds = region.get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4:
            return bounds

    image = page_map.get("visualImage")
    if not isinstance(image, Image.Image):
        signature = page_map.get("signature")
        if isinstance(signature, dict):
            reference = visual_reference_path(signature)
            if reference.is_file():
                try:
                    with Image.open(reference) as stored:
                        image = stored.convert("RGB")
                except OSError:
                    image = None
    if not isinstance(image, Image.Image):
        return None
    foreground = detect_foreground_panel(image)
    bounds = foreground.get("bounds") if isinstance(foreground, dict) else None
    return bounds if isinstance(bounds, list) and len(bounds) == 4 else None


def point_is_inside_bounds(point: Any, bounds: Any) -> bool:
    if not (
        isinstance(point, (list, tuple))
        and len(point) >= 2
        and isinstance(bounds, (list, tuple))
        and len(bounds) == 4
    ):
        return False
    try:
        x, y = float(point[0]), float(point[1])
        left, top, right, bottom = (float(value) for value in bounds)
    except (TypeError, ValueError):
        return False
    return left <= x <= right and top <= y <= bottom


def announce_visual_state(
    context: ExecutionContext,
    page_map: dict[str, Any],
) -> None:
    signature = page_map["signature"]
    key = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    if context.state.get("active_visual_state") == key:
        return
    context.state["active_visual_state"] = key
    event = page_map.get("stateEvent")
    fingerprint = signature.get("stateFingerprint")
    if event == "created":
        if is_interactive_overlay_change(page_map.get("changeRegion")):
            message = (
                "检测到新的局部交互状态（菜单、日历或弹层），"
                f"将建立独立 Map；状态={fingerprint}"
            )
        else:
            message = f"检测到新的页面结构，将建立独立 Map；状态={fingerprint}"
    else:
        message = f"已匹配并复用页面 Map；状态={fingerprint}"
    context.emit("info", message, None)


def get_visual_page_map(
    context: ExecutionContext,
    window: Any,
    page: str = "current-page",
    parent_page_map: dict[str, Any] | None = None,
    force_new: bool = False,
    prefer_changed_region: bool = False,
    target_key: str | None = None,
) -> dict[str, Any]:
    transition = context.state.get("pending_visual_page_transition")
    if isinstance(transition, dict) and transition.get("page") == page:
        context.state.pop("pending_visual_page_transition", None)
        # Give an asynchronously rendered target page a short head start before
        # testing stability, otherwise the closed-menu blank frame may look stable.
        context.wait(0.35)
    else:
        transition = None
    snapshot = capture_stable_visual_window(
        context,
        window,
        timeout=8.0 if force_new else 2.0,
        minimum_wait=0.8 if force_new else 0.0,
        stable_samples=3 if force_new else 1,
        content_sensitive=force_new,
    )
    descriptor = structure_descriptor(snapshot)
    snapshot_foreground = detect_foreground_panel(snapshot)
    snapshot_has_foreground = isinstance(snapshot_foreground, dict)
    provisional = visual_window_signature(window, page, "")
    maps = context.state.setdefault("visual_page_maps", {})

    # Prefer the live in-memory map when two candidates have the same descriptor.
    # Its snapshot represents the state immediately before a menu was opened.
    available: list[dict[str, Any]] = list(
        item
        for item in reversed(list(maps.values()))
        if isinstance(item, dict)
        and isinstance(item.get("signature"), dict)
        and same_visual_page_family(item["signature"], provisional)
        and isinstance(item.get("structureDescriptor"), dict)
    )
    available.extend(persisted_page_candidates(provisional))
    excluded_fingerprints = {
        str(value)
        for value in ((transition or {}).get("excludedFingerprints") or [])
        if value
    }
    if excluded_fingerprints:
        available = [
            candidate
            for candidate in available
            if str((candidate.get("signature") or {}).get("stateFingerprint") or "")
            not in excluded_fingerprints
        ]
    best: dict[str, Any] | None = None
    best_distance = 1.0
    snapshot_has_foreground = detect_foreground_panel(snapshot) is not None
    if not force_new:
        for candidate in available:
            candidate_has_foreground = mapped_foreground_bounds(candidate) is not None
            if candidate_has_foreground != snapshot_has_foreground:
                # A modal and its dimmed parent are separate interaction states,
                # even when their coarse line descriptors remain very similar.
                continue
            distance = descriptor_distance(descriptor, candidate.get("structureDescriptor"))
            if distance < best_distance:
                best, best_distance = candidate, distance

    overlay_parent: dict[str, Any] | None = None
    overlay_change = None
    # The desktop may be mostly blank, so a permissive whole-image threshold can
    # hide a real page navigation. Tiny overlays are handled separately below.
    reuse_threshold = 0.012 if transition else 0.02
    if best is not None and best_distance <= reuse_threshold and prefer_changed_region:
        base_image = visual_page_image(best)
        if isinstance(base_image, Image.Image):
            candidate_change = changed_region(base_image, snapshot)
            cached_target = (best.get("targets") or {}).get(target_key) if target_key else None
            if (
                is_interactive_overlay_change(candidate_change)
                and not point_is_inside_bounds(
                    cached_target,
                    candidate_change.get("bounds") if candidate_change else None,
                )
            ):
                # A menu, calendar or popover appeared over an otherwise unchanged
                # page. The old same-name coordinate must not win over its contents.
                overlay_parent = best
                overlay_change = candidate_change
                best = None

    # Small text/data changes stay on the same structural page. Larger layout
    # changes and target-bearing overlays receive their own immutable state map.
    if best is not None and best_distance <= reuse_threshold:
        signature = dict(best["signature"])
        persisted = load_visual_model(signature)
        state_event = "reused"
    else:
        fingerprint = descriptor_fingerprint(descriptor)
        if force_new or fingerprint in excluded_fingerprints:
            fingerprint = f"{fingerprint}-{time.time_ns():x}"
        signature = visual_window_signature(window, page, fingerprint)
        persisted = {}
        state_event = "created"

    cache_key = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    cached = maps.get(cache_key)
    if isinstance(cached, dict) and state_event == "reused":
        cached["currentSnapshot"] = snapshot
        cached["stateDistance"] = best_distance
        announce_visual_state(context, cached)
        return cached

    persisted_details = persisted.get("targetDetails", {})
    persisted_elements = persisted.get("elements", [])
    persisted_ambiguous = persisted.get("ambiguousTargets", {})
    persisted_regions = persisted.get("regions", [])
    persisted_relationships = persisted.get("relationships", [])
    persisted_candidates = persisted.get("visualCandidates", [])
    persisted_unclassified = persisted.get("unclassified", [])
    persisted_performance = persisted.get("performance", {})
    persisted_raw_ocr = persisted.get("rawOcr", [])
    persisted_descriptor = persisted.get("structureDescriptor", {})
    persisted_classification_version = int(persisted.get("classificationVersion") or 0)
    change = None
    ocr_bounds = None
    parent_signature = None
    effective_parent = parent_page_map or overlay_parent
    if state_event == "created" and isinstance(effective_parent, dict):
        parent_signature = effective_parent.get("signature")
        parent_image = visual_page_image(effective_parent)
        if isinstance(parent_image, Image.Image):
            change = overlay_change or changed_region(parent_image, snapshot)
            if is_local_change(change):
                ocr_bounds = list(change["bounds"])

    page_map = {
        "signature": signature,
        "targets": persisted.get("targets", {}) if isinstance(persisted.get("targets"), dict) else {},
        "targetDetails": persisted_details if isinstance(persisted_details, dict) else {},
        "elements": persisted_elements if isinstance(persisted_elements, list) else [],
        "ambiguousTargets": persisted_ambiguous if isinstance(persisted_ambiguous, dict) else {},
        "ocrItems": None,
        "visualImage": None,
        "pendingImage": snapshot if state_event == "created" else None,
        "currentSnapshot": snapshot,
        "ocrBounds": ocr_bounds,
        "structureDescriptor": persisted_descriptor if isinstance(persisted_descriptor, dict) and persisted_descriptor else descriptor,
        "parentSignature": persisted.get("parentSignature") or parent_signature,
        "changeRegion": persisted.get("changeRegion") or change,
        "stateEvent": state_event,
        "stateDistance": best_distance if state_event == "reused" else None,
        "regions": persisted_regions if isinstance(persisted_regions, list) else [],
        "relationships": persisted_relationships if isinstance(persisted_relationships, list) else [],
        "visualCandidates": persisted_candidates if isinstance(persisted_candidates, list) else [],
        "unclassified": persisted_unclassified if isinstance(persisted_unclassified, list) else [],
        "performance": persisted_performance if isinstance(persisted_performance, dict) else {},
        "persistedRawOcr": persisted_raw_ocr if isinstance(persisted_raw_ocr, list) else [],
        "classificationVersion": persisted_classification_version,
    }
    maps[cache_key] = page_map
    announce_visual_state(context, page_map)
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
            if page_map.get("classificationVersion", 0) < VISUAL_CLASSIFICATION_VERSION:
                classify_visual_page(page_map)
                save_visual_targets(page_map)
            return restored

    screenshot_path = context.output_dir / (
        f".visual_map_{context.state['timestamp']}_{time.time_ns()}.png"
    )
    try:
        pending = page_map.get("pendingImage")
        visual_image = (
            pending
            if isinstance(pending, Image.Image)
            else capture_visual_window(window)
        )
        ocr_bounds = page_map.get("ocrBounds")
        if (
            isinstance(ocr_bounds, list)
            and len(ocr_bounds) == 4
            and all(isinstance(value, int) for value in ocr_bounds)
        ):
            ocr_image = visual_image.crop(tuple(ocr_bounds))
            offset_x, offset_y = ocr_bounds[0], ocr_bounds[1]
        else:
            ocr_image = visual_image
            offset_x = offset_y = 0
        ocr_image.save(screenshot_path)
        engine = context.state.get("ocr_engine")
        engine_init_seconds = 0.0
        if engine is None:
            engine_init_started = time.perf_counter()
            engine = marker.create_ocr_engine()
            context.state["ocr_engine"] = engine
            engine_init_seconds = time.perf_counter() - engine_init_started
        ocr_started = time.perf_counter()
        detected_items = marker.run_ocr(engine, screenshot_path)
        ocr_seconds = time.perf_counter() - ocr_started
        items = [
            marker.OcrItem(
                text=item.text,
                score=item.score,
                left=item.left + offset_x,
                top=item.top + offset_y,
                right=item.right + offset_x,
                bottom=item.bottom + offset_y,
                raw_text=item.raw_text,
            )
            for item in detected_items
        ]
    finally:
        try:
            screenshot_path.unlink()
        except FileNotFoundError:
            pass

    page_map["ocrItems"] = items
    page_map["visualImage"] = visual_image
    page_map["pendingImage"] = None
    regions_started = time.perf_counter()
    page_map["regions"] = build_ocr_regions(items)
    regions_seconds = time.perf_counter() - regions_started
    relationships_started = time.perf_counter()
    page_map["relationships"] = build_ocr_relationships(items)
    relationships_seconds = time.perf_counter() - relationships_started
    classify_visual_page(page_map)
    page_map.setdefault("performance", {}).update(
        {
            "engineInitSeconds": round(engine_init_seconds, 4),
            "ocrSeconds": round(ocr_seconds, 4),
            "regionBuildSeconds": round(regions_seconds, 4),
            "relationshipBuildSeconds": round(relationships_seconds, 4),
        }
    )
    save_visual_targets(page_map)
    performance = page_map.get("performance", {})
    context.emit(
        "info",
        (
            f"已建立新的视觉状态地图并完成预分类：{len(items)} 段文字，"
            f"{len(page_map.get('visualCandidates', []))} 个控件/表格候选；"
            f"识别范围={'局部变化区域' if page_map.get('ocrBounds') else '完整窗口'}；"
            f"耗时 OCR={performance.get('ocrSeconds', 0):.2f}s，"
            f"候选检测与分类={performance.get('classificationSeconds', 0):.2f}s，"
            f"关系={performance.get('relationshipBuildSeconds', 0):.2f}s"
        ),
        None,
    )
    return items


def local_point_to_screen(window: Any, point: list[int]) -> tuple[int, int]:
    rect = window.rectangle()
    return rect.left + int(point[0]), rect.top + int(point[1])


def visual_foreground_bounds(page_map: dict[str, Any]) -> list[int] | None:
    """Return the active dialog bounds, detecting it for older persisted maps."""

    for region in page_map.get("regions") or []:
        if isinstance(region, dict) and region.get("type") == "foreground-dialog":
            bounds = region.get("bounds")
            if isinstance(bounds, list) and len(bounds) == 4:
                return bounds
    image = visual_page_image(page_map)
    if not isinstance(image, Image.Image):
        return None
    foreground = detect_foreground_panel(image)
    if not isinstance(foreground, dict):
        return None
    bounds = foreground.get("bounds")
    return bounds if isinstance(bounds, list) and len(bounds) == 4 else None


def visual_table_bounds(page_map: dict[str, Any]) -> list[list[int]]:
    """Return table bounds recorded by either the region or element inventory."""

    tables: list[list[int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for collection in (
        page_map.get("regions") or [],
        page_map.get("elements") or [],
        page_map.get("visualCandidates") or [],
    ):
        for item in collection:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not (
                item.get("type") == "table"
                or item.get("kind") == "table"
                or item_id.startswith("table-region-")
            ):
                continue
            bounds = item.get("bounds")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
                continue
            try:
                normalized = tuple(int(value) for value in bounds)
            except (TypeError, ValueError):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            tables.append(list(normalized))
    return tables


def visual_bounds_overlap_table(
    bounds: Any,
    table_bounds: list[list[int]],
) -> bool:
    """Reject field candidates whose center or substantial area is inside a table."""

    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return False
    try:
        left, top, right, bottom = (float(value) for value in bounds)
    except (TypeError, ValueError):
        return False
    if right <= left or bottom <= top:
        return False

    center = [(left + right) / 2, (top + bottom) / 2]
    area = (right - left) * (bottom - top)
    for table in table_bounds:
        if point_is_inside_bounds(center, table):
            return True
        table_left, table_top, table_right, table_bottom = table
        overlap_width = max(0.0, min(right, table_right) - max(left, table_left))
        overlap_height = max(0.0, min(bottom, table_bottom) - max(top, table_top))
        if overlap_width * overlap_height / max(1.0, area) >= 0.25:
            return True
    return False


def visual_toggle_anchor(
    page_map: dict[str, Any],
    state_texts: tuple[str, str],
) -> tuple[list[int], list[int]] | None:
    """Find a previously mapped toggle label without starting another OCR run."""

    needles = [marker.normalize_ocr_text(text) for text in state_texts]
    candidates: dict[tuple[int, int], tuple[list[int], list[int]]] = {}
    for detail in (page_map.get("targetDetails") or {}).values():
        if not isinstance(detail, dict):
            continue
        if str(detail.get("role") or "") not in {
            "button",
            "text-action",
            "hover-trigger",
            "menu-item",
            "link",
        }:
            continue
        name = marker.normalize_ocr_text(detail.get("name"))
        point = detail.get("clickPoint")
        bounds = detail.get("bounds")
        if (
            any(needle and needle in name for needle in needles)
            and isinstance(point, list)
            and len(point) == 2
            and isinstance(bounds, list)
            and len(bounds) == 4
        ):
            candidates[(int(point[0]), int(point[1]))] = (
                [int(point[0]), int(point[1])],
                [int(value) for value in bounds],
            )

    if not candidates:
        raw_items = page_map.get("persistedRawOcr") or []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = marker.normalize_ocr_text(item.get("text") or item.get("rawText"))
            bounds = item.get("bounds")
            if (
                any(needle and needle in text for needle in needles)
                and isinstance(bounds, list)
                and len(bounds) == 4
            ):
                left, top, right, bottom = (int(value) for value in bounds)
                point = [(left + right) // 2, (top + bottom) // 2]
                candidates[(point[0], point[1])] = (point, [left, top, right, bottom])

    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None


def local_visual_ocr(
    context: ExecutionContext,
    image: Image.Image,
    bounds: list[int],
) -> list[Any]:
    """Run OCR only inside one small window-relative rectangle."""

    left, top, right, bottom = bounds
    temporary = context.output_dir / f".local_ocr_{time.time_ns()}.png"
    try:
        image.crop((left, top, right, bottom)).save(temporary)
        engine = context.state.get("ocr_engine")
        if engine is None:
            engine = marker.create_ocr_engine()
            context.state["ocr_engine"] = engine
        detected = marker.run_ocr(engine, temporary)
        return [
            marker.OcrItem(
                text=item.text,
                score=item.score,
                left=item.left + left,
                top=item.top + top,
                right=item.right + left,
                bottom=item.bottom + top,
                raw_text=item.raw_text,
            )
            for item in detected
        ]
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def toggle_state_match(items: list[Any], text: str) -> Any | None:
    """Return the best OCR item whose normalized text contains one state label."""

    target = marker.normalize_ocr_text(text)
    matches = [item for item in items if target and target in item.text]
    return max(matches, key=lambda item: (item.score, -item.top, -item.left)) if matches else None


def visual_text_point(
    context: ExecutionContext,
    window: Any,
    target_name: str,
    contains: bool = False,
    page: str = "current-page",
    role: str = "button",
    parent_page_map: dict[str, Any] | None = None,
    force_new: bool = False,
) -> tuple[int, int] | None:
    """Resolve visible text from a reusable per-page visual element map."""

    target_key = f"{role}:{'contains' if contains else 'exact'}:{target_name}"
    page_map = get_visual_page_map(
        context,
        window,
        page,
        parent_page_map=parent_page_map,
        force_new=force_new,
        prefer_changed_region=True,
        target_key=target_key,
    )
    context.state["last_visual_resolved_map"] = page_map
    foreground_bounds = visual_foreground_bounds(page_map)
    cached = page_map["targets"].get(target_key)
    if cached is not None and (
        foreground_bounds is None or point_is_inside_bounds(cached, foreground_bounds)
    ):
        return local_point_to_screen(window, cached)
    if cached is not None:
        # A legacy map may have cached the same-name button from the dimmed
        # background. Discard it and resolve again inside the active dialog.
        page_map["targets"].pop(target_key, None)
        page_map.get("targetDetails", {}).pop(target_key, None)

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

    if foreground_bounds is not None:
        foreground_matches = [
            item
            for item in matches
            if point_is_inside_bounds(
                [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                foreground_bounds,
            )
        ]
        if not foreground_matches:
            return None
        matches = foreground_matches

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
    force_new: bool = False,
) -> tuple[int, int] | None:
    """Infer a painted input area below its label and cache the relative point."""

    target_key = f"input:{field_name}"
    page_map = get_visual_page_map(
        context,
        window,
        page,
        force_new=force_new,
        prefer_changed_region=True,
        target_key=target_key,
    )
    foreground_bounds = visual_foreground_bounds(page_map)
    table_bounds = visual_table_bounds(page_map)
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        detail = page_map.get("targetDetails", {}).get(target_key, {})
        cached_in_table = any(
            point_is_inside_bounds(cached, table) for table in table_bounds
        )
        if foreground_bounds is not None and not point_is_inside_bounds(
            cached, foreground_bounds
        ):
            page_map["targets"].pop(target_key, None)
            page_map.get("targetDetails", {}).pop(target_key, None)
        elif cached_in_table or visual_bounds_overlap_table(
            detail.get("bounds") if isinstance(detail, dict) else cached,
            table_bounds,
        ):
            # A same-name table header may have been cached by the legacy
            # on-demand locator. Never treat a table cell/header as an input.
            context.emit(
                "info",
                f"已忽略字段“{field_name}”落在表格内的旧定位缓存，改用页面预分类输入框",
                None,
            )
            page_map["targets"].pop(target_key, None)
            page_map.get("targetDetails", {}).pop(target_key, None)
        elif not isinstance(detail, dict) or not detail.get("labelBounds"):
            return local_point_to_screen(window, cached)
        elif input_bounds_match_label(
            detail.get("bounds"),
            detail.get("labelBounds"),
            str(detail.get("relation") or ""),
        ):
            return local_point_to_screen(window, cached)
        else:
            page_map["targets"].pop(target_key, None)
            page_map.get("targetDetails", {}).pop(target_key, None)

    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(field_name)
    if not target:
        return None

    # Prefer the full-page classifier's field inventory. Unlike the legacy
    # per-label scan, these candidates have already been separated from tables.
    element_by_id = {
        str(element.get("id")): element
        for element in page_map.get("elements") or []
        if isinstance(element, dict) and element.get("id")
    }
    classified_candidates: list[dict[str, Any]] = []
    for element in page_map.get("elements") or []:
        if not isinstance(element, dict) or element.get("kind") != "field-rectangle":
            continue
        if marker.normalize_ocr_text(element.get("labelText")) != target:
            continue
        bounds = element.get("bounds")
        center = element.get("center")
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            continue
        label_element = element_by_id.get(str(element.get("labelId") or ""), {})
        if str(label_element.get("kind") or "") in {
            "table-header",
            "table-cell-text",
            "background-text",
        }:
            continue
        if visual_bounds_overlap_table(bounds, table_bounds):
            continue
        if foreground_bounds is not None and not point_is_inside_bounds(
            center, foreground_bounds
        ):
            continue
        classified_candidates.append(element)

    if classified_candidates:
        candidate = max(
            classified_candidates,
            key=lambda item: float(item.get("score") or 0),
        )
        local_point = [int(value) for value in candidate["center"]]
        label_element = element_by_id.get(str(candidate.get("labelId") or ""), {})
        label_bounds = label_element.get("bounds")
        page_map["targets"][target_key] = local_point
        page_map["targetDetails"][target_key] = {
            "role": "input",
            "name": field_name,
            "bounds": candidate.get("bounds"),
            "clickPoint": local_point,
            "labelBounds": label_bounds,
            "relation": candidate.get("relation"),
            "confidence": candidate.get("score"),
            "source": "ocr-page-classifier",
        }
        save_visual_targets(page_map)
        return local_point_to_screen(window, local_point)

    labels = [
        item
        for item in items
        if item.text == target
        and not any(
            point_is_inside_bounds(
                [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                table,
            )
            for table in table_bounds
        )
    ]
    if foreground_bounds is not None:
        labels = [
            item
            for item in labels
            if point_is_inside_bounds(
                [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                foreground_bounds,
            )
        ]
    if not labels:
        return None

    visual_image = page_map.get("visualImage")
    if not isinstance(visual_image, Image.Image):
        return None
    detected: list[tuple[Any, dict[str, Any]]] = []
    for label in labels:
        rectangle = find_input_rectangle(visual_image, label)
        if rectangle is not None and not visual_bounds_overlap_table(
            rectangle.get("bounds"), table_bounds
        ):
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
    force_new: bool = False,
) -> tuple[int, int] | None:
    """Locate a visually rendered select/combo box by its label."""

    target_key = f"select:{field_name}"
    page_map = get_visual_page_map(
        context,
        window,
        page,
        force_new=force_new,
        prefer_changed_region=True,
        target_key=target_key,
    )
    foreground_bounds = visual_foreground_bounds(page_map)
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        detail = page_map.get("targetDetails", {}).get(target_key, {})
        if foreground_bounds is not None and not point_is_inside_bounds(
            cached, foreground_bounds
        ):
            page_map["targets"].pop(target_key, None)
            page_map.get("targetDetails", {}).pop(target_key, None)
        elif not isinstance(detail, dict) or not detail.get("labelBounds"):
            return local_point_to_screen(window, cached)
        elif input_bounds_match_label(
            detail.get("bounds"),
            detail.get("labelBounds"),
            str(detail.get("relation") or ""),
        ):
            return local_point_to_screen(window, cached)
        else:
            page_map["targets"].pop(target_key, None)
            page_map.get("targetDetails", {}).pop(target_key, None)
    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(field_name)
    labels = [item for item in items if item.text == target]
    if foreground_bounds is not None:
        labels = [
            item
            for item in labels
            if point_is_inside_bounds(
                [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                foreground_bounds,
            )
        ]
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
    force_new: bool = False,
) -> tuple[int, int] | None:
    """Locate a painted checkbox around its label without assuming direction."""

    target_key = f"checkbox:{checkbox_name}"
    page_map = get_visual_page_map(
        context,
        window,
        page,
        force_new=force_new,
        prefer_changed_region=True,
        target_key=target_key,
    )
    foreground_bounds = visual_foreground_bounds(page_map)
    cached = page_map["targets"].get(target_key)
    if cached is not None:
        detail = page_map.get("targetDetails", {}).get(target_key, {})
        if (
            (foreground_bounds is None or point_is_inside_bounds(cached, foreground_bounds))
            and isinstance(detail, dict)
            and detail.get("locatorVersion") == VISUAL_CHECKBOX_LOCATOR_VERSION
            and checkbox_bounds_match_label(
                detail.get("bounds"),
                detail.get("labelBounds"),
                str(detail.get("relation") or ""),
            )
        ):
            return local_point_to_screen(window, cached)
        page_map["targets"].pop(target_key, None)
        page_map.get("targetDetails", {}).pop(target_key, None)

    items = ensure_visual_ocr(context, window, page_map)
    target = marker.normalize_ocr_text(checkbox_name)
    labels = [item for item in items if item.text == target]
    if foreground_bounds is not None:
        labels = [
            item
            for item in labels
            if point_is_inside_bounds(
                [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                foreground_bounds,
            )
        ]
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
        "locatorVersion": VISUAL_CHECKBOX_LOCATOR_VERSION,
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
    """Detect a common blue checked state around a visual checkbox point."""

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
    submit = bool(params.get("pressEnter", True))
    if not field_name:
        raise WorkflowExecutionError("目标字段名称不能为空")
    if not value:
        raise WorkflowExecutionError(f"字段“{field_name}”的填写内容不能为空")

    window = current_window(context)
    page = visual_page_id(params)
    point = visual_input_point(
        context,
        window,
        field_name,
        page=page,
    )
    if point is None:
        context.emit(
            "info",
            f"视觉地图未找到字段“{field_name}”，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_input_point(
            context,
            window,
            field_name,
            page=page,
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            f"完整 OCR 页面布局中仍未找到字段“{field_name}”，未执行输入"
        )
    mouse.click(button="left", coords=point)
    locator_mode = "visual-map"

    time.sleep(0.15)
    send_keys("^a", pause=0.06)
    send_keys("{BACKSPACE}", pause=0.06)
    send_keys(value, pause=0.08)
    time.sleep(0.2)
    if submit:
        send_keys("{ENTER}", pause=0.08)
    context.emit(
        "info",
        f"已填写字段“{field_name}”{('并按回车' if submit else '')}；定位方式={locator_mode}",
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
    if not field_name:
        raise WorkflowExecutionError("下拉框字段名称不能为空")
    if not option_text:
        raise WorkflowExecutionError(f"下拉框“{field_name}”的目标选项不能为空")

    window = current_window(context)
    page = visual_page_id(params)
    source_map = get_visual_page_map(
        context,
        window,
        page,
        prefer_changed_region=True,
        target_key=f"select:{field_name}",
    )
    point = visual_select_point(context, window, field_name, page=page)
    if point is None:
        context.emit(
            "info",
            f"视觉地图未找到下拉框“{field_name}”，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_select_point(
            context,
            window,
            field_name,
            page=page,
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            f"完整 OCR 页面布局中仍未找到下拉框“{field_name}”，未执行选择"
        )
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
        parent_page_map=source_map,
    )
    if option_point is None:
        context.emit(
            "info",
            f"视觉地图未找到下拉项“{option_text}”，刷新展开状态后再次视觉识别",
            None,
        )
        option_point = visual_text_point(
            context,
            window,
            option_text,
            contains=False,
            page=option_page,
            role="option",
            parent_page_map=source_map,
            force_new=True,
        )
    if option_point is None:
        raise WorkflowExecutionError(
            f"OCR 未识别到下拉项“{option_text}”，未执行选择"
        )
    mouse.click(button="left", coords=option_point)
    locator_mode = "visual-map"

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
    if not checkbox_name:
        raise WorkflowExecutionError("勾选框名称不能为空")
    if target_state not in {"勾选", "取消勾选", "切换"}:
        raise WorkflowExecutionError("勾选框目标状态无效")

    window = current_window(context)
    page = visual_page_id(params)
    point = visual_checkbox_point(
        context,
        window,
        checkbox_name,
        page=page,
    )
    if point is None:
        context.emit(
            "info",
            f"视觉地图未找到勾选框“{checkbox_name}”，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_checkbox_point(
            context,
            window,
            checkbox_name,
            page=page,
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            f"完整 OCR 页面布局中仍未找到勾选框“{checkbox_name}”，未执行操作"
        )
    locator_mode = "visual-map"
    current_state = visual_checkbox_state(point)
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
    match_mode = str(params.get("matchMode") or "精确匹配").strip()
    if not target_name:
        raise WorkflowExecutionError("点击对象名称不能为空")
    if match_mode not in {"精确匹配", "包含文字"}:
        raise WorkflowExecutionError("点击对象匹配方式无效")

    window = current_window(context)
    page = visual_page_id(params)
    context.state.pop("last_visual_resolved_map", None)
    point = visual_text_point(
        context,
        window,
        target_name,
        contains=match_mode == "包含文字",
        page=page,
    )
    if point is None:
        context.emit(
            "info",
            f"视觉地图未找到对象“{target_name}”，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_text_point(
            context,
            window,
            target_name,
            contains=match_mode == "包含文字",
            page=page,
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            f"完整 OCR 页面布局中仍未找到对象“{target_name}”，未执行点击"
        )
    mouse.click(button="left", coords=point)
    locator_mode = "visual-map"
    resolved_map = context.state.get("last_visual_resolved_map")
    if isinstance(resolved_map, dict) and resolved_map.get("parentSignature"):
        signatures = [
            resolved_map.get("signature"),
            resolved_map.get("parentSignature"),
        ]
        context.state["pending_visual_page_transition"] = {
            "page": page,
            "trigger": target_name,
            "excludedFingerprints": [
                signature.get("stateFingerprint")
                for signature in signatures
                if isinstance(signature, dict)
            ],
        }

    context.emit(
        "info",
        f"已点击对象“{target_name}”；定位方式={locator_mode}",
        None,
    )
    return {
        "targetName": target_name,
        "matchMode": match_mode,
        "locatorMode": locator_mode,
        "point": list(point),
    }


def run_his_input_number(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    number = str(params.get("value") or "").strip()
    if not number:
        raise WorkflowExecutionError("住院号不能为空")
    window_adapter.HOSPITALIZATION_NUMBER = number
    marker.base.HOSPITALIZATION_NUMBER = number
    context.variables["hospitalization_number"] = number
    window = current_window(context)
    page = visual_page_id(params)
    point = visual_input_point(
        context,
        window,
        "住院号",
        page=page,
    )
    if point is None:
        context.emit(
            "info",
            "视觉地图未找到住院号输入框，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_input_point(
            context,
            window,
            "住院号",
            page=page,
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            "完整 OCR 页面布局中仍未找到住院号输入框，未执行输入"
        )
    mouse.click(button="left", coords=point)
    locator_mode = "visual-map"
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


def _ocr_row_groups(items: list[Any], minimum_y: int) -> list[list[Any]]:
    """Group OCR fragments into visual rows below one table header."""

    candidates = sorted(
        (
            item
            for item in items
            if (item.top + item.bottom) // 2 > minimum_y
        ),
        key=lambda item: ((item.top + item.bottom) // 2, item.left),
    )
    groups: list[list[Any]] = []
    for item in candidates:
        center_y = (item.top + item.bottom) // 2
        if groups:
            group_y = round(
                sum((member.top + member.bottom) // 2 for member in groups[-1])
                / len(groups[-1])
            )
            tolerance = max(
                8,
                round(
                    sum(max(1, member.bottom - member.top) for member in groups[-1])
                    / len(groups[-1])
                    * 0.7
                ),
            )
            if abs(center_y - group_y) <= tolerance:
                groups[-1].append(item)
                continue
        groups.append([item])
    return groups


def _find_table_header(
    items: list[Any],
    header_text: str,
    table_title: str,
    foreground_bounds: list[int] | None,
) -> tuple[Any, list[Any]] | None:
    """Find a header label and the OCR fragments belonging to its header row."""

    normalized_header = marker.normalize_ocr_text(header_text)
    headers = [item for item in items if item.text == normalized_header]
    if foreground_bounds is not None:
        headers = [
            item
            for item in headers
            if point_is_inside_bounds(
                [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                foreground_bounds,
            )
        ]
    if not headers:
        return None

    normalized_title = marker.normalize_ocr_text(table_title)
    titles = [item for item in items if normalized_title and item.text == normalized_title]

    scored: list[tuple[float, Any, list[Any]]] = []
    for header in headers:
        center_y = (header.top + header.bottom) // 2
        tolerance = max(10, round(max(1, header.bottom - header.top) * 0.8))
        row_items = [
            item
            for item in items
            if abs((item.top + item.bottom) // 2 - center_y) <= tolerance
            and (
                foreground_bounds is None
                or point_is_inside_bounds(
                    [(item.left + item.right) // 2, (item.top + item.bottom) // 2],
                    foreground_bounds,
                )
            )
        ]
        span = max((item.right for item in row_items), default=header.right) - min(
            (item.left for item in row_items), default=header.left
        )
        score = len(row_items) * 1000.0 + span
        if titles:
            above = [
                title
                for title in titles
                if title.bottom <= header.top and header.top - title.bottom <= 500
            ]
            if above:
                score += 100000.0 - min(header.top - title.bottom for title in above)
        scored.append((score, header, row_items))
    _, header, row_items = max(scored, key=lambda value: value[0])
    return header, row_items


def run_visual_click_table_row(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Click a data row using its table header, never a repeated cell value."""

    row_number = int(params.get("row") or 1)
    header_text = str(params.get("headerText") or "序号").strip()
    table_title = str(params.get("tableTitle") or "").strip()
    click_column = str(params.get("clickColumn") or "").strip()
    if row_number < 1 or row_number > 100:
        raise WorkflowExecutionError("表格行号必须在 1～100 之间")
    if not header_text:
        raise WorkflowExecutionError("表头定位文字不能为空")

    window = current_window(context)
    page = visual_page_id(params)
    page_map = get_visual_page_map(
        context,
        window,
        page,
        prefer_changed_region=True,
        target_key=f"table-row:{header_text}:{row_number}",
    )
    items = ensure_visual_ocr(context, window, page_map)
    foreground_bounds = visual_foreground_bounds(page_map)
    located = _find_table_header(items, header_text, table_title, foreground_bounds)
    if located is None:
        context.emit(
            "info",
            f"页面地图未找到表头“{header_text}”，刷新当前页面后再次识别",
            None,
        )
        page_map = get_visual_page_map(
            context,
            window,
            page,
            force_new=True,
            prefer_changed_region=True,
            target_key=f"table-row:{header_text}:{row_number}",
        )
        items = ensure_visual_ocr(context, window, page_map)
        foreground_bounds = visual_foreground_bounds(page_map)
        located = _find_table_header(items, header_text, table_title, foreground_bounds)
    if located is None:
        raise WorkflowExecutionError(
            f"完整 OCR 页面布局中仍未找到表头“{header_text}”，未执行点击"
        )

    header, header_row = located
    detected_tables = visual_table_bounds(page_map)
    header_center = [(header.left + header.right) // 2, (header.top + header.bottom) // 2]
    table_bounds = next(
        (bounds for bounds in detected_tables if point_is_inside_bounds(header_center, bounds)),
        None,
    )

    snapshot = page_map.get("currentSnapshot")
    if not isinstance(snapshot, Image.Image):
        snapshot = capture_visual_window(window)
    header_left = min((item.left for item in header_row), default=header.left)
    header_right = max((item.right for item in header_row), default=header.right)
    header_top = min((item.top for item in header_row), default=header.top)
    header_bottom = max((item.bottom for item in header_row), default=header.bottom)
    if table_bounds is not None:
        crop_left = max(0, table_bounds[0])
        crop_right = min(snapshot.width, table_bounds[2])
        table_bottom = table_bounds[3]
    else:
        crop_left = max(0, header_left - 16)
        crop_right = min(snapshot.width, header_right + 16)
        table_bottom = snapshot.height
    crop_top = max(0, header_top - 12)
    desired_height = max(140, 65 * (row_number + 1))
    crop_bottom = min(snapshot.height, table_bottom, header_bottom + desired_height)
    if foreground_bounds is not None:
        crop_left = max(crop_left, foreground_bounds[0])
        crop_top = max(crop_top, foreground_bounds[1])
        crop_right = min(crop_right, foreground_bounds[2])
        crop_bottom = min(crop_bottom, foreground_bounds[3])
    ocr_bounds = [crop_left, crop_top, crop_right, crop_bottom]
    local_items = local_visual_ocr(context, snapshot, ocr_bounds)

    local_header = _find_table_header(
        local_items,
        header_text,
        "",
        foreground_bounds,
    )
    if local_header is not None:
        live_header, live_header_row = local_header
        header_bottom = max(
            (item.bottom for item in live_header_row),
            default=live_header.bottom,
        )
        header_row = live_header_row

    minimum_data_y = header_bottom + 3
    minimum_span = max(28, round((crop_right - crop_left) * 0.06))
    data_rows: list[list[Any]] = []
    for group in _ocr_row_groups(local_items, minimum_data_y):
        span = max((item.right for item in group), default=0) - min(
            (item.left for item in group), default=0
        )
        if len(group) >= 2 and span >= minimum_span:
            data_rows.append(group)
    if len(data_rows) < row_number:
        raise WorkflowExecutionError(
            f"表头“{header_text}”下方只识别到 {len(data_rows)} 条数据，无法点击第 {row_number} 行"
        )

    selected_row = data_rows[row_number - 1]
    row_y = round(
        sum((item.top + item.bottom) // 2 for item in selected_row)
        / len(selected_row)
    )
    column_target = marker.normalize_ocr_text(click_column or header_text)
    column_headers = [item for item in header_row if item.text == column_target]
    column_header = min(
        column_headers,
        key=lambda item: abs((item.top + item.bottom) // 2 - (header.top + header.bottom) // 2),
    ) if column_headers else header
    click_x = (column_header.left + column_header.right) // 2
    local_point = [click_x, row_y]
    screen_point = local_point_to_screen(window, local_point)
    mouse.click(button="left", coords=screen_point)
    context.emit(
        "info",
        (
            f"已点击表头“{header_text}”对应表格的第 {row_number} 行；"
            "定位方式=页面地图+局部OCR"
        ),
        None,
    )
    return {
        "row": row_number,
        "headerText": header_text,
        "tableTitle": table_title,
        "clickColumn": click_column or header_text,
        "recognizedCells": [item.raw_text for item in selected_row],
        "locatorMode": "visual-map+local-ocr",
        "ocrBounds": ocr_bounds,
        "point": local_point,
    }


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

    # 表头是标题下方第一条包含多个文字框的水平行。这里不读取 DOM 或控件树
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
        window_adapter.capture_his_window(window, probe_path)
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
    window_adapter.capture_his_window(current_window(context), path)
    context.state["current_screenshot"] = path
    context.state["screenshots"].append(path)
    context.emit("info", f"当前位置截图：{path}", {"path": str(path)})
    return {"path": str(path)}


def run_capture_rightmost(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    current = context.state.get("current_screenshot")
    if not isinstance(current, Path) or not current.is_file():
        raise WorkflowExecutionError("请先执行“截取当前位置”模块")
    window = current_window(context)
    scrollbar = window_adapter.detect_horizontal_scrollbar(window, current)
    if scrollbar is None:
        context.emit("info", "未检测到横向滚动条，不生成最右端截图", None)
        return {"captured": False}

    original_position, rightmost_position = scrollbar
    window_adapter.drag_scrollbar(original_position, rightmost_position)
    prefix = safe_file_component(
        params.get("filePrefix")
        or f"his_inpatient_{context.variables.get('hospitalization_number', 'unknown')}"
    )
    path = context.output_dir / f"{prefix}_rightmost_{context.state['timestamp']}.png"
    try:
        time.sleep(float(params.get("layoutWaitSeconds", 0.5)))
        window_adapter.capture_his_window(window, path)
        context.state["screenshots"].append(path)
        context.emit("info", f"最右端截图：{path}", {"path": str(path)})
    finally:
        if bool(params.get("restorePosition", True)) and path.is_file():
            right_state = window_adapter.detect_horizontal_scrollbar(window, path)
            if right_state is not None:
                window_adapter.drag_scrollbar(right_state[0], original_position)
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
    window_adapter.capture_his_window(window, left_path)
    context.state["current_screenshot"] = left_path
    context.state["screenshots"].append(left_path)
    context.emit("info", f"当前位置截图：{left_path}", {"path": str(left_path)})

    context.check_cancelled()
    context.emit("info", "组合操作 [3/6]：检测横向滚动条并拖到最右端", None)
    scrollbar = window_adapter.detect_horizontal_scrollbar(window, left_path)
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
        window_adapter.drag_scrollbar(current_position, rightmost_position)
        moved_right = True
        context.wait(scroll_wait_seconds)

        context.check_cancelled()
        context.emit("info", "组合操作 [4/6]：拓宽最右侧视图中显示不全的表格列", None)
        right_expand_result = run_his_expand_table_column(context, expand_params)

        context.check_cancelled()
        context.emit("info", "组合操作 [5/6]：截取最右侧当前位置", None)
        # 拓宽列会增加表格总宽度，因此先截图检测新的滑块范围；若产生了新的
        # 右侧空间，则继续拖到新的最右端，再覆盖保存最终截图。
        window_adapter.capture_his_window(window, right_path)
        right_state = window_adapter.detect_horizontal_scrollbar(window, right_path)
        if right_state is not None and right_state[0] != right_state[1]:
            window_adapter.drag_scrollbar(right_state[0], right_state[1])
            context.wait(scroll_wait_seconds)
            window_adapter.capture_his_window(window, right_path)
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
                    window_adapter.capture_his_window(window, right_path)
                    restore_source = right_path
                except Exception:
                    restore_source = None
            restore_state = (
                window_adapter.detect_horizontal_scrollbar(window, restore_source)
                if restore_source is not None
                else None
            )
            if restore_state is not None:
                rect = window.rectangle()
                leftmost_position = (rect.left + 1, restore_state[0][1])
                window_adapter.drag_scrollbar(restore_state[0], leftmost_position)
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
    """Force a complete OCR and preclassification of the current full window."""

    window = current_window(context)
    page = visual_page_id(params)
    page_load_wait = float(params.get("pageLoadWaitSeconds", 1.5))
    if not 0 <= page_load_wait <= 30:
        raise WorkflowExecutionError("页面加载等待时间必须在 0～30 秒之间")
    if page_load_wait:
        context.emit("info", f"等待页面加载稳定（至少 {page_load_wait:g} 秒）", None)
        context.wait(page_load_wait)
    page_map = get_visual_page_map(
        context,
        window,
        page,
        force_new=True,
    )
    items = ensure_visual_ocr(context, window, page_map)
    path = visual_map_path(page_map["signature"])
    return {
        "page": page,
        "stateFingerprint": page_map["signature"].get("stateFingerprint"),
        "stateEvent": page_map.get("stateEvent"),
        "stateDistance": page_map.get("stateDistance"),
        "recognitionScope": "local-change" if page_map.get("ocrBounds") else "full-window",
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


def run_visual_refresh_page(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility alias for forced full-window page mapping."""

    context.state.pop("pending_visual_page_transition", None)
    result = run_visual_map_page(context, params)
    result["manualRefresh"] = True
    result["forced"] = True
    context.emit(
        "info",
        f"已强制完成整页 OCR、预分类并保存新 Map；页面={result['page']}",
        None,
    )
    return result


def infer_date_order(
    page_map: dict[str, Any],
    local_bounds: list[int],
) -> str:
    """Infer date segment order from placeholder text inside the field."""

    left, top, right, bottom = local_bounds
    fragments: list[str] = []
    for item in page_map.get("ocrItems") or []:
        center_x = (item.left + item.right) // 2
        center_y = (item.top + item.bottom) // 2
        if left <= center_x <= right and top <= center_y <= bottom:
            fragments.append(str(item.raw_text or item.text or ""))
    placeholder = "".join(fragments).lower()
    tokens = {
        "年": min(
            [position for token in ("年", "yyyy", "year") if (position := placeholder.find(token)) >= 0]
            or [10_000]
        ),
        "月": min(
            [position for token in ("月", "mm", "month") if (position := placeholder.find(token)) >= 0]
            or [10_000]
        ),
        "日": min(
            [position for token in ("日", "dd", "day") if (position := placeholder.find(token)) >= 0]
            or [10_000]
        ),
    }
    if all(position < 10_000 for position in tokens.values()):
        return "".join(sorted(tokens, key=tokens.get))
    return "年月日"


def date_segment_local_points(
    page_map: dict[str, Any],
    local_bounds: list[int],
) -> list[list[int]]:
    """Locate the three editable date segments, excluding the calendar button."""

    left, top, right, bottom = local_bounds
    field_width = right - left
    calendar_width = min(34, max(20, round(field_width * 0.18)))
    content_right = right - calendar_width
    candidates = []
    for item in page_map.get("ocrItems") or []:
        center_x = (item.left + item.right) // 2
        center_y = (item.top + item.bottom) // 2
        raw_text = str(item.raw_text or item.text or "")
        if (
            left <= center_x <= content_right
            and top <= center_y <= bottom
            and (any(token in raw_text.lower() for token in ("年", "月", "日", "y", "m", "d"))
                 or any(character.isdigit() for character in raw_text))
        ):
            candidates.append(item)

    if candidates:
        text_item = max(candidates, key=lambda item: item.right - item.left)
        segment_left = max(left + 2, text_item.left)
        segment_right = min(content_right, text_item.right)
        if segment_right - segment_left >= 30:
            return [
                [
                    segment_left + round((segment_right - segment_left) * (index * 2 + 1) / 6),
                    (top + bottom) // 2,
                ]
                for index in range(3)
            ]

    # Empty or low-confidence placeholders normally occupy the leading portion
    # of a date field rather than the full area before the calendar icon.
    segment_right = left + round(max(30, content_right - left) * 0.68)
    return [
        [
            left + round((segment_right - left) * (index * 2 + 1) / 6),
            (top + bottom) // 2,
        ]
        for index in range(3)
    ]


def run_his_input_date(context: ExecutionContext, params: dict[str, Any]) -> dict[str, Any]:
    """Fill a segmented date control located entirely from the visual map."""

    field_name = str(params.get("fieldName") or "").strip()
    date_value = str(params.get("dateValue") or "").strip()
    requested_order = str(params.get("dateOrder") or "自动识别").strip()
    confirm_key = str(params.get("confirmKey") or "Tab").strip()
    segment_pause = float(params.get("segmentPauseSeconds", 0.12))
    if not field_name:
        raise WorkflowExecutionError("日期字段名称不能为空")
    try:
        parsed = datetime.date.fromisoformat(date_value)
    except ValueError as exc:
        raise WorkflowExecutionError(
            f"日期“{date_value}”格式无效，请使用 YYYY-MM-DD"
        ) from exc
    if requested_order not in {"自动识别", "年月日", "月日年", "日月年"}:
        raise WorkflowExecutionError("日期显示顺序无效")
    if confirm_key not in {"Tab", "Enter", "不发送"}:
        raise WorkflowExecutionError("日期确认方式无效")
    if not 0 <= segment_pause <= 2:
        raise WorkflowExecutionError("日期分段输入间隔必须在 0～2 秒之间")

    window = current_window(context)
    page = visual_page_id(params)
    point = visual_input_point(context, window, field_name, page=page)
    if point is None:
        context.emit(
            "info",
            f"视觉地图未找到日期字段“{field_name}”，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_input_point(
            context,
            window,
            field_name,
            page=page,
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            f"视觉页面布局中未找到日期字段“{field_name}”"
        )

    page_map = get_visual_page_map(context, window, page)
    detail = page_map.get("targetDetails", {}).get(f"input:{field_name}", {})
    bounds = detail.get("bounds") if isinstance(detail, dict) else None
    if not (
        isinstance(bounds, list)
        and len(bounds) == 4
        and all(isinstance(value, int) for value in bounds)
    ):
        raise WorkflowExecutionError(
            f"日期字段“{field_name}”已有中心坐标，但布局中缺少输入框边界；请强制重新识别页面布局"
        )

    order = infer_date_order(page_map, bounds) if requested_order == "自动识别" else requested_order
    parts = {
        "年": f"{parsed.year:04d}",
        "月": str(parsed.month),
        "日": str(parsed.day),
    }
    rect = window.rectangle()
    local_left, local_top, local_right, local_bottom = bounds
    local_segment_points = date_segment_local_points(page_map, bounds)
    segment_points = [
        (rect.left + point[0], rect.top + point[1])
        for point in local_segment_points
    ]
    # Segmented native date controls keep one focus across year/month/day.
    # Click the first segment once, then move between segments with Right so
    # approximate per-segment coordinates cannot focus the wrong date part.
    mouse.click(button="left", coords=segment_points[0])
    time.sleep(0.08)
    for index, segment in enumerate(order):
        send_keys(parts[segment], pause=0.06)
        if index < len(order) - 1:
            send_keys("{RIGHT}", pause=0.06)
            if segment_pause:
                context.wait(segment_pause)
    if confirm_key == "Tab":
        send_keys("{TAB}", pause=0.08)
    elif confirm_key == "Enter":
        send_keys("{ENTER}", pause=0.08)

    context.emit(
        "info",
        (
            f"已填写日期字段“{field_name}”={date_value}；分段顺序={order}；"
            "切换方式=右方向键；定位方式=visual-map"
        ),
        None,
    )
    return {
        "fieldName": field_name,
        "dateValue": date_value,
        "dateOrder": order,
        "confirmKey": confirm_key,
        "segmentNavigation": "Right",
        "locatorMode": "visual-map",
        "point": list(segment_points[0]),
        "segmentPoints": [list(point) for point in segment_points],
        "bounds": [
            rect.left + local_left,
            rect.top + local_top,
            rect.left + local_right,
            rect.top + local_bottom,
        ],
    }


def run_visual_ensure_expanded(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Use small-area OCR to make a mapped expand/collapse toggle idempotent."""

    collapsed_text = str(params.get("collapsedText") or "").strip()
    expanded_text = str(params.get("expandedText") or "").strip()
    horizontal_padding = int(params.get("horizontalPadding", 100))
    vertical_padding = int(params.get("verticalPadding", 40))
    wait_after_click = float(params.get("waitAfterClickSeconds", 0.3))
    if not collapsed_text or not expanded_text:
        raise WorkflowExecutionError("折叠状态文字和展开状态文字不能为空")
    if marker.normalize_ocr_text(collapsed_text) == marker.normalize_ocr_text(expanded_text):
        raise WorkflowExecutionError("折叠状态文字和展开状态文字不能相同")
    if not 10 <= horizontal_padding <= 500 or not 10 <= vertical_padding <= 300:
        raise WorkflowExecutionError("局部 OCR 范围参数无效")
    if not 0 <= wait_after_click <= 10:
        raise WorkflowExecutionError("点击后等待时间必须在 0～10 秒之间")

    window = current_window(context)
    page = visual_page_id(params)
    provisional = visual_window_signature(window, page, "")
    in_memory = [
        item
        for item in reversed(
            list((context.state.get("visual_page_maps") or {}).values())
        )
        if isinstance(item, dict)
        and isinstance(item.get("signature"), dict)
        and same_visual_page_family(item["signature"], provisional)
    ]
    candidates = in_memory + persisted_page_candidates(provisional)
    anchors: list[tuple[list[int], list[int]]] = []
    seen_points: set[tuple[int, int]] = set()
    for candidate in candidates:
        anchor = visual_toggle_anchor(
            candidate,
            (collapsed_text, expanded_text),
        )
        if anchor is None:
            continue
        point_key = (anchor[0][0], anchor[0][1])
        if point_key not in seen_points:
            seen_points.add(point_key)
            anchors.append(anchor)
    if not anchors:
        raise WorkflowExecutionError(
            "已有页面 Map 中未找到展开/收起状态文字，无法确定局部 OCR 区域；请先建立当前页面布局"
        )

    snapshot = capture_visual_window(window)
    for _anchor_point, anchor_bounds in anchors[:8]:
        left = max(0, anchor_bounds[0] - horizontal_padding)
        top = max(0, anchor_bounds[1] - vertical_padding)
        right = min(snapshot.width, anchor_bounds[2] + horizontal_padding)
        bottom = min(snapshot.height, anchor_bounds[3] + vertical_padding)
        bounds = [left, top, right, bottom]
        items = local_visual_ocr(context, snapshot, bounds)
        expanded_match = toggle_state_match(items, expanded_text)
        if expanded_match is not None:
            point = [
                (expanded_match.left + expanded_match.right) // 2,
                (expanded_match.top + expanded_match.bottom) // 2,
            ]
            context.emit(
                "info",
                f"局部 OCR 检测到“{expanded_text}”，区域已经展开，本次不点击",
                None,
            )
            return {
                "state": "expanded",
                "clicked": False,
                "matchedText": expanded_match.text,
                "point": point,
                "ocrBounds": bounds,
                "recognitionScope": "local",
            }

        collapsed_match = toggle_state_match(items, collapsed_text)
        if collapsed_match is not None:
            local_point = [
                (collapsed_match.left + collapsed_match.right) // 2,
                (collapsed_match.top + collapsed_match.bottom) // 2,
            ]
            mouse.click(
                button="left",
                coords=local_point_to_screen(window, local_point),
            )
            if wait_after_click:
                context.wait(wait_after_click)
            context.emit(
                "info",
                f"局部 OCR 检测到“{collapsed_text}”，已点击并展开",
                None,
            )
            return {
                "state": "collapsed",
                "clicked": True,
                "matchedText": collapsed_match.text,
                "point": local_point,
                "ocrBounds": bounds,
                "recognitionScope": "local",
            }

    raise WorkflowExecutionError(
        f"局部 OCR 未识别到“{collapsed_text}”或“{expanded_text}”，未执行点击"
    )


def run_visual_hover_object(
    context: ExecutionContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Hover a named object and capture the resulting menu/popover page state."""

    target_name = str(params.get("targetName") or "").strip()
    if not target_name:
        raise WorkflowExecutionError("悬浮对象名称不能为空")
    wait_seconds = float(params.get("hoverWaitSeconds", 0.5))
    if not 0 <= wait_seconds <= 10:
        raise WorkflowExecutionError("悬浮后等待时间必须在 0～10 秒之间")
    source_page = visual_page_id(params)
    hover_page = str(
        params.get("hoverPage") or f"{source_page}:hover:{target_name}"
    ).strip()[:80]
    window = current_window(context)
    source_map = get_visual_page_map(context, window, source_page)
    point = visual_text_point(
        context,
        window,
        target_name,
        page=source_page,
        role="hover-trigger",
    )
    if point is None:
        context.emit(
            "info",
            f"视觉地图未找到悬浮对象“{target_name}”，刷新当前页面后再次视觉识别",
            None,
        )
        point = visual_text_point(
            context,
            window,
            target_name,
            page=source_page,
            role="hover-trigger",
            force_new=True,
        )
    if point is None:
        raise WorkflowExecutionError(
            f"完整 OCR 页面布局中仍未找到悬浮对象“{target_name}”，未执行悬浮"
        )
    locator_mode = "visual-map"
    mouse.move(coords=point)
    context.wait(wait_seconds)
    hover_map = get_visual_page_map(
        context,
        window,
        hover_page,
        parent_page_map=source_map,
    )
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
            "每次执行都等待页面稳定，强制进行完整窗口 OCR 和整页预分类，并保存为新 Map。",
            (
                visual_page_field(),
                field("pageLoadWaitSeconds", "页面加载等待（秒）", "number", 1.5, min=0, max=30, step=0.5),
            ),
            run_visual_map_page,
        ),
        ModuleDefinition(
            "visual.refresh_page",
            "重新识别整页",
            "视觉",
            "兼容模块：等待页面稳定后强制完成完整窗口 OCR 和整页预分类，并保存为新 Map。",
            (
                visual_page_field(),
                field("pageLoadWaitSeconds", "页面加载等待（秒）", "number", 1.5, min=0, max=30, step=0.5),
            ),
            run_visual_refresh_page,
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
            ),
            run_visual_hover_object,
        ),
        ModuleDefinition(
            "visual.ensure_expanded",
            "确保区域已展开（局部 OCR）",
            "视觉",
            "只识别已有位置附近的小区域；检测到展开状态时跳过，检测到折叠状态时才点击。",
            (
                field("collapsedText", "折叠状态文字", "text", "更多"),
                field("expandedText", "展开状态文字", "text", "收起"),
                visual_page_field(),
                field("horizontalPadding", "左右识别范围（像素）", "number", 100, min=10, max=500, step=10),
                field("verticalPadding", "上下识别范围（像素）", "number", 40, min=10, max=300, step=10),
                field("waitAfterClickSeconds", "点击后等待（秒）", "number", 0.3, min=0, max=10, step=0.1),
            ),
            run_visual_ensure_expanded,
        ),
        ModuleDefinition(
            "visual.input_field",
            "填写文本字段",
            "视觉",
            "通过 OCR 页面布局定位并填写普通文本字段。",
            (
                field("fieldName", "目标字段名称", "text", "卡号"),
                field("value", "填写内容/变量", "text", ""),
                field("pressEnter", "填写后按回车", "boolean", True),
                visual_page_field(),
            ),
            run_his_input_field,
        ),
        ModuleDefinition(
            "visual.input_date",
            "填写日期",
            "视觉",
            "通过 OCR 页面布局定位日期框；点击首个日期段后，用右方向键依次切换并填写各段。",
            (
                field("fieldName", "日期字段名称", "text", "开始日期"),
                field("dateValue", "日期/变量（YYYY-MM-DD）", "text", ""),
                field(
                    "dateOrder",
                    "控件日期顺序",
                    "select",
                    "自动识别",
                    options=["自动识别", "年月日", "月日年", "日月年"],
                ),
                field(
                    "confirmKey",
                    "填写后确认方式",
                    "select",
                    "Tab",
                    options=["Tab", "Enter", "不发送"],
                ),
                field(
                    "segmentPauseSeconds",
                    "日期段切换间隔（秒）",
                    "number",
                    0.12,
                    min=0,
                    max=2,
                    step=0.01,
                ),
                visual_page_field(),
            ),
            run_his_input_date,
        ),
        ModuleDefinition(
            "visual.select_option",
            "选择下拉项",
            "视觉",
            "通过 OCR 页面布局定位下拉框和展开后的目标选项。",
            (
                field("fieldName", "下拉框字段名称", "text", "性别"),
                field("optionText", "选择内容/变量", "text", "全部"),
                visual_page_field(),
            ),
            run_his_select_option,
        ),
        ModuleDefinition(
            "visual.set_checkbox",
            "设置勾选框",
            "视觉",
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
            ),
            run_his_set_checkbox,
        ),
        ModuleDefinition(
            "visual.click_object",
            "点击文字对象",
            "视觉",
            "通过 OCR 页面布局点击查询、清屏、导出、打印等可见文字对象。",
            (
                field("targetName", "点击对象名称", "text", "查询"),
                field(
                    "matchMode",
                    "名称匹配方式",
                    "select",
                    "精确匹配",
                    options=["精确匹配", "包含文字"],
                ),
                visual_page_field(),
            ),
            run_his_click_object,
        ),
        ModuleDefinition(
            "visual.click_table_row",
            "点击表格行",
            "视觉",
            "通过表头识别表格，并用快速局部 OCR 点击指定数据行；不依赖行内重复文字。",
            (
                field("tableTitle", "表格标题（可选）", "text", ""),
                field("headerText", "表头定位文字", "text", "序号"),
                field("row", "数据行号", "number", 1, min=1, max=100, step=1),
                field("clickColumn", "点击列名（留空使用定位表头）", "text", ""),
                visual_page_field(),
            ),
            run_visual_click_table_row,
        ),
        ModuleDefinition(
            "visual.expand_table_column",
            "自动拓宽显示不全的列",
            "视觉",
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
            "visual.expand_capture_full_table",
            "拓宽并截取完整表格",
            "视觉",
            "依次拓宽左侧可见列、截图、滚到最右侧、拓宽右侧可见列、截图并恢复到最左侧。",
            (
                field("tableTitle", "表格标题/定位文字", "text", "医嘱明细"),
                field("filePrefix", "截图文件名前缀", "text", "table_${hospitalization_number}"),
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
            "截取当前目标窗口并记录为 OCR 输入。",
            (field("filePrefix", "文件名前缀", "text", "capture_${hospitalization_number}"),),
            run_capture_current,
        ),
        ModuleDefinition(
            "capture.rightmost",
            "截取横向最右端",
            "截图",
            "检测横向滚动条，拖到最右端截图并恢复。",
            (
                field("filePrefix", "文件名前缀", "text", "capture_${hospitalization_number}"),
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
    ]
    for definition in definitions:
        registry.register(definition)

    # Existing saved workflows from earlier releases keep working, but these
    # application-specific type names are no longer shown in the module panel.
    legacy_aliases = {
        "his.input_field": "visual.input_field",
        "his.input_date": "visual.input_date",
        "his.select_option": "visual.select_option",
        "his.set_checkbox": "visual.set_checkbox",
        "his.click_object": "visual.click_object",
        "his.click_first_result": "visual.click_table_row",
        "his.expand_table_column": "visual.expand_table_column",
        "his.expand_capture_full_table": "visual.expand_capture_full_table",
    }
    for legacy_type, current_type in legacy_aliases.items():
        current = registry.get(current_type)
        registry.register(
            ModuleDefinition(
                legacy_type,
                current.name,
                current.category,
                current.description,
                current.fields,
                current.handler,
                hidden=True,
            )
        )

    # Compatibility only for old demo configurations. New workflows use the
    # generic OCR modules above and never receive these entries from the API.
    for definition in (
        ModuleDefinition(
            "his.input_hospitalization",
            "填写住院号并回车",
            "视觉",
            "旧流程兼容模块。",
            (
                field("value", "住院号/变量", "text", "${hospitalization_number}"),
                visual_page_field(),
            ),
            run_his_input_number,
            hidden=True,
        ),
        ModuleDefinition(
            "his.wait_query",
            "等待患者查询结果",
            "高级（兼容）",
            "旧流程兼容模块。",
            (field("timeoutSeconds", "超时（秒）", "number", 30, min=1, max=180, step=1),),
            run_wait,
            hidden=True,
        ),
        ModuleDefinition(
            "his.wait_navigation",
            "等待页面跳转",
            "高级（兼容）",
            "旧流程兼容模块。",
            (field("timeoutSeconds", "超时（秒）", "number", 30, min=1, max=180, step=1),),
            run_wait,
            hidden=True,
        ),
    ):
        registry.register(definition)
    return registry
