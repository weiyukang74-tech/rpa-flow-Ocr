"""Offline tests for the configurable automatic table-column expansion module."""

from __future__ import annotations

import unittest

from ocr_marker import OcrItem
from rpa_modules import build_registry, cluster_ocr_rows, ocr_item_looks_truncated


class ExpandTableColumnTests(unittest.TestCase):
    def test_detects_ascii_ellipsis_from_raw_ocr_text(self) -> None:
        item = OcrItem("多烯磷", 0.98, 8, 50, 55, 70, "多烯磷...")
        self.assertTrue(ocr_item_looks_truncated(item, 72, 8))

    def test_detects_unicode_ellipsis_from_raw_ocr_text(self) -> None:
        item = OcrItem("多烯磷", 0.98, 8, 50, 55, 70, "多烯磷…")
        self.assertTrue(ocr_item_looks_truncated(item, 72, 8))

    def test_uses_single_dot_at_right_edge_when_ocr_merges_ellipsis(self) -> None:
        item = OcrItem("5ml2", 0.98, 8, 50, 68, 70, "5ml:2.")
        self.assertTrue(ocr_item_looks_truncated(item, 72, 6))

    def test_complete_text_near_right_edge_is_not_truncated(self) -> None:
        item = OcrItem("每周3次", 0.98, 8, 50, 68, 70, "每周3次")
        self.assertFalse(ocr_item_looks_truncated(item, 72, 6))

    def test_complete_text_away_from_edge_is_not_truncated(self) -> None:
        item = OcrItem("阿司匹林", 0.98, 8, 50, 48, 70, "阿司匹林")
        self.assertFalse(ocr_item_looks_truncated(item, 72, 8))

    def test_registry_exposes_configurable_expand_module(self) -> None:
        definition = build_registry().get("his.expand_table_column")
        self.assertEqual(definition.name, "自动拓宽显示不全的列")
        fields = {item["name"] for item in definition.fields}
        self.assertIn("tableTitle", fields)
        self.assertNotIn("targetColumn", fields)
        self.assertNotIn("locateMode", fields)

    def test_clusters_all_visible_header_cells_into_one_row(self) -> None:
        items = [
            OcrItem("医嘱名称", 0.98, 5, 20, 70, 40),
            OcrItem("规格", 0.98, 75, 21, 120, 41),
            OcrItem("剂量", 0.98, 125, 19, 165, 39),
            OcrItem("第一行数据", 0.98, 5, 60, 70, 80),
        ]
        rows = cluster_ocr_rows(items)
        self.assertEqual([item.text for item in rows[0]], ["医嘱名称", "规格", "剂量"])
        self.assertEqual([item.text for item in rows[1]], ["第一行数据"])


if __name__ == "__main__":
    unittest.main()
