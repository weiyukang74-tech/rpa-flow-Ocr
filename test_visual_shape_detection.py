"""Offline regression tests for generic OCR-adjacent shape detection."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from PIL import Image, ImageDraw

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


class VisualShapeDetectionTests(unittest.TestCase):
    @staticmethod
    def checkbox_image(center: tuple[int, int]) -> Image.Image:
        image = Image.new("RGB", (360, 180), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        x, y = center
        draw.rectangle((x - 10, y - 10, x + 10, y + 10), outline=(65, 95, 120), width=2)
        return image

    def test_finds_checkbox_left_of_label_on_either_pixel_parity(self) -> None:
        label = SimpleNamespace(left=145, top=72, right=255, bottom=89)
        result = find_checkbox_square(self.checkbox_image((113, 81)), label)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["relation"], "left")
        self.assertLessEqual(abs(result["center"][0] - 113), 2)
        self.assertLessEqual(abs(result["center"][1] - 81), 2)

    def test_finds_checkbox_right_of_label(self) -> None:
        label = SimpleNamespace(left=55, top=72, right=165, bottom=89)
        result = find_checkbox_square(self.checkbox_image((199, 82)), label)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["relation"], "right")
        self.assertLessEqual(abs(result["center"][0] - 199), 2)
        self.assertLessEqual(abs(result["center"][1] - 82), 2)

    def test_uses_ocr_checkbox_prefix_for_filled_checkbox(self) -> None:
        image = Image.new("RGB", (420, 180), (245, 245, 245))
        label = SimpleNamespace(
            left=211,
            top=67,
            right=293,
            bottom=88,
            raw_text="✅出院患者",
        )
        result = find_checkbox_square(image, label)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["relation"], "ocr-prefix")
        self.assertLessEqual(result["center"][0], label.left + 12)
        self.assertEqual(result["center"][1], 77)
        self.assertTrue(
            checkbox_bounds_match_label(
                result["bounds"],
                [label.left, label.top, label.right, label.bottom],
                result["relation"],
            )
        )

    def test_prefers_checkbox_attached_to_label_over_distant_select_edge(self) -> None:
        image = Image.new("RGB", (420, 180), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle((218, 71, 234, 87), outline=(65, 95, 120), width=2)
        draw.rectangle((40, 55, 190, 91), outline=(25, 90, 170), width=3)
        label = SimpleNamespace(left=241, top=70, right=345, bottom=89)
        result = find_checkbox_square(image, label)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertLessEqual(abs(result["center"][0] - 226), 2)

    def test_rejects_distant_square_as_checkbox_for_label(self) -> None:
        self.assertFalse(
            checkbox_bounds_match_label(
                [982, 270, 998, 286],
                [1111, 255, 1176, 277],
                "left",
            )
        )

    def test_finds_input_rectangle_below_label(self) -> None:
        image = Image.new("RGB", (360, 180), (225, 225, 225))
        ImageDraw.Draw(image).rectangle(
            (40, 58, 240, 94),
            fill=(255, 255, 255),
            outline=(165, 180, 195),
            width=1,
        )
        label = SimpleNamespace(left=40, top=30, right=92, bottom=46)
        result = find_input_rectangle(image, label)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["relation"], "below")
        self.assertTrue(40 < result["center"][0] < 240)
        self.assertTrue(58 < result["center"][1] < 94)

    def test_prefers_inline_input_right_of_label(self) -> None:
        image = Image.new("RGB", (420, 180), (225, 225, 225))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (128, 42, 300, 75),
            fill=(255, 255, 255),
            outline=(165, 180, 195),
            width=1,
        )
        # A nearer light area below the text must not beat the inline field.
        draw.rectangle(
            (42, 67, 118, 92),
            fill=(255, 255, 255),
            outline=(165, 180, 195),
            width=1,
        )
        label = SimpleNamespace(left=48, top=45, right=118, bottom=70)
        result = find_input_rectangle(image, label)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["relation"], "right")
        self.assertGreaterEqual(result["bounds"][0], label.right)

    def test_rejects_cached_rectangle_overlapping_label(self) -> None:
        self.assertFalse(
            input_bounds_match_label(
                [441, 214, 518, 238],
                [453, 197, 518, 219],
                "below",
            )
        )

    def test_detects_grid_table_without_treating_short_fields_as_columns(self) -> None:
        image = Image.new("RGB", (500, 300), "white")
        draw = ImageDraw.Draw(image)
        for x in (30, 120, 220, 330, 470):
            draw.line((x, 120, x, 250), fill=(160, 170, 180), width=2)
        for y in (120, 155, 190, 220, 250):
            draw.line((30, y, 470, y), fill=(160, 170, 180), width=2)
        draw.rectangle((40, 35, 180, 65), outline=(160, 170, 180), width=2)
        tables = detect_table_regions(image)
        self.assertEqual(len(tables), 1)
        left, top, right, bottom = tables[0]["bounds"]
        self.assertLessEqual(left, 31)
        self.assertLessEqual(top, 122)
        self.assertGreaterEqual(right, 469)
        self.assertGreaterEqual(bottom, 249)

    def test_detects_control_candidates_once_and_associates_labels(self) -> None:
        image = Image.new("RGB", (460, 190), (238, 242, 246))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (130, 38, 350, 76),
            fill="white",
            outline=(130, 165, 195),
            width=2,
        )
        draw.rectangle((327, 49, 339, 62), outline=(30, 55, 70), width=2)
        draw.rectangle((42, 113, 62, 133), fill="white", outline=(65, 95, 120), width=2)

        inventory = detect_visual_control_candidates(image)
        field_label = SimpleNamespace(left=48, top=46, right=118, bottom=68)
        checkbox_label = SimpleNamespace(
            left=70,
            top=113,
            right=180,
            bottom=134,
            raw_text="出院患者",
        )
        field = match_input_candidate(inventory["fieldRectangles"], field_label)
        checkbox = match_checkbox_candidate(inventory["squareControls"], checkbox_label)

        self.assertIsNotNone(field)
        self.assertIsNotNone(checkbox)
        assert field is not None and checkbox is not None
        self.assertEqual(field["relation"], "right")
        self.assertLessEqual(abs(field["center"][0] - 240), 3)
        self.assertEqual(checkbox["relation"], "left")
        self.assertLessEqual(abs(checkbox["center"][0] - 52), 3)
        self.assertTrue(
            all(
                not (130 <= candidate["center"][0] <= 350 and 38 <= candidate["center"][1] <= 76)
                for candidate in inventory["squareControls"]
            )
        )

    def test_detects_bright_dialog_over_dimmed_background(self) -> None:
        image = Image.new("RGB", (1000, 600), (174, 180, 184))
        draw = ImageDraw.Draw(image)
        draw.rectangle((145, 95, 855, 530), fill="white", outline="#607d8b", width=2)
        draw.rectangle((145, 95, 855, 140), fill="#1688bc")
        draw.rectangle((700, 165, 810, 200), fill="#1688bc")
        panel = detect_foreground_panel(image)
        self.assertIsNotNone(panel)
        assert panel is not None
        left, top, right, bottom = panel["bounds"]
        self.assertTrue(left < 755 < right)
        self.assertTrue(top < 182 < bottom)
        self.assertFalse(left <= 50 <= right)

    def test_full_bright_page_is_not_a_foreground_dialog(self) -> None:
        image = Image.new("RGB", (1000, 600), "white")
        ImageDraw.Draw(image).rectangle((20, 40, 980, 100), outline="#1688bc", width=2)
        self.assertIsNone(detect_foreground_panel(image))


if __name__ == "__main__":
    unittest.main()
