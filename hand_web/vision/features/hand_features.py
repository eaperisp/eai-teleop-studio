"""Extract anatomical hand features in a palm-local coordinate frame."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from hand_web.vision.features.palm_frame import PalmFrame, Vec3, dot, norm, subtract, unit


FEATURE_NAMES = ("thumb_flex", "thumb_aux", "index", "middle", "ring", "pinky")
FINGER_CHAINS = {
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}


def _angle(a: Vec3, b: Vec3, c: Vec3) -> float:
    first = subtract(a, b)
    second = subtract(c, b)
    first_norm = norm(first)
    second_norm = norm(second)
    if first_norm < 1e-8 or second_norm < 1e-8:
        return 180.0
    cosine = dot(first, second) / (first_norm * second_norm)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _bend(a: Vec3, b: Vec3, c: Vec3) -> float:
    return max(0.0, min(180.0, 180.0 - _angle(a, b, c)))


@dataclass(frozen=True)
class HandFeatures:
    thumb_flex: float
    thumb_aux: float
    index: float
    middle: float
    ring: float
    pinky: float

    def to_dict(self) -> dict[str, float]:
        return {key: round(float(value), 4) for key, value in asdict(self).items()}

    def values(self) -> list[float]:
        return [float(getattr(self, name)) for name in FEATURE_NAMES]


class HandFeatureExtractor:
    def extract(self, landmarks: Any) -> HandFeatures:
        frame = PalmFrame.from_landmarks(landmarks)
        points = frame.transform(landmarks)

        thumb_proximal = subtract(points[3], points[2])
        try:
            thumb_direction = unit(thumb_proximal)
        except ValueError:
            thumb_direction = (0.0, 1.0, 0.0)
        thumb_elevation = math.degrees(math.asin(max(-1.0, min(1.0, abs(thumb_direction[2])))))
        thumb_flex = (
            thumb_elevation * 0.30
            + _bend(points[1], points[2], points[3]) * 0.35
            + _bend(points[2], points[3], points[4]) * 0.55
        )

        # The CMC-to-tip ray follows thumb abduction. Wrist-to-MCP barely moves
        # during this gesture and can leave the auxiliary joint stuck at zero.
        thumb_vector = subtract(points[4], points[1])
        thumb_aux = math.degrees(math.atan2(thumb_vector[0], max(abs(thumb_vector[1]), 1e-8)))

        curls = {
            name: self._finger_curl(points, chain)
            for name, chain in FINGER_CHAINS.items()
        }
        return HandFeatures(thumb_flex, thumb_aux, **curls)

    @staticmethod
    def _finger_curl(points: list[Vec3], chain: tuple[int, int, int, int]) -> float:
        mcp, pip, dip, tip = (points[index] for index in chain)
        try:
            proximal_direction = unit(subtract(pip, mcp))
        except ValueError:
            proximal_direction = (0.0, 1.0, 0.0)
        # Only motion away from the palm plane is flexion. Measuring against
        # the middle-finger axis also counted a naturally spread index/pinky.
        mcp_flex = math.degrees(math.asin(max(-1.0, min(1.0, abs(proximal_direction[2])))))
        pip_bend = _bend(mcp, pip, dip)
        dip_bend = _bend(pip, dip, tip)
        # Depth noise affects the MCP direction most when a hand is viewed at
        # an angle. PIP/DIP bends are stronger evidence of an actual curl.
        return max(0.0, min(150.0, mcp_flex * 0.15 + pip_bend * 0.50 + dip_bend * 0.35))


__all__ = ["FEATURE_NAMES", "FINGER_CHAINS", "HandFeatureExtractor", "HandFeatures"]
