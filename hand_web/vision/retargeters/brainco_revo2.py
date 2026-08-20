"""Constraint-aware retargeting from human hand features to Revo2 joints."""

from __future__ import annotations

import time
from typing import Any

from hand_web.vision.features import FEATURE_NAMES, HandFeatureExtractor, HandFeatures
from hand_web.vision.filters import OneEuroVectorFilter


DEFAULT_RANGES = {
    "thumb_flex": [5.0, 75.0],
    "thumb_aux": [65.0, 15.0],
    "index": [8.0, 48.0],
    "middle": [8.0, 60.0],
    "ring": [8.0, 78.0],
    "pinky": [8.0, 75.0],
}


class BraincoRevo2Retargeter:
    def __init__(self, config: dict[str, Any], profile: dict[str, Any] | None = None) -> None:
        limits = config.get("joint_limits") or [[0.0, 1.0]] * 6
        if not isinstance(limits, list) or len(limits) != 6:
            raise ValueError("vision.joint_limits 必须包含六个关节范围")
        self.limits = [(float(item[0]), float(item[1])) for item in limits]
        self.deadband = max(0.0, float(config.get("deadband", 0.005)))
        self.max_velocity = max(0.1, float(config.get("max_velocity", 3.0)))
        self.endpoint_snap = max(0.0, min(0.15, float(config.get("endpoint_snap", 0.025))))
        self.thumb_flex_aux_coupling = max(
            0.0, min(1.0, float(config.get("thumb_flex_aux_coupling", 0.9)))
        )
        self.extractor = HandFeatureExtractor()
        self.filter = OneEuroVectorFilter(
            6,
            min_cutoff=float(config.get("filter_min_cutoff", 1.2)),
            beta=float(config.get("filter_beta", 0.08)),
            derivative_cutoff=float(config.get("filter_derivative_cutoff", 1.0)),
        )
        self._ranges: dict[str, list[float]] = {}
        self._last_output: list[float] | None = None
        self._last_time: float | None = None
        self.last_features: HandFeatures | None = None
        self.last_unfiltered: list[float] | None = None
        self.set_profile(profile)

    def set_profile(self, profile: dict[str, Any] | None) -> None:
        configured = (profile or {}).get("ranges", {})
        self._ranges = {
            name: self._range(configured.get(name), DEFAULT_RANGES[name])
            for name in FEATURE_NAMES
        }
        self.reset()

    @staticmethod
    def _range(value: Any, default: list[float]) -> list[float]:
        if not isinstance(value, list) or len(value) != 2:
            return default[:]
        low, high = float(value[0]), float(value[1])
        return [low, high] if abs(high - low) >= 1e-6 else default[:]

    def map(self, landmarks: Any, gesture: str | None = None, timestamp: float | None = None) -> list[float]:
        timestamp = time.monotonic() if timestamp is None else float(timestamp)
        features = self.extractor.extract(landmarks)
        self.last_features = features
        raw = [self._normalize(name, getattr(features, name)) for name in FEATURE_NAMES]
        # Revo2 needs both active thumb joints when a human thumb moves inward
        # against the palm, even if the visible IP segment is straight.
        raw[0] = max(raw[0], raw[1] * self.thumb_flex_aux_coupling)
        if gesture == "Open Hand":
            # MediaPipe's label describes the four long fingers. The thumb can
            # still adduct or flex while that label remains unchanged.
            raw[2:] = [0.0] * 4
        self.last_unfiltered = raw[:]
        filtered = self.filter.filter(raw, timestamp)
        constrained = self._constrain_velocity(filtered, timestamp)
        output = []
        for value, (low, high) in zip(constrained, self.limits):
            value = max(0.0, min(1.0, value))
            if value <= self.endpoint_snap:
                value = 0.0
            elif value >= 1.0 - self.endpoint_snap:
                value = 1.0
            output.append(max(low, min(high, value)))
        if self._last_output is not None:
            output = [old if abs(new - old) < self.deadband else new for new, old in zip(output, self._last_output)]
        self._last_output = output[:]
        self._last_time = timestamp
        return [round(value, 4) for value in output]

    def _normalize(self, name: str, value: float) -> float:
        opened, closed = self._ranges[name]
        return max(0.0, min(1.0, (float(value) - opened) / (closed - opened)))

    def _constrain_velocity(self, values: list[float], timestamp: float) -> list[float]:
        if self._last_output is None or self._last_time is None or timestamp <= self._last_time:
            return values
        maximum_delta = self.max_velocity * min(timestamp - self._last_time, 0.25)
        return [
            max(old - maximum_delta, min(old + maximum_delta, value))
            for value, old in zip(values, self._last_output)
        ]

    def reset(self) -> None:
        self.filter.reset()
        self._last_output = None
        self._last_time = None
        self.last_features = None
        self.last_unfiltered = None


__all__ = ["BraincoRevo2Retargeter", "DEFAULT_RANGES"]
