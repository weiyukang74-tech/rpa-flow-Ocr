"""Tests for per-step OCR table targeting without opening the HIS window."""

from __future__ import annotations

import unittest

from ocr_marker import OcrItem, find_header_matches


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


if __name__ == "__main__":
    unittest.main()
