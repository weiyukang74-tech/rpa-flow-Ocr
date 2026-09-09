"""Pure image-shape detectors used by the persistent OCR page layout.

This module deliberately knows nothing about HIS pages or business field names.
It only turns an OCR label and a screenshot into generic visual candidates.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from PIL import Image


def pixel_distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> int:
    return sum(abs(int(left) - int(right)) for left, right in zip(first, second))


def _true_runs(values: np.ndarray) -> list[tuple[int, int]]:
    padded = np.pad(values.astype(np.int8), (1, 1))
    transitions = np.diff(padded)
    starts = np.flatnonzero(transitions == 1)
    ends = np.flatnonzero(transitions == -1)
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def _border_coverage(edges: np.ndarray, bounds: list[int]) -> tuple[float, float]:
    """Return minimum and average edge coverage around a rectangle border."""

    left, top, right, bottom = bounds
    height, width = edges.shape
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width - 1, right))
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height - 1, bottom))
    band = 2
    sides = (
        edges[max(0, top - band) : min(height, top + band + 1), left : right + 1],
        edges[max(0, bottom - band) : min(height, bottom + band + 1), left : right + 1],
        edges[top : bottom + 1, max(0, left - band) : min(width, left + band + 1)],
        edges[top : bottom + 1, max(0, right - band) : min(width, right + band + 1)],
    )
    coverages = [float(np.count_nonzero(side)) / max(1, side.size) for side in sides]
    return min(coverages), sum(coverages) / len(coverages)


def _intersection_over_union(first: list[int], second: list[int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = max(1, first[2] - first[0]) * max(1, first[3] - first[1])
    second_area = max(1, second[2] - second[0]) * max(1, second[3] - second[1])
    return intersection / max(1, first_area + second_area - intersection)


def _deduplicate_rectangles(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the inner and outer contours produced by one painted border."""

    selected: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        bounds = candidate["bounds"]
        center_x, center_y = candidate["center"]
        duplicate = False
        for existing in selected:
            existing_x, existing_y = existing["center"]
            existing_bounds = existing["bounds"]
            if (
                _intersection_over_union(bounds, existing_bounds) >= 0.72
                or (
                    abs(center_x - existing_x) <= 3
                    and abs(center_y - existing_y) <= 3
                    and abs((bounds[2] - bounds[0]) - (existing_bounds[2] - existing_bounds[0])) <= 6
                    and abs((bounds[3] - bounds[1]) - (existing_bounds[3] - existing_bounds[1])) <= 6
                )
            ):
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
    return sorted(selected, key=lambda item: (item["bounds"][1], item["bounds"][0]))


def detect_visual_control_candidates(image: Image.Image) -> dict[str, list[dict[str, Any]]]:
    """Extract field-like rectangles and small square controls in one pass.

    Contours are calculated once for the entire screenshot. Later classification
    only associates these reusable candidates with OCR labels, avoiding a fresh
    neighborhood scan for every text fragment.
    """

    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 22, 72)
    closed = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
    )
    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    fields: list[dict[str, Any]] = []
    squares: list[dict[str, Any]] = []
    image_area = max(1, image.width * image.height)

    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < 8 or height < 8 or width * height > image_area * 0.30:
            continue
        right, bottom = x + width - 1, y + height - 1
        bounds = [int(x), int(y), int(right), int(bottom)]
        minimum_border, average_border = _border_coverage(edges, bounds)
        contour_area = float(abs(cv2.contourArea(contour)))
        rectangularity = contour_area / max(1.0, float(width * height))
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        center = [int(x + width // 2), int(y + height // 2)]

        if (
            45 <= width <= min(900, round(image.width * 0.75))
            and 18 <= height <= 120
            and width / max(1, height) >= 1.45
            and minimum_border >= 0.10
            and average_border >= 0.20
            and rectangularity >= 0.42
            and 4 <= len(polygon) <= 12
        ):
            inset = max(2, min(6, height // 5))
            interior = rgb[
                min(image.height, y + inset) : max(
                    y + inset + 1,
                    min(image.height, y + height - inset),
                ),
                min(image.width, x + inset) : max(
                    x + inset + 1,
                    min(image.width, x + width - inset),
                ),
            ]
            interior_luminance = float(interior.mean()) if interior.size else 0.0
            if interior_luminance >= 175:
                score = (
                    average_border * 75
                    + minimum_border * 35
                    + min(1.0, rectangularity) * 20
                    + max(0.0, 1.0 - abs(height - 34) / 80) * 10
                )
                fields.append(
                    {
                        "bounds": bounds,
                        "center": center,
                        "score": round(score, 3),
                        "source": "global-contour",
                    }
                )

        aspect = width / max(1, height)
        if (
            8 <= width <= 48
            and 8 <= height <= 48
            and 0.65 <= aspect <= 1.45
            and minimum_border >= 0.10
            and average_border >= 0.20
            and rectangularity >= 0.25
            and 4 <= len(polygon) <= 16
        ):
            score = (
                average_border * 80
                + minimum_border * 40
                + min(1.0, rectangularity) * 20
                + max(0.0, 1.0 - abs(width - height) / max(width, height)) * 15
            )
            squares.append(
                {
                    "bounds": bounds,
                    "center": center,
                    "score": round(score, 3),
                    "source": "global-contour",
                }
            )

    fields = _deduplicate_rectangles(fields)
    squares = _deduplicate_rectangles(squares)

    # Calendar icons and combo arrows live inside a field rectangle. They are
    # not independent checkboxes, so enforce mutual exclusion before labels are
    # associated with the candidate pool.
    independent_squares = []
    for square in squares:
        center_x, center_y = square["center"]
        if any(
            field["bounds"][0] - 2 <= center_x <= field["bounds"][2] + 2
            and field["bounds"][1] - 2 <= center_y <= field["bounds"][3] + 2
            for field in fields
        ):
            continue
        independent_squares.append(square)

    return {
        "fieldRectangles": fields,
        "squareControls": independent_squares,
    }


def detect_foreground_panel(image: Image.Image) -> dict[str, Any] | None:
    """Detect a bright foreground dialog displayed over a dimmed application.

    Detection is based only on panel geometry and foreground/background contrast;
    it does not depend on window titles, button names or HIS-specific positions.
    """

    if image.width < 320 or image.height < 240:
        return None
    scale = min(1.0, 320 / image.width, 220 / image.height)
    sample_width = max(80, round(image.width * scale))
    sample_height = max(60, round(image.height * scale))
    sample = np.asarray(
        image.convert("RGB").resize(
            (sample_width, sample_height),
            Image.Resampling.BILINEAR,
        ),
        dtype=np.float32,
    )
    luminance = (
        sample[:, :, 0] * 0.299
        + sample[:, :, 1] * 0.587
        + sample[:, :, 2] * 0.114
    )

    vertical_start = round(sample_height * 0.15)
    vertical_end = round(sample_height * 0.90)
    column_means = luminance[vertical_start:vertical_end].mean(axis=0)
    column_runs = [
        (start, end)
        for start, end in _true_runs(column_means >= 214)
        if sample_width * 0.35 <= end - start <= sample_width * 0.94
        and start >= sample_width * 0.025
        and sample_width - end >= sample_width * 0.025
    ]
    if not column_runs:
        return None
    left, right = max(column_runs, key=lambda run: run[1] - run[0])

    row_means = luminance[:, left:right].mean(axis=1)
    row_runs = [
        (start, end)
        for start, end in _true_runs(row_means >= 218)
        if end - start >= sample_height * 0.20
    ]
    if not row_runs:
        return None
    top, bottom = max(row_runs, key=lambda run: run[1] - run[0])

    outside_parts = []
    if left > 1:
        outside_parts.append(luminance[top:bottom, :left])
    if right < sample_width - 1:
        outside_parts.append(luminance[top:bottom, right:])
    if not outside_parts:
        return None
    outside_values = np.concatenate([part.reshape(-1) for part in outside_parts])
    inside_mean = float(luminance[top:bottom, left:right].mean())
    outside_mean = float(outside_values.mean())
    contrast = inside_mean - outside_mean
    if contrast < 16:
        return None

    # The bright body usually begins below a colored dialog title bar. Expand
    # the detected body so header controls and footer buttons remain in scope.
    left = max(0, left - round(sample_width * 0.01))
    right = min(sample_width, right + round(sample_width * 0.01))
    top = max(0, top - round(sample_height * 0.08))
    bottom = min(sample_height, bottom + round(sample_height * 0.04))
    bounds = [
        round(left * image.width / sample_width),
        round(top * image.height / sample_height),
        round(right * image.width / sample_width),
        round(bottom * image.height / sample_height),
    ]
    return {
        "kind": "foreground-dialog",
        "bounds": bounds,
        "confidence": round(min(1.0, contrast / 55), 3),
    }


def light_region_at(
    image: Image.Image,
    seed_x: int,
    seed_y: int,
) -> list[int] | None:
    """Find a bounded light rectangular interior around one candidate seed."""

    if not (1 <= seed_x < image.width - 1 and 1 <= seed_y < image.height - 1):
        return None
    base = image.getpixel((seed_x, seed_y))
    if sum(base) < 610 or max(base) - min(base) > 55:
        return None

    def similar(x: int, y: int) -> bool:
        pixel = image.getpixel((x, y))
        return sum(pixel) >= 585 and pixel_distance(pixel, base) <= 48

    top = seed_y
    while top > 1 and similar(seed_x, top - 1):
        top -= 1
    bottom = seed_y
    while bottom < image.height - 2 and similar(seed_x, bottom + 1):
        bottom += 1
    height = bottom - top + 1
    if not 14 <= height <= 120:
        return None

    # Text and icons split an otherwise uniform input interior on its center row.
    # Scan columns across the full interior height so those small dark strokes do
    # not truncate the detected rectangle.
    sample_step = max(1, height // 12)

    def light_column(x: int) -> bool:
        samples = list(range(top, bottom + 1, sample_step))
        return sum(similar(x, y) for y in samples) / max(1, len(samples)) >= 0.30

    left = seed_x
    while left > 1 and light_column(left - 1):
        left -= 1
    right = seed_x
    while right < image.width - 2 and light_column(right + 1):
        right += 1
    width, height = right - left + 1, bottom - top + 1
    if not (45 <= width <= min(900, image.width * 0.75) and 14 <= height <= 120):
        return None
    return [left, top, right, bottom]


def input_bounds_match_label(
    bounds: Any,
    label_bounds: Any,
    relation: str,
) -> bool:
    """Reject rectangles that overlap a label or contradict their relation."""

    if not (
        isinstance(bounds, (list, tuple))
        and len(bounds) == 4
        and isinstance(label_bounds, (list, tuple))
        and len(label_bounds) == 4
    ):
        return False
    try:
        left, top, right, bottom = (int(value) for value in bounds)
        label_left, label_top, label_right, label_bottom = (
            int(value) for value in label_bounds
        )
    except (TypeError, ValueError):
        return False
    if right <= left or bottom <= top or label_right <= label_left or label_bottom <= label_top:
        return False

    overlap_width = max(0, min(right, label_right) - max(left, label_left))
    overlap_height = max(0, min(bottom, label_bottom) - max(top, label_top))
    overlap_area = overlap_width * overlap_height
    smaller_area = min(
        (right - left) * (bottom - top),
        (label_right - label_left) * (label_bottom - label_top),
    )
    if overlap_area / max(1, smaller_area) > 0.05:
        return False

    label_center_x = (label_left + label_right) / 2
    label_center_y = (label_top + label_bottom) / 2
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    label_height = max(8, label_bottom - label_top)
    label_width = max(8, label_right - label_left)
    if relation == "right":
        return -2 <= left - label_right <= 72 and abs(center_y - label_center_y) <= max(24, label_height)
    if relation == "left":
        return -2 <= label_left - right <= 72 and abs(center_y - label_center_y) <= max(24, label_height)
    if relation == "below":
        horizontally_related = right >= label_left and left <= label_right
        return (
            -2 <= top - label_bottom <= 60
            and horizontally_related
            and abs(center_x - label_center_x) <= max(100, label_width * 2)
        )
    if relation == "above":
        horizontally_related = right >= label_left and left <= label_right
        return (
            -2 <= label_top - bottom <= 60
            and horizontally_related
            and abs(center_x - label_center_x) <= max(100, label_width * 2)
        )
    return False


def detect_table_regions(image: Image.Image) -> list[dict[str, Any]]:
    """Detect grid-like table regions from repeated long vertical separators.

    The scan is performed once per new full-page map. Short input borders do not
    qualify, while table columns share nearly the same long vertical span.
    """

    rgb = image.convert("RGB")
    pixels = rgb.load()
    minimum_span = max(60, round(rgb.height * 0.065))
    raw_lines: list[tuple[int, int, int]] = []

    for x in range(1, rgb.width):
        best: tuple[int, int] | None = None
        start: int | None = None
        last_edge: int | None = None
        edge_count = 0
        for y in range(rgb.height):
            edge = pixel_distance(pixels[x, y], pixels[x - 1, y]) >= 18
            if edge:
                if start is None:
                    start = y
                    edge_count = 0
                last_edge = y
                edge_count += 1
            elif start is not None and last_edge is not None and y - last_edge > 2:
                span = last_edge - start + 1
                if (
                    span >= minimum_span
                    and edge_count / span >= 0.70
                    and (best is None or span > best[1] - best[0] + 1)
                ):
                    best = (start, last_edge)
                start = last_edge = None
                edge_count = 0
        if start is not None and last_edge is not None:
            span = last_edge - start + 1
            if (
                span >= minimum_span
                and edge_count / span >= 0.70
                and (best is None or span > best[1] - best[0] + 1)
            ):
                best = (start, last_edge)
        if best is not None:
            raw_lines.append((x, best[0], best[1]))

    grouped: list[list[tuple[int, int, int]]] = []
    for line in raw_lines:
        if (
            grouped
            and line[0] <= grouped[-1][-1][0] + 2
            and abs(line[1] - grouped[-1][0][1]) <= 4
            and abs(line[2] - grouped[-1][0][2]) <= 4
        ):
            grouped[-1].append(line)
        else:
            grouped.append([line])
    lines = [
        {
            "x": round(sum(line[0] for line in group) / len(group)),
            "top": round(sum(line[1] for line in group) / len(group)),
            "bottom": round(sum(line[2] for line in group) / len(group)),
        }
        for group in grouped
    ]

    clusters: list[list[dict[str, int]]] = []
    for line in lines:
        matched = next(
            (
                cluster
                for cluster in clusters
                if abs(line["top"] - cluster[0]["top"]) <= 8
                and abs(line["bottom"] - cluster[0]["bottom"]) <= 8
            ),
            None,
        )
        if matched is None:
            clusters.append([line])
        else:
            matched.append(line)

    tables: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster.sort(key=lambda line: line["x"])
        if len(cluster) < 4 or cluster[-1]["x"] - cluster[0]["x"] < rgb.width * 0.15:
            continue
        top = round(sum(line["top"] for line in cluster) / len(cluster))
        bottom = round(sum(line["bottom"] for line in cluster) / len(cluster)) + 1
        enclosing = [
            line
            for line in lines
            if line["top"] <= top + 3 and line["bottom"] >= bottom - 4
        ]
        left_options = [line["x"] for line in enclosing if line["x"] < cluster[0]["x"]]
        right_options = [line["x"] for line in enclosing if line["x"] > cluster[-1]["x"]]
        left = max(left_options) if left_options else cluster[0]["x"]
        right = min(right_options) if right_options else cluster[-1]["x"]
        bounds = [left, top, right, bottom]
        if any(existing["bounds"] == bounds for existing in tables):
            continue
        tables.append(
            {
                "kind": "table",
                "bounds": bounds,
                "columnLines": [line["x"] for line in cluster],
                "confidence": round(min(1.0, len(cluster) / 8), 3),
            }
        )
    return tables


def find_input_rectangle(image: Image.Image, label: Any) -> dict[str, Any] | None:
    """Search all common label/control directions for a light input rectangle."""

    label_center_x = (label.left + label.right) // 2
    label_center_y = (label.top + label.bottom) // 2
    label_width = max(8, label.right - label.left)
    seeds: list[tuple[str, int, int]] = []
    for distance in range(10, 151, 8):
        seeds.extend(
            [
                ("right", label.right + distance, label_center_y),
                ("left", label.left - distance, label_center_y),
            ]
        )
    below_x_values = {
        label.left + 20,
        label.left + max(35, label_width),
        label_center_x,
        label.right + 20,
    }
    for distance in range(8, 101, 6):
        for seed_x in below_x_values:
            seeds.append(("below", seed_x, label.bottom + distance))
            seeds.append(("above", seed_x, label.top - distance))

    candidates: dict[tuple[int, int, int, int, str], dict[str, Any]] = {}
    label_bounds = [label.left, label.top, label.right, label.bottom]
    for relation, seed_x, seed_y in seeds:
        bounds = light_region_at(image, seed_x, seed_y)
        if bounds is None or not input_bounds_match_label(bounds, label_bounds, relation):
            continue
        key = (*bounds, relation)
        center_x = (bounds[0] + bounds[2]) // 2
        center_y = (bounds[1] + bounds[3]) // 2
        distance = (
            (center_x - label_center_x) ** 2 + (center_y - label_center_y) ** 2
        ) ** 0.5
        height = bounds[3] - bounds[1] + 1
        score = 100.0 - distance * 0.08 - abs(height - 34) * 0.35
        candidate = {
            "bounds": bounds,
            "center": [center_x, center_y],
            "relation": relation,
            "score": round(score, 3),
        }
        if key not in candidates or score > candidates[key]["score"]:
            candidates[key] = candidate
    if not candidates:
        return None
    horizontal = [
        candidate
        for candidate in candidates.values()
        if candidate["relation"] in {"left", "right"}
    ]
    if horizontal:
        return max(horizontal, key=lambda item: item["score"])
    vertical = [
        candidate
        for candidate in candidates.values()
        if candidate["relation"] in {"above", "below"}
    ]
    return max(vertical, key=lambda item: item["score"]) if vertical else None


def match_input_candidate(
    candidates: list[dict[str, Any]],
    label: Any,
) -> dict[str, Any] | None:
    """Associate one OCR label with the best already-detected field rectangle."""

    label_bounds = [label.left, label.top, label.right, label.bottom]
    label_center_x = (label.left + label.right) / 2
    label_center_y = (label.top + label.bottom) / 2
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        left, top, right, bottom = candidate["bounds"]
        center_x, center_y = candidate["center"]
        relations: list[str] = []
        if left >= label.right - 2:
            relations.append("right")
        if right <= label.left + 2:
            relations.append("left")
        if top >= label.bottom - 2:
            relations.append("below")
        if bottom <= label.top + 2:
            relations.append("above")
        for relation in relations:
            if not input_bounds_match_label(candidate["bounds"], label_bounds, relation):
                continue
            distance = (
                (center_x - label_center_x) ** 2
                + (center_y - label_center_y) ** 2
            ) ** 0.5
            height = bottom - top + 1
            relation_bonus = 18.0 if relation in {"left", "right"} else 0.0
            score = (
                float(candidate.get("score") or 0.0)
                + relation_bonus
                - distance * 0.08
                - abs(height - 34) * 0.35
            )
            matches.append(
                {**candidate, "relation": relation, "score": round(score, 3)}
            )
    if not matches:
        return None
    horizontal = [
        item for item in matches if item["relation"] in {"left", "right"}
    ]
    return max(horizontal or matches, key=lambda item: item["score"])


def square_outline_score(
    image: Image.Image,
    center_x: int,
    center_y: int,
    size: int,
) -> float:
    """Score a square by contrast on all four borders against outside pixels."""

    half = size // 2
    left, top = center_x - half, center_y - half
    right, bottom = center_x + half, center_y + half
    if left < 3 or top < 3 or right >= image.width - 3 or bottom >= image.height - 3:
        return 0.0
    step = max(1, size // 8)
    side_deltas: list[list[int]] = [[], [], [], []]
    for x in range(left + 1, right, step):
        side_deltas[0].append(
            pixel_distance(image.getpixel((x, top)), image.getpixel((x, top - 2)))
        )
        side_deltas[1].append(
            pixel_distance(image.getpixel((x, bottom)), image.getpixel((x, bottom + 2)))
        )
    for y in range(top + 1, bottom, step):
        side_deltas[2].append(
            pixel_distance(image.getpixel((left, y)), image.getpixel((left - 2, y)))
        )
        side_deltas[3].append(
            pixel_distance(image.getpixel((right, y)), image.getpixel((right + 2, y)))
        )
    if any(not values for values in side_deltas):
        return 0.0
    coverages = [
        sum(delta >= 24 for delta in values) / len(values) for values in side_deltas
    ]
    if min(coverages) < 0.35:
        return 0.0
    average_delta = sum(sum(values) for values in side_deltas) / sum(
        len(values) for values in side_deltas
    )
    return min(coverages) * 100.0 + average_delta * 0.2


def checkbox_bounds_match_label(
    bounds: Any,
    label_bounds: Any,
    relation: str,
) -> bool:
    """Return whether a small square is plausibly attached to its text label."""

    if not (
        isinstance(bounds, (list, tuple))
        and len(bounds) == 4
        and isinstance(label_bounds, (list, tuple))
        and len(label_bounds) == 4
    ):
        return False
    try:
        left, top, right, bottom = (float(value) for value in bounds)
        label_left, label_top, label_right, label_bottom = (
            float(value) for value in label_bounds
        )
    except (TypeError, ValueError):
        return False
    width, height = right - left, bottom - top
    label_height = max(8.0, label_bottom - label_top)
    if width < 8 or height < 8 or width > 48 or height > 48:
        return False
    if not 0.65 <= width / max(height, 1.0) <= 1.45:
        return False

    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    label_center_x = (label_left + label_right) / 2
    label_center_y = (label_top + label_bottom) / 2
    maximum_gap = max(36.0, label_height * 2.5)
    inline_tolerance = max(6.0, label_height * 0.65)
    if relation == "ocr-prefix":
        return (
            label_left - 3 <= center_x <= label_left + max(22.0, label_height * 1.2)
            and abs(center_y - label_center_y) <= inline_tolerance
        )
    if relation == "left":
        gap = label_left - right
        return -2 <= gap <= maximum_gap and abs(center_y - label_center_y) <= inline_tolerance
    if relation == "right":
        gap = left - label_right
        return -2 <= gap <= maximum_gap and abs(center_y - label_center_y) <= inline_tolerance
    if relation == "above":
        gap = label_top - bottom
        return -2 <= gap <= maximum_gap and abs(center_x - label_center_x) <= label_height * 1.5
    if relation == "below":
        gap = top - label_bottom
        return -2 <= gap <= maximum_gap and abs(center_x - label_center_x) <= label_height * 1.5
    return False


def find_checkbox_square(image: Image.Image, label: Any) -> dict[str, Any] | None:
    """Search left/right first, then above/below, for a square control.

    A coarse two-pixel scan is refined around its strongest candidates. This
    covers both pixel parities without paying the cost of a full-screen fine scan.
    """

    label_center_x = (label.left + label.right) // 2
    label_center_y = (label.top + label.bottom) // 2
    label_height = max(8, label.bottom - label.top)
    raw_text = "".join(str(getattr(label, "raw_text", "") or "").split())
    checkbox_prefixes = ("✅", "☑", "☒", "☐", "□", "▣", "■", "✔", "✓")
    if raw_text.startswith(checkbox_prefixes):
        # Some OCR engines return the checkbox glyph and its label as one item.
        # Its left edge is then more reliable than scanning a filled square,
        # whose border may be obscured by its checked background.
        size = min(32, max(12, round(label_height * 0.78)))
        center_x = label.left + max(6, size // 2)
        center_y = label_center_y
        half = size // 2
        return {
            "bounds": [
                center_x - half,
                center_y - half,
                center_x + half,
                center_y + half,
            ],
            "center": [center_x, center_y],
            "relation": "ocr-prefix",
            "score": 150.0,
        }
    minimum_size = max(10, round(label_height * 0.7))
    maximum_size = min(44, max(minimum_size + 2, round(label_height * 2.8)))
    # A checkbox belongs close to its label. Restricting the scan also prevents
    # a distant select arrow or input border from becoming the label's square.
    search_distance = min(90, max(52, round(label_height * 3.5)))
    align_range = max(6, label_height)

    def scan(relation: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if relation == "left":
            x_values = range(max(4, label.left - search_distance), max(5, label.left - 3))
            y_values = range(label_center_y - align_range, label_center_y + align_range + 1, 2)
        elif relation == "right":
            x_values = range(label.right + 3, min(image.width - 4, label.right + search_distance))
            y_values = range(label_center_y - align_range, label_center_y + align_range + 1, 2)
        elif relation == "above":
            x_values = range(label_center_x - align_range, label_center_x + align_range + 1, 2)
            y_values = range(max(4, label.top - search_distance), max(5, label.top - 4), 2)
        else:
            x_values = range(label_center_x - align_range, label_center_x + align_range + 1, 2)
            y_values = range(label.bottom + 4, min(image.height - 4, label.bottom + search_distance), 2)

        def add_candidate(center_x: int, center_y: int, size: int, outline: float) -> None:
            bounds = [
                center_x - size // 2,
                center_y - size // 2,
                center_x + size // 2,
                center_y + size // 2,
            ]
            label_bounds = [label.left, label.top, label.right, label.bottom]
            if not checkbox_bounds_match_label(bounds, label_bounds, relation):
                return
            if relation in {"left", "right"}:
                alignment_penalty = abs(center_y - label_center_y) * 1.5
                gap = label.left - bounds[2] if relation == "left" else bounds[0] - label.right
            else:
                alignment_penalty = abs(center_x - label_center_x) * 1.5
                gap = label.top - bounds[3] if relation == "above" else bounds[1] - label.bottom
            score = outline - alignment_penalty - max(0, gap) * 0.35
            results.append(
                {
                    "center": [center_x, center_y],
                    "bounds": bounds,
                    "relation": relation,
                    "score": round(score, 3),
                    "size": size,
                    "gap": round(max(0, gap), 3),
                }
            )

        for center_x in x_values:
            for center_y in y_values:
                for size in range(minimum_size, maximum_size + 1, 2):
                    outline = square_outline_score(image, center_x, center_y, size)
                    if outline > 0:
                        add_candidate(center_x, center_y, size, outline)

        # Refine only around the strongest coarse locations. This catches a
        # border whose true center or size lies between coarse sample points.
        coarse_best = sorted(results, key=lambda item: item["score"], reverse=True)[:6]
        coarse_best.extend(
            sorted(results, key=lambda item: (item["gap"], -item["score"]))[:4]
        )
        for candidate in coarse_best:
            base_x, base_y = candidate["center"]
            base_size = candidate["size"]
            for center_x in range(base_x - 2, base_x + 3):
                for center_y in range(base_y - 2, base_y + 3):
                    for size in range(
                        max(minimum_size, base_size - 3),
                        min(maximum_size, base_size + 3) + 1,
                    ):
                        outline = square_outline_score(image, center_x, center_y, size)
                        if outline > 0:
                            add_candidate(center_x, center_y, size, outline)
        return results

    # 左右属于同一优先级；只有两侧都没有可信候选时才搜索上下。
    horizontal = scan("left") + scan("right")
    if horizontal:
        best = max(horizontal, key=lambda item: item["score"])
        if best["score"] >= 42:
            return best
    vertical = scan("above") + scan("below")
    if vertical:
        best = max(vertical, key=lambda item: item["score"])
        if best["score"] >= 42:
            return best
    return None


def match_checkbox_candidate(
    candidates: list[dict[str, Any]],
    label: Any,
) -> dict[str, Any] | None:
    """Associate one OCR label with a precomputed square-control candidate."""

    label_center_x = (label.left + label.right) / 2
    label_center_y = (label.top + label.bottom) / 2
    label_height = max(8, label.bottom - label.top)
    raw_text = "".join(str(getattr(label, "raw_text", "") or "").split())
    checkbox_prefixes = ("✅", "☑", "☒", "☐", "□", "▣", "■", "✔", "✓")
    if raw_text.startswith(checkbox_prefixes):
        size = min(32, max(12, round(label_height * 0.78)))
        center_x = label.left + max(6, size // 2)
        center_y = round(label_center_y)
        half = size // 2
        return {
            "bounds": [
                center_x - half,
                center_y - half,
                center_x + half,
                center_y + half,
            ],
            "center": [center_x, center_y],
            "relation": "ocr-prefix",
            "score": 150.0,
            "source": "ocr-prefix",
        }

    label_bounds = [label.left, label.top, label.right, label.bottom]
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        left, top, right, bottom = candidate["bounds"]
        center_x, center_y = candidate["center"]
        relations: list[str] = []
        if right <= label.left + 2:
            relations.append("left")
        if left >= label.right - 2:
            relations.append("right")
        if bottom <= label.top + 2:
            relations.append("above")
        if top >= label.bottom - 2:
            relations.append("below")
        for relation in relations:
            if not checkbox_bounds_match_label(
                candidate["bounds"], label_bounds, relation
            ):
                continue
            if relation in {"left", "right"}:
                alignment_penalty = abs(center_y - label_center_y) * 1.5
                gap = label.left - right if relation == "left" else left - label.right
                relation_bonus = 15.0
            else:
                alignment_penalty = abs(center_x - label_center_x) * 1.5
                gap = label.top - bottom if relation == "above" else top - label.bottom
                relation_bonus = 0.0
            score = (
                float(candidate.get("score") or 0.0)
                + relation_bonus
                - alignment_penalty
                - max(0.0, gap) * 0.35
            )
            matches.append(
                {
                    **candidate,
                    "relation": relation,
                    "score": round(score, 3),
                    "gap": round(max(0.0, gap), 3),
                }
            )
    if not matches:
        return None
    horizontal = [
        item for item in matches if item["relation"] in {"left", "right"}
    ]
    return max(horizontal or matches, key=lambda item: item["score"])
