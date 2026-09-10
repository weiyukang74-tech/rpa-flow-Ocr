"""Offline checks for generic OCR table-row selection."""

from __future__ import annotations

import unittest

from ocr_marker import OcrItem
from rpa_modules import _find_table_header, _ocr_row_groups, build_registry


class TableRowClickTests(unittest.TestCase):
    def test_groups_data_row_separately_from_pagination(self) -> None:
        items = [
            OcrItem("序号", 0.99, 10, 100, 45, 122, "序号"),
            OcrItem("患者号", 0.99, 70, 100, 130, 122, "患者号"),
            OcrItem("姓名", 0.99, 160, 100, 200, 122, "姓名"),
            OcrItem("1", 0.99, 20, 136, 30, 156, "1"),
            OcrItem("P810274", 0.99, 70, 136, 135, 156, "P810274"),
            OcrItem("林若宁", 0.99, 160, 136, 215, 156, "林若宁"),
            OcrItem("第", 0.99, 20, 700, 35, 720, "第"),
            OcrItem("1", 0.99, 45, 700, 55, 720, "1"),
            OcrItem("页", 0.99, 65, 700, 80, 720, "页"),
        ]
        located = _find_table_header(items, "序号", "", None)
        self.assertIsNotNone(located)
        header, header_row = located or (None, [])
        self.assertEqual(header.text, "序号")
        self.assertEqual([item.text for item in header_row], ["序号", "患者号", "姓名"])

        groups = _ocr_row_groups(items, 125)
        self.assertEqual([item.text for item in groups[0]], ["1", "P810274", "林若宁"])
        self.assertEqual([item.text for item in groups[-1]], ["第", "1", "页"])

    def test_public_registry_uses_generic_visual_modules(self) -> None:
        registry = build_registry()
        public_types = {item["type"] for item in registry.public_modules()}
        self.assertIn("visual.click_table_row", public_types)
        self.assertIn("visual.input_field", public_types)
        self.assertFalse(any(item.startswith("his.") for item in public_types))
        self.assertTrue(registry.get("his.click_first_result").hidden)


if __name__ == "__main__":
    unittest.main()
