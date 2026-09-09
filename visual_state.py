"""Fast visual-state comparison helpers for reusable OCR page maps."""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageOps


DESCRIPTOR_SIZE = (64, 36)
DESCRIPTOR_VERSION = 2


def pixel_values(image: Image.Image) -> Any:
    modern_getter = getattr(image, "get_flattened_data", None)
    return modern_getter() if callable(modern_getter) else image.getdata()


def structure_descriptor(image: Image.Image) -> dict[str, Any]:
    """Build a control-border skeleton that ignores text and mutable field data."""

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    horizontal_edges = np.zeros(rgb.shape[:2], dtype=bool)
    vertical_edges = np.zeros(rgb.shape[:2], dtype=bool)
    horizontal_edges[1:, :] = np.abs(rgb[1:, :] - rgb[:-1, :]).sum(axis=2) >= 18
    vertical_edges[:, 1:] = np.abs(rgb[:, 1:] - rgb[:, :-1]).sum(axis=2) >= 18

    def retain_long_runs(mask: np.ndarray, axis: int, minimum: int) -> np.ndarray:
        output = np.zeros_like(mask, dtype=np.uint8)
        rows = mask if axis == 1 else mask.T
        result_rows = output if axis == 1 else output.T
        for index, row in enumerate(rows):
            transitions = np.diff(np.pad(row.astype(np.int8), (1, 1)))
            starts = np.flatnonzero(transitions == 1)
            ends = np.flatnonzero(transitions == -1)
            for start, end in zip(starts, ends):
                if end - start >= minimum:
                    result_rows[index, start:end] = 255
        return output

    minimum_horizontal = max(10, round(image.width * 0.006))
    minimum_vertical = max(10, round(image.height * 0.012))
    horizontal_lines = retain_long_runs(
        horizontal_edges,
        axis=1,
        minimum=minimum_horizontal,
    )
    vertical_lines = retain_long_runs(
        vertical_edges,
        axis=0,
        minimum=minimum_vertical,
    )

    # Result rows are runtime data. When several long grid separators share one
    # span, retain only the table header skeleton and mask the variable body.
    grid_segments: list[tuple[int, int, int]] = []
    minimum_grid_span = max(60, round(image.height * 0.065))
    for x in range(vertical_edges.shape[1]):
        edge_rows = np.flatnonzero(vertical_edges[:, x])
        if edge_rows.size == 0:
            continue
        groups = np.split(edge_rows, np.flatnonzero(np.diff(edge_rows) > 3) + 1)
        longest = max(groups, key=len)
        span = int(longest[-1] - longest[0] + 1 + 1)
        if span >= minimum_grid_span and len(longest) / span >= 0.70:
            grid_segments.append((x, int(longest[0]), int(longest[-1])))

    grid_clusters: list[list[tuple[int, int, int]]] = []
    for segment in grid_segments:
        matched = next(
            (
                cluster
                for cluster in grid_clusters
                if abs(segment[1] - cluster[0][1]) <= 6
                and abs(segment[2] - cluster[0][2]) <= 6
            ),
            None,
        )
        if matched is None:
            grid_clusters.append([segment])
        else:
            matched.append(segment)
    for cluster in grid_clusters:
        x_values = sorted({segment[0] for segment in cluster})
        if len(x_values) < 6 or x_values[-1] - x_values[0] < image.width * 0.15:
            continue
        top = round(sum(segment[1] for segment in cluster) / len(cluster))
        bottom = round(sum(segment[2] for segment in cluster) / len(cluster))
        header_bottom = min(bottom, top + max(36, round(image.height * 0.045)))
        left, right = x_values[0], x_values[-1]
        horizontal_lines[header_bottom : bottom + 1, left : right + 1] = 0
        vertical_lines[header_bottom : bottom + 1, left : right + 1] = 0

    skeleton = np.maximum(horizontal_lines, vertical_lines)
    reduced = Image.fromarray(skeleton).resize(
        DESCRIPTOR_SIZE,
        Image.Resampling.LANCZOS,
    )
    return {
        "version": DESCRIPTOR_VERSION,
        "size": list(DESCRIPTOR_SIZE),
        "lines": [value // 32 for value in pixel_values(reduced)],
    }


def descriptor_distance(first: Any, second: Any) -> float:
    """Return 0 for the same structure and values near 1 for very different pages."""

    if not isinstance(first, dict) or not isinstance(second, dict):
        return 1.0
    if first.get("version") != DESCRIPTOR_VERSION or second.get("version") != DESCRIPTOR_VERSION:
        return 1.0
    first_lines, second_lines = first.get("lines"), second.get("lines")
    if not (
        isinstance(first_lines, list)
        and isinstance(second_lines, list)
        and len(first_lines) == len(second_lines) > 0
    ):
        return 1.0
    distance = sum(
        abs(int(left) - int(right))
        for left, right in zip(first_lines, second_lines)
    ) / (len(first_lines) * 7)
    return round(distance, 6)


def changed_region(
    before: Image.Image,
    after: Image.Image,
    threshold: int = 22,
    padding: int = 18,
) -> dict[str, Any] | None:
    """Describe the changed screen region, suitable for popup-local OCR."""

    if before.size != after.size:
        return None
    difference = ImageOps.grayscale(
        ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
    )
    mask = difference.point(lambda value: 255 if value >= threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    changed_pixels = sum(1 for value in pixel_values(mask) if value)
    image_area = max(1, after.width * after.height)
    left, top, right, bottom = bbox
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(after.width, right + padding)
    bottom = min(after.height, bottom + padding)
    bounds_area = max(1, (right - left) * (bottom - top))
    return {
        "bounds": [left, top, right, bottom],
        "changedPixelRatio": round(changed_pixels / image_area, 6),
        "boundsAreaRatio": round(bounds_area / image_area, 6),
        "widthRatio": round((right - left) / max(1, after.width), 6),
        "heightRatio": round((bottom - top) / max(1, after.height), 6),
    }


def is_local_change(change: Any) -> bool:
    """Return True when a change looks like one popup rather than a full page."""

    return bool(
        isinstance(change, dict)
        and float(change.get("changedPixelRatio", 1)) <= 0.12
        and float(change.get("boundsAreaRatio", 1)) <= 0.45
    )


def is_interactive_overlay_change(change: Any) -> bool:
    """Ignore caret/text noise but retain menus, calendars and popovers."""

    return bool(
        is_local_change(change)
        and float(change.get("changedPixelRatio", 0)) >= 0.0008
        and float(change.get("boundsAreaRatio", 0)) >= 0.015
        # Menus, calendars and popovers occupy a compact part of the window.
        # A changed strip spanning most of the page is normally mutable form
        # data, a result/table refresh or a responsive reflow, not an overlay.
        and float(change.get("widthRatio", 1)) <= 0.72
        and float(change.get("heightRatio", 1)) <= 0.72
    )
