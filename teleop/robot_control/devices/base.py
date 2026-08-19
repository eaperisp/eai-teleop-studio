"""Common models used by dexterous-hand device adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal


HandSide = Literal["left", "right"]
HAND_SIDES: tuple[HandSide, ...] = ("left", "right")


@dataclass(frozen=True)
class JointSpec:
    id: str
    name: str
    english_name: str
    group: str = "finger"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def validate_side(side: str) -> HandSide:
    if side not in HAND_SIDES:
        raise ValueError(f"side must be one of {HAND_SIDES}, got {side!r}")
    return side  # type: ignore[return-value]


def normalize_positions(values: Iterable[int | float], count: int) -> list[float]:
    positions = list(values)
    if len(positions) != count:
        raise ValueError(f"expected {count} positions, got {len(positions)}")

    normalized: list[float] = []
    for index, value in enumerate(positions):
        if isinstance(value, bool):
            raise ValueError(f"position {index} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"position {index} must be a number") from exc
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"position {index} must be between 0.0 and 1.0")
        normalized.append(number)
    return normalized


def public_error(exc: Exception) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": str(exc)}


__all__ = [
    "HAND_SIDES",
    "HandSide",
    "JointSpec",
    "normalize_positions",
    "public_error",
    "validate_side",
]
