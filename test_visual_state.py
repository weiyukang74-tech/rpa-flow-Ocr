"""Offline tests for fast visual page-state selection."""

from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from visual_state import (
    changed_region,
    descriptor_distance,
    is_interactive_overlay_change,
    is_local_change,
    structure_descriptor,
)


class VisualStateTests(unittest.TestCase):
    def test_same_layout_has_zero_distance(self) -> None:
        image = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(image).rectangle((20, 40, 580, 90), outline="navy", width=3)
        descriptor = structure_descriptor(image)
        self.assertEqual(descriptor_distance(descriptor, descriptor), 0)

    def test_large_layout_change_is_farther_than_small_data_change(self) -> None:
        base = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(base).rectangle((20, 40, 580, 90), outline="navy", width=3)
        small = base.copy()
        ImageDraw.Draw(small).text((80, 160), "P810274", fill="black")
        large = base.copy()
        draw = ImageDraw.Draw(large)
        for y in range(130, 350, 45):
            draw.rectangle((20, y, 580, y + 30), outline="black", width=2)
        base_descriptor = structure_descriptor(base)
        self.assertLess(
            descriptor_distance(base_descriptor, structure_descriptor(small)),
            descriptor_distance(base_descriptor, structure_descriptor(large)),
        )

    def test_popup_change_is_local(self) -> None:
        before = Image.new("RGB", (600, 400), "white")
        after = before.copy()
        ImageDraw.Draw(after).rectangle((350, 80, 540, 260), fill="#eeeeee", outline="black")
        change = changed_region(before, after)
        self.assertIsNotNone(change)
        self.assertTrue(is_local_change(change))
        self.assertTrue(is_interactive_overlay_change(change))

    def test_input_text_does_not_change_control_structure(self) -> None:
        before = Image.new("RGB", (600, 400), "white")
        draw = ImageDraw.Draw(before)
        draw.rectangle((80, 70, 300, 110), outline="#8ab6d6", width=2)
        after = before.copy()
        ImageDraw.Draw(after).text((95, 82), "P810274", fill="black")
        distance = descriptor_distance(
            structure_descriptor(before),
            structure_descriptor(after),
        )
        self.assertLessEqual(distance, 0.02)

    def test_moving_a_major_panel_changes_control_structure(self) -> None:
        before = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(before).rectangle((40, 50, 560, 140), outline="#8ab6d6", width=3)
        after = Image.new("RGB", (600, 400), "white")
        ImageDraw.Draw(after).rectangle((40, 220, 560, 310), outline="#8ab6d6", width=3)
        distance = descriptor_distance(
            structure_descriptor(before),
            structure_descriptor(after),
        )
        self.assertGreater(distance, 0.01)

    def test_typed_text_is_not_an_interactive_overlay(self) -> None:
        before = Image.new("RGB", (600, 400), "white")
        after = before.copy()
        ImageDraw.Draw(after).text((80, 160), "P810274", fill="black")
        change = changed_region(before, after)
        self.assertIsNotNone(change)
        self.assertFalse(is_interactive_overlay_change(change))

    def test_wide_form_refresh_is_not_an_interactive_overlay(self) -> None:
        before = Image.new("RGB", (1000, 600), "white")
        after = before.copy()
        draw = ImageDraw.Draw(after)
        draw.rectangle((40, 100, 960, 180), outline="#8ab6d6", width=2)
        for x in range(70, 930, 110):
            draw.text((x, 130), "updated", fill="black")
        change = changed_region(before, after)
        self.assertIsNotNone(change)
        assert change is not None
        self.assertGreater(change["widthRatio"], 0.72)
        self.assertFalse(is_interactive_overlay_change(change))


if __name__ == "__main__":
    unittest.main()
