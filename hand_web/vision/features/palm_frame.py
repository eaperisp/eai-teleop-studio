"""Scale and camera-pose invariant palm coordinate frame."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


Vec3 = tuple[float, float, float]


def point3(point: Any) -> Vec3:
    return float(point.x), float(point.y), float(getattr(point, "z", 0.0))


def add(a: Vec3, b: Vec3) -> Vec3:
    return tuple(x + y for x, y in zip(a, b))  # type: ignore[return-value]


def subtract(a: Vec3, b: Vec3) -> Vec3:
    return tuple(x - y for x, y in zip(a, b))  # type: ignore[return-value]


def scale(vector: Vec3, value: float) -> Vec3:
    return tuple(component * value for component in vector)  # type: ignore[return-value]


def dot(a: Vec3, b: Vec3) -> float:
    return sum(x * y for x, y in zip(a, b))


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(vector: Vec3) -> float:
    return math.sqrt(dot(vector, vector))


def unit(vector: Vec3) -> Vec3:
    length = norm(vector)
    if length < 1e-8:
        raise ValueError("无法建立手掌坐标系：关键点重合")
    return scale(vector, 1.0 / length)


@dataclass(frozen=True)
class PalmFrame:
    origin: Vec3
    x_axis: Vec3
    y_axis: Vec3
    z_axis: Vec3
    palm_width: float

    @classmethod
    def from_landmarks(cls, landmarks: Any) -> "PalmFrame":
        if len(landmarks) < 21:
            raise ValueError("手部关键点数量不足")
        wrist = point3(landmarks[0])
        index_mcp = point3(landmarks[5])
        middle_mcp = point3(landmarks[9])
        pinky_mcp = point3(landmarks[17])
        across = subtract(index_mcp, pinky_mcp)
        x_axis = unit(across)
        forward = subtract(middle_mcp, wrist)
        forward = subtract(forward, scale(x_axis, dot(forward, x_axis)))
        y_axis = unit(forward)
        z_axis = unit(cross(x_axis, y_axis))
        return cls(wrist, x_axis, y_axis, z_axis, max(norm(across), 1e-8))

    def local(self, point: Any) -> Vec3:
        relative = subtract(point3(point), self.origin)
        inverse_scale = 1.0 / self.palm_width
        return (
            dot(relative, self.x_axis) * inverse_scale,
            dot(relative, self.y_axis) * inverse_scale,
            dot(relative, self.z_axis) * inverse_scale,
        )

    def transform(self, landmarks: Any) -> list[Vec3]:
        return [self.local(point) for point in landmarks]


__all__ = [
    "PalmFrame", "Vec3", "add", "cross", "dot", "norm", "point3", "scale", "subtract", "unit",
]
