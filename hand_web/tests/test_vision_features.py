from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from hand_web.vision.calibration import VisionCalibrationStore
from hand_web.vision.features import HandFeatureExtractor
from hand_web.vision.filters import OneEuroFilter


class Point:
    def __init__(self, x: float, y: float, z: float = 0.0) -> None:
        self.x, self.y, self.z = x, y, z


def open_hand() -> list[Point]:
    points = [Point(0, 0, 0) for _ in range(21)]
    points[0] = Point(0.0, 0.0, 0.0)
    points[1], points[2], points[3], points[4] = (
        Point(0.22, 0.18, 0.0), Point(0.45, 0.35, 0.0),
        Point(0.66, 0.50, 0.0), Point(0.86, 0.64, 0.0),
    )
    for chain, x, length in (
        ((5, 6, 7, 8), 0.36, 1.62),
        ((9, 10, 11, 12), 0.0, 1.72),
        ((13, 14, 15, 16), -0.28, 1.58),
        ((17, 18, 19, 20), -0.50, 1.40),
    ):
        base = 0.68 if chain[0] != 17 else 0.58
        for index, fraction in zip(chain, (0.0, 0.38, 0.68, 1.0)):
            points[index] = Point(x, base + (length - base) * fraction, 0.0)
    return points


def fist() -> list[Point]:
    points = open_hand()
    for chain in ((5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20)):
        mcp = points[chain[0]]
        points[chain[1]] = Point(mcp.x, mcp.y + 0.18, 0.28)
        points[chain[2]] = Point(mcp.x, mcp.y + 0.02, 0.46)
        points[chain[3]] = Point(mcp.x, mcp.y - 0.15, 0.28)
    points[1] = Point(0.22, 0.18, 0.0)
    points[2] = Point(0.18, 0.40, 0.20)
    points[3] = Point(0.02, 0.43, 0.34)
    points[4] = Point(-0.10, 0.34, 0.22)
    return points


def transform(points: list[Point], scale: float, angle: float) -> list[Point]:
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        Point(
            1.3 + scale * (point.x * cosine - point.y * sine),
            -0.7 + scale * (point.x * sine + point.y * cosine),
            0.4 + scale * point.z,
        )
        for point in points
    ]


class VisionFeatureTests(unittest.TestCase):
    def test_features_are_invariant_to_translation_rotation_and_scale(self):
        extractor = HandFeatureExtractor()
        original = extractor.extract(fist()).values()
        converted = extractor.extract(transform(fist(), 2.7, 0.9)).values()
        for first, second in zip(original, converted):
            self.assertAlmostEqual(first, second, places=5)

    def test_closed_fingers_have_more_curl(self):
        extractor = HandFeatureExtractor()
        opened = extractor.extract(open_hand())
        closed = extractor.extract(fist())
        for name in ("index", "middle", "ring", "pinky"):
            self.assertGreater(getattr(closed, name), getattr(opened, name) + 40.0)

    def test_lateral_finger_spread_is_not_counted_as_flexion(self):
        points = open_hand()
        base = points[5]
        for index, distance in zip((6, 7, 8), (0.35, 0.65, 0.95)):
            points[index] = Point(base.x + distance * 0.55, base.y + distance, 0.0)
        self.assertLess(HandFeatureExtractor().extract(points).index, 1.0)

    def test_one_euro_filter_reduces_stationary_jitter(self):
        filter_ = OneEuroFilter(min_cutoff=0.6, beta=0.05)
        raw = [0.5 + (0.03 if index % 2 else -0.03) for index in range(40)]
        filtered = [filter_.filter(value, index / 30.0) for index, value in enumerate(raw)]
        raw_span = max(raw[-10:]) - min(raw[-10:])
        filtered_span = max(filtered[-10:]) - min(filtered[-10:])
        self.assertLess(filtered_span, raw_span * 0.25)

    def test_calibration_builds_descending_thumb_aux_range(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VisionCalibrationStore(Path(directory) / "calibration.json")
            captures = {
                "open": {name: 5.0 for name in ("thumb_flex", "thumb_aux", "index", "middle", "ring", "pinky")},
                "fist": {name: 80.0 for name in ("thumb_flex", "thumb_aux", "index", "middle", "ring", "pinky")},
                "thumb_abducted": {name: 50.0 for name in ("thumb_flex", "thumb_aux", "index", "middle", "ring", "pinky")},
                "thumb_adducted": {name: 10.0 for name in ("thumb_flex", "thumb_aux", "index", "middle", "ring", "pinky")},
            }
            profile = store.save("brainco_revo2", "right", captures)
            self.assertEqual(profile["ranges"]["thumb_aux"], [50.0, 10.0])
            self.assertEqual(store.profile("brainco_revo2", "right")["ranges"]["index"], [5.0, 80.0])


if __name__ == "__main__":
    unittest.main()
