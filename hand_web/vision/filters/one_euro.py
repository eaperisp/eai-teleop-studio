"""Dependency-free One Euro filter for low-lag hand tracking."""

from __future__ import annotations

import math


class LowPassFilter:
    def __init__(self) -> None:
        self.value: float | None = None

    def filter(self, value: float, alpha: float) -> float:
        self.value = value if self.value is None else alpha * value + (1.0 - alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None


class OneEuroFilter:
    def __init__(self, min_cutoff: float = 1.2, beta: float = 0.08, derivative_cutoff: float = 1.0) -> None:
        self.min_cutoff = max(0.01, float(min_cutoff))
        self.beta = max(0.0, float(beta))
        self.derivative_cutoff = max(0.01, float(derivative_cutoff))
        self._signal = LowPassFilter()
        self._derivative = LowPassFilter()
        self._last_raw: float | None = None
        self._last_time: float | None = None

    @staticmethod
    def _alpha(delta_time: float, cutoff: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / max(delta_time, 1e-6))

    def filter(self, value: float, timestamp: float) -> float:
        if self._last_time is None or timestamp <= self._last_time:
            derivative = 0.0
            delta_time = 1.0 / 30.0
        else:
            delta_time = timestamp - self._last_time
            derivative = (value - float(self._last_raw)) / delta_time
        filtered_derivative = self._derivative.filter(
            derivative,
            self._alpha(delta_time, self.derivative_cutoff),
        )
        cutoff = self.min_cutoff + self.beta * abs(filtered_derivative)
        result = self._signal.filter(value, self._alpha(delta_time, cutoff))
        self._last_raw = value
        self._last_time = timestamp
        return result

    def reset(self) -> None:
        self._signal.reset()
        self._derivative.reset()
        self._last_raw = None
        self._last_time = None


class OneEuroVectorFilter:
    def __init__(self, size: int, **options: float) -> None:
        self.filters = [OneEuroFilter(**options) for _ in range(size)]

    def filter(self, values: list[float], timestamp: float) -> list[float]:
        if len(values) != len(self.filters):
            raise ValueError("滤波数据维度不正确")
        return [item.filter(value, timestamp) for item, value in zip(self.filters, values)]

    def reset(self) -> None:
        for item in self.filters:
            item.reset()


__all__ = ["OneEuroFilter", "OneEuroVectorFilter"]
