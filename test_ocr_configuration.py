"""Tests for per-step OCR table targeting without opening the HIS window."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

from ocr_marker import (
    OcrItem,
    find_header_matches,
    parse_ocr_result,
    run_ocr,
    visual_text_segments,
)


def item(text: str, left: int, top: int, width: int = 70) -> OcrItem:
    return OcrItem(text, 0.98, left, top, left + width, top + 24)


class OcrConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            item("患者查询", 20, 20, 90),
            item("姓名", 200, 100),  # 查询表单标签，不应被当成表头。
            item("住院号", 400, 145),
            item("患者号", 80, 320),
            item("姓名", 200, 320),
            item("住院号", 420, 320),
        ]

    def test_configured_title_and_search_height_locate_distant_header(self) -> None:
        matches, title = find_header_matches(
            self.items,
            1000,
            ["患者号", "姓名", "住院号"],
            "患者查询",
            1.0,
            400,
        )
        self.assertEqual(title.text, "患者查询")
        self.assertEqual([match.target for match in matches], ["患者号", "姓名", "住院号"])
        self.assertTrue(all(match.item.top == 320 for match in matches))

    def test_auto_mode_chooses_row_with_most_target_columns(self) -> None:
        matches, _title = find_header_matches(
            self.items,
            1000,
            ["患者号", "姓名", "住院号"],
            "",
            1.0,
            90,
        )
        self.assertEqual([match.target for match in matches], ["患者号", "姓名", "住院号"])
        self.assertTrue(all(match.item.top == 320 for match in matches))

    def test_missing_configured_title_reports_configured_value(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "费用明细"):
            find_header_matches(
                self.items,
                1000,
                ["患者号"],
                "费用明细",
                1.0,
                100,
            )

    def test_low_confidence_text_can_be_collected_for_weak_candidates(self) -> None:
        result = SimpleNamespace(
            boxes=[[[10, 10], [40, 10], [40, 30], [10, 30]]],
            txts=["模糊字段"],
            scores=[0.30],
        )

        self.assertEqual(parse_ocr_result(result, 1.0), [])
        weak_items = parse_ocr_result(result, 1.0, min_score=0.20)
        self.assertEqual(len(weak_items), 1)
        self.assertEqual(weak_items[0].text, "模糊字段")

    def test_empty_rapidocr_output_is_not_iterated(self) -> None:
        result = SimpleNamespace(
            boxes=None,
            txts=None,
            scores=None,
            to_json=lambda: None,
        )

        self.assertEqual(parse_ocr_result(result, 1.0), [])

    def test_ocr_runs_in_memory_with_adaptive_scale_and_threshold(self) -> None:
        calls = []

        def engine(image: np.ndarray, **kwargs: object) -> SimpleNamespace:
            calls.append((image.shape, kwargs))
            return SimpleNamespace(
                boxes=[[[20, 20], [80, 20], [80, 60], [20, 60]]],
                txts=["姓名"],
                scores=[0.90],
            )

        items = run_ocr(
            engine,
            Image.new("RGB", (1000, 500), "white"),
            min_score=0.20,
        )

        self.assertEqual(calls[0][0], (1000, 2000, 3))
        self.assertEqual(calls[0][1]["text_score"], 0.20)
        self.assertFalse(calls[0][1]["use_cls"])
        self.assertEqual(items[0].left, 10)
        self.assertEqual(items[0].right, 40)

    def test_wide_navigation_text_is_split_at_large_visual_gaps(self) -> None:
        image = Image.new("RGB", (340, 44), (0, 126, 190))
        pixels = np.asarray(image).copy()
        cursor = 12
        group_bounds = []
        for character_count in (6, 4, 6):
            group_left = cursor
            for _ in range(character_count):
                pixels[12:32, cursor : cursor + 7] = 255
                cursor += 10
            group_bounds.append((group_left, cursor - 3))
            cursor += 15
        image = Image.fromarray(pixels)
        merged = OcrItem(
            "工伤标志查询系统配置医保住院登记",
            0.98,
            group_bounds[0][0],
            12,
            group_bounds[-1][1],
            32,
        )

        segments = visual_text_segments(image, merged)

        self.assertEqual(len(segments), 3)
        self.assertLess(segments[0][2], segments[1][0] + 2)
        self.assertLess(segments[1][2], segments[2][0] + 2)


if __name__ == "__main__":
    unittest.main()
