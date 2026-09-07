"""Offline regression tests for generic OCR-adjacent shape detection."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from PIL import Image, ImageDraw

from visual_shape_detection import find_checkbox_square, find_input_rectangle


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


if __name__ == "__main__":
    unittest.main()
