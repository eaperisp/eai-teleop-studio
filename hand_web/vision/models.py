"""Small dependency-free data types shared by vision components."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandDetection:
    landmarks: Any
    side: str
    gesture: str | None = None
    render_data: Any = None
    world_landmarks: Any = None


class VisionDependencyError(RuntimeError):
    pass


__all__ = ["HandDetection", "VisionDependencyError"]
