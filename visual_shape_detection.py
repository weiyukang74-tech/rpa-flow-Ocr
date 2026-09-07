"""Pure image-shape detectors used by the persistent OCR page layout.

This module deliberately knows nothing about HIS pages or business field names.
It only turns an OCR label and a screenshot into generic visual candidates.
"""

from __future__ import annotations

from typing import Any

from PIL import Image


def pixel_distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> int:
    return sum(abs(int(left) - int(right)) for left, right in zip(first, second))


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

    left = seed_x
    while left > 1 and similar(left - 1, seed_y):
        left -= 1
    right = seed_x
    while right < image.width - 2 and similar(right + 1, seed_y):
        right += 1
    top = seed_y
    while top > 1 and similar(seed_x, top - 1):
        top -= 1
    bottom = seed_y
    while bottom < image.height - 2 and similar(seed_x, bottom + 1):
        bottom += 1
    width, height = right - left + 1, bottom - top + 1
    if not (45 <= width <= min(900, image.width * 0.75) and 14 <= height <= 120):
        return None
    return [left, top, right, bottom]


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

    candidates: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for relation, seed_x, seed_y in seeds:
        bounds = light_region_at(image, seed_x, seed_y)
        if bounds is None:
            continue
        key = tuple(bounds)
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
    return max(candidates.values(), key=lambda item: item["score"])


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


def find_checkbox_square(image: Image.Image, label: Any) -> dict[str, Any] | None:
    """Search left/right first, then above/below, for a square control.

    A coarse two-pixel scan is refined around its strongest candidates. This
    covers both pixel parities without paying the cost of a full-screen fine scan.
    """

    label_center_x = (label.left + label.right) // 2
    label_center_y = (label.top + label.bottom) // 2
    label_height = max(8, label.bottom - label.top)
    minimum_size = max(10, round(label_height * 0.7))
    maximum_size = min(44, max(minimum_size + 2, round(label_height * 2.8)))
    search_distance = max(80, min(180, image.width // 8))
    align_range = max(6, label_height)

    def scan(relation: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if relation == "left":
            x_values = range(max(4, label.left - search_distance), max(5, label.left - 4), 2)
            y_values = range(label_center_y - align_range, label_center_y + align_range + 1, 2)
        elif relation == "right":
            x_values = range(label.right + 4, min(image.width - 4, label.right + search_distance), 2)
            y_values = range(label_center_y - align_range, label_center_y + align_range + 1, 2)
        elif relation == "above":
            x_values = range(label_center_x - align_range, label_center_x + align_range + 1, 2)
            y_values = range(max(4, label.top - search_distance), max(5, label.top - 4), 2)
        else:
            x_values = range(label_center_x - align_range, label_center_x + align_range + 1, 2)
            y_values = range(label.bottom + 4, min(image.height - 4, label.bottom + search_distance), 2)

        def add_candidate(center_x: int, center_y: int, size: int, outline: float) -> None:
            if relation in {"left", "right"}:
                alignment_penalty = abs(center_y - label_center_y) * 1.5
                distance = label.left - center_x if relation == "left" else center_x - label.right
            else:
                alignment_penalty = abs(center_x - label_center_x) * 1.5
                distance = label.top - center_y if relation == "above" else center_y - label.bottom
            score = outline - alignment_penalty - max(0, distance) * 0.08
            results.append(
                {
                    "center": [center_x, center_y],
                    "bounds": [
                        center_x - size // 2,
                        center_y - size // 2,
                        center_x + size // 2,
                        center_y + size // 2,
                    ],
                    "relation": relation,
                    "score": round(score, 3),
                    "size": size,
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
