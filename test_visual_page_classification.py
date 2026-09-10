"""Offline tests for whole-page OCR/control association."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from ocr_marker import OcrItem
from rpa_modules import (
    choose_visual_text_match,
    classify_visual_page,
    run_visual_set_expand_state,
)
from workflow_engine import WorkflowExecutionError


class VisualPageClassificationTests(unittest.TestCase):
    def make_expand_context(self) -> SimpleNamespace:
        window = SimpleNamespace(
            rectangle=lambda: SimpleNamespace(left=100, top=200)
        )
        return SimpleNamespace(
            state={"window": window},
            wait=lambda _seconds: None,
            emit=lambda *_args: None,
        )

    def test_set_expand_state_clicks_once_and_verifies_result(self) -> None:
        context = self.make_expand_context()
        collapsed = OcrItem("更多v", 0.98, 45, 45, 75, 58)
        expanded = OcrItem("收起", 0.98, 45, 45, 75, 58)
        params = {
            "targetState": "展开",
            "collapsedText": "更多",
            "expandedText": "收起",
            "waitAfterClickSeconds": 0,
            "verificationAttempts": 2,
        }
        with (
            patch("rpa_modules.visual_window_signature", return_value={}),
            patch("rpa_modules.persisted_page_candidates", return_value=[{}]),
            patch(
                "rpa_modules.visual_toggle_anchor",
                return_value=([60, 51], [40, 40, 80, 62]),
            ),
            patch(
                "rpa_modules.capture_visual_window",
                return_value=Image.new("RGB", (200, 120), "white"),
            ),
            patch(
                "rpa_modules.local_visual_ocr",
                side_effect=[[collapsed], [expanded]],
            ),
            patch("rpa_modules.mouse.click") as click,
        ):
            result = run_visual_set_expand_state(context, params)

        click.assert_called_once_with(button="left", coords=(160, 251))
        self.assertTrue(result["clicked"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["state"], "展开")

    def test_set_expand_state_does_not_click_when_already_expanded(self) -> None:
        context = self.make_expand_context()
        params = {
            "targetState": "展开",
            "collapsedText": "更多",
            "expandedText": "收起",
        }
        with (
            patch("rpa_modules.visual_window_signature", return_value={}),
            patch("rpa_modules.persisted_page_candidates", return_value=[{}]),
            patch(
                "rpa_modules.visual_toggle_anchor",
                return_value=([60, 51], [40, 40, 80, 62]),
            ),
            patch(
                "rpa_modules.capture_visual_window",
                return_value=Image.new("RGB", (200, 120), "white"),
            ),
            patch(
                "rpa_modules.local_visual_ocr",
                return_value=[OcrItem("收起^", 0.98, 45, 45, 75, 58)],
            ),
            patch("rpa_modules.mouse.click") as click,
        ):
            result = run_visual_set_expand_state(context, params)

        click.assert_not_called()
        self.assertFalse(result["clicked"])
        self.assertTrue(result["verified"])

    def test_set_expand_state_can_collapse_an_expanded_region(self) -> None:
        context = self.make_expand_context()
        expanded = OcrItem("收起^", 0.98, 45, 45, 75, 58)
        collapsed = OcrItem("更多v", 0.98, 45, 45, 75, 58)
        params = {
            "targetState": "收起",
            "collapsedText": "更多",
            "expandedText": "收起",
            "waitAfterClickSeconds": 0,
        }
        with (
            patch("rpa_modules.visual_window_signature", return_value={}),
            patch("rpa_modules.persisted_page_candidates", return_value=[{}]),
            patch(
                "rpa_modules.visual_toggle_anchor",
                return_value=([60, 51], [40, 40, 80, 62]),
            ),
            patch(
                "rpa_modules.capture_visual_window",
                return_value=Image.new("RGB", (200, 120), "white"),
            ),
            patch(
                "rpa_modules.local_visual_ocr",
                side_effect=[[expanded], [collapsed]],
            ),
            patch("rpa_modules.mouse.click") as click,
        ):
            result = run_visual_set_expand_state(context, params)

        click.assert_called_once()
        self.assertEqual(result["state"], "收起")

    def test_set_expand_state_refuses_to_click_when_state_is_unknown(self) -> None:
        context = self.make_expand_context()
        params = {
            "targetState": "展开",
            "collapsedText": "更多",
            "expandedText": "收起",
        }
        with (
            patch("rpa_modules.visual_window_signature", return_value={}),
            patch("rpa_modules.persisted_page_candidates", return_value=[{}]),
            patch(
                "rpa_modules.visual_toggle_anchor",
                return_value=([60, 51], [40, 40, 80, 62]),
            ),
            patch(
                "rpa_modules.capture_visual_window",
                return_value=Image.new("RGB", (200, 120), "white"),
            ),
            patch("rpa_modules.local_visual_ocr", return_value=[]),
            patch("rpa_modules.mouse.click") as click,
        ):
            with self.assertRaises(WorkflowExecutionError):
                run_visual_set_expand_state(context, params)

        click.assert_not_called()

    def test_duplicate_text_prefers_previous_operation_row_over_ocr_score(self) -> None:
        upper = SimpleNamespace(
            text="查询", score=0.91, left=1700, top=225, right=1760, bottom=258
        )
        lower = SimpleNamespace(
            text="查询", score=0.99, left=1840, top=398, right=1905, bottom=432
        )

        selected = choose_visual_text_match([lower, upper], (300, 242))

        self.assertIs(selected, upper)

    def test_table_exclusion_applies_to_fields_but_not_checkboxes(self) -> None:
        image = Image.new("RGB", (420, 220), "white")
        page_map = {
            "signature": {"dpiScale": 1.0},
            "ocrItems": [
                OcrItem("姓名", 0.98, 50, 78, 92, 98),
                OcrItem("出院患者", 0.98, 100, 126, 180, 147),
            ],
            "weakOcrItems": [],
            "visualImage": image,
            "targets": {},
            "targetDetails": {},
            "regions": [],
            "relationships": [],
        }
        inventory = {
            "fieldRectangles": [
                {
                    "bounds": [105, 72, 300, 104],
                    "center": [202, 88],
                    "score": 90.0,
                }
            ],
            "squareControls": [
                {
                    "bounds": [76, 126, 94, 144],
                    "center": [85, 135],
                    "score": 90.0,
                }
            ],
            "weakFieldRectangles": [],
            "weakSquareControls": [],
        }
        table = {
            "kind": "table",
            "bounds": [20, 50, 390, 190],
            "columnLines": [20, 100, 200, 300, 390],
            "rowLines": [50, 110, 150, 190],
            "confidence": 1.0,
        }

        with (
            patch("rpa_modules.detect_foreground_panel", return_value=None),
            patch("rpa_modules.detect_table_regions", return_value=[table]),
            patch(
                "rpa_modules.detect_visual_control_candidates",
                return_value=inventory,
            ),
        ):
            classify_visual_page(page_map)

        self.assertFalse(
            any(
                element.get("kind") == "field-rectangle"
                for element in page_map["elements"]
            )
        )
        self.assertTrue(
            any(
                element.get("kind") == "checkbox"
                and element.get("labelText") == "出院患者"
                for element in page_map["elements"]
            )
        )

    def test_one_field_is_not_assigned_to_two_nearby_labels(self) -> None:
        image = Image.new("RGB", (420, 150), (238, 242, 246))
        ImageDraw.Draw(image).rectangle(
            (120, 40, 320, 76),
            fill="white",
            outline=(130, 165, 195),
            width=2,
        )
        page_map = {
            "signature": {"dpiScale": 1.0},
            "ocrItems": [
                OcrItem("姓名", 0.98, 50, 42, 100, 59),
                OcrItem("别名", 0.98, 50, 58, 100, 75),
            ],
            "weakOcrItems": [],
            "visualImage": image,
            "targets": {},
            "targetDetails": {},
            "regions": [],
            "relationships": [],
        }

        classify_visual_page(page_map)

        associated_fields = [
            element
            for element in page_map["elements"]
            if element.get("kind") == "field-rectangle"
        ]
        self.assertEqual(len(associated_fields), 1)
        self.assertEqual(
            page_map["performance"]["associationStrategy"],
            "global-one-to-one",
        )


if __name__ == "__main__":
    unittest.main()
