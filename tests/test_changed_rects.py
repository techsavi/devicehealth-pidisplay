"""Checks for display.changed_rects, which decides what each redraw sends to the panel.

Run from pi_display with:
    python3 -m unittest discover -s tests -v
"""
import unittest

import numpy as np

from wifi_meter.display import changed_rects


def frame():
    return np.zeros((320, 480), "<u2")


class ChangedRectsTests(unittest.TestCase):
    def test_identical_frames_send_nothing(self):
        self.assertEqual(changed_rects(frame(), frame()), [])

    def test_one_change_is_trimmed_to_its_columns(self):
        new = frame()
        new[100:118, 380:426] = 0xFFFF   # a "64%" -> "66%" percentage
        self.assertEqual(changed_rects(frame(), new), [(380, 100, 426, 118)])

    def test_distant_changes_are_separate_rectangles(self):
        new = frame()
        new[8:24, 300:470] = 1      # the header clock
        new[290:300, 200:300] = 1   # a bar near the bottom
        self.assertEqual(changed_rects(frame(), new), [(300, 8, 470, 24), (200, 290, 300, 300)])

    def test_nearby_rows_share_one_band(self):
        new = frame()
        new[50:52, 10:20] = 1
        new[56:58, 30:40] = 1       # 4 rows apart: within the default gap of 8
        self.assertEqual(changed_rects(frame(), new), [(10, 50, 40, 58)])

    def test_every_changed_pixel_is_covered(self):
        rng = np.random.default_rng(1)
        old, new = frame(), frame()
        ys, xs = rng.integers(0, 320, 40), rng.integers(0, 480, 40)
        new[ys, xs] = 7
        covered = np.zeros_like(new, bool)
        for x0, y0, x1, y1 in changed_rects(old, new):
            covered[y0:y1, x0:x1] = True
        self.assertTrue(covered[new != old].all())


if __name__ == "__main__":
    unittest.main()
