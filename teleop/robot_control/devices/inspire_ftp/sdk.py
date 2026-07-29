"""High-level SDK for the Inspire FTP dexterous hand DDS adapter.

This layer is intentionally small: it hides Unitree DDS setup, topic names,
command scaling, and basic safety clamping so tools and future teleoperation
code can drive the hand without duplicating message plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable, Literal

from . import defaults
from . import messages


HandSide = Literal["left", "right"]

LEFT_CMD_TOPIC = "rt/inspire_hand/ctrl/l"
RIGHT_CMD_TOPIC = "rt/inspire_hand/ctrl/r"
LEFT_STATE_TOPIC = "rt/inspire_hand/state/l"
RIGHT_STATE_TOPIC = "rt/inspire_hand/state/r"

HAND_SIDES: tuple[HandSide, ...] = ("left", "right")

OPEN_ANGLES = [1000, 1000, 1000, 1000, 1000, 1000]
CLOSE_ANGLES = [0, 0, 0, 0, 0, 0]
PINCH_ANGLES = [1000, 1000, 1000, 250, 250, 550]


@dataclass(frozen=True)
class InspireHandState:
    """One sampled hand state.

    ``angles`` are raw Inspire FTP hand units, in the inclusive range 0-1000.
    The current adapter treats 1000 as open and 0 as closed.
    """

    side: HandSide
    angles: tuple[int, int, int, int, int, int]

    @property
    def normalized(self) -> tuple[float, float, float, float, float, float]:
        return tuple(angle / messages.ANGLE_MAX for angle in self.angles)


class InspireHandSDK:
    """Control and inspect Inspire FTP dexterous hands over Unitree DDS."""

    def __init__(
        self,
        network_interface: str | None = None,
        domain: int = 0,
        enable_left: bool = True,
        enable_right: bool = True,
    ) -> None:
        self.network_interface = network_interface
        self.domain = domain
        self.enabled_sides = {
            "left": enable_left,
            "right": enable_right,
        }
        self._publishers = {}
        self._subscribers = {}
        self._initialized = False

    def initialize(self) -> None:
        """Initialize DDS channels for enabled hands."""

        if self._initialized:
            return

        from unitree_sdk2py.core.channel import (  # pylint: disable=import-outside-toplevel
            ChannelFactoryInitialize,
            ChannelPublisher,
            ChannelSubscriber,
        )

        if self.network_interface:
            ChannelFactoryInitialize(self.domain, networkInterface=self.network_interface)
        else:
            ChannelFactoryInitialize(self.domain)

        topic_map = {
            "left": (LEFT_CMD_TOPIC, LEFT_STATE_TOPIC),
            "right": (RIGHT_CMD_TOPIC, RIGHT_STATE_TOPIC),
        }
        for side, enabled in self.enabled_sides.items():
            if not enabled:
                continue
            cmd_topic, state_topic = topic_map[side]
            publisher = ChannelPublisher(cmd_topic, messages.inspire_hand_ctrl)
            publisher.Init()
            subscriber = ChannelSubscriber(state_topic, messages.inspire_hand_state)
            subscriber.Init()
            self._publishers[side] = publisher
            self._subscribers[side] = subscriber

        self._initialized = True

    def read_state(self, side: HandSide, timeout: float = 0.02) -> InspireHandState | None:
        """Read one state sample for ``side``.

        Returns ``None`` if no DDS sample is available before ``timeout``.
        """

        self._require_side(side)
        subscriber = self._subscribers[side]
        try:
            state = subscriber.Read(timeout)
        except TypeError:
            state = subscriber.Read()
        if state is None:
            return None
        return InspireHandState(side=side, angles=tuple(self._coerce_angles(state.angle_act)))

    def read_states(self, timeout: float = 0.02) -> dict[HandSide, InspireHandState | None]:
        """Read one state sample from every enabled side."""

        result = {}
        for side in HAND_SIDES:
            if self.enabled_sides[side]:
                result[side] = self.read_state(side, timeout=timeout)
        return result

    def command(self, side: HandSide, angles: Iterable[int | float]) -> list[int]:
        """Send one angle command to ``side`` and return the clamped command."""

        self._require_side(side)
        scaled_angles = self._coerce_angles(angles)
        cmd = defaults.get_inspire_hand_ctrl()
        cmd.angle_set = scaled_angles
        cmd.mode = messages.ANGLE_CONTROL_MODE
        self._publishers[side].Write(cmd)
        return scaled_angles

    def command_both(
        self,
        left_angles: Iterable[int | float] | None = None,
        right_angles: Iterable[int | float] | None = None,
    ) -> dict[HandSide, list[int]]:
        """Send commands to one or both hands."""

        sent = {}
        if left_angles is not None:
            sent["left"] = self.command("left", left_angles)
        if right_angles is not None:
            sent["right"] = self.command("right", right_angles)
        return sent

    def move_to(
        self,
        side: HandSide,
        target_angles: Iterable[int | float],
        duration: float = 1.0,
        rate: float = 50.0,
        start_angles: Iterable[int | float] | None = None,
    ) -> list[int]:
        """Ramp ``side`` from current/start angles to target angles."""

        self._require_side(side)
        target = self._coerce_angles(target_angles)
        start = self._resolve_start_angles(side, start_angles)
        steps = max(1, int(max(duration, 0.0) * max(rate, 1.0)))
        sleep_time = 1.0 / max(rate, 1.0)

        last = target
        for step in range(1, steps + 1):
            alpha = step / steps
            blended = [
                round(start[i] + (target[i] - start[i]) * alpha)
                for i in range(messages.INSPIRE_HAND_DOF)
            ]
            last = self.command(side, blended)
            time.sleep(sleep_time)
        return last

    def open(self, side: HandSide, duration: float = 1.0, rate: float = 50.0) -> list[int]:
        return self.move_to(side, OPEN_ANGLES, duration=duration, rate=rate)

    def close(self, side: HandSide, duration: float = 1.0, rate: float = 50.0) -> list[int]:
        return self.move_to(side, CLOSE_ANGLES, duration=duration, rate=rate)

    def pinch(self, side: HandSide, duration: float = 1.0, rate: float = 50.0) -> list[int]:
        return self.move_to(side, PINCH_ANGLES, duration=duration, rate=rate)

    def _resolve_start_angles(
        self,
        side: HandSide,
        start_angles: Iterable[int | float] | None,
    ) -> list[int]:
        if start_angles is not None:
            return self._coerce_angles(start_angles)
        state = self.read_state(side, timeout=0.05)
        if state is not None:
            return list(state.angles)
        return OPEN_ANGLES.copy()

    def _require_side(self, side: HandSide) -> None:
        if side not in HAND_SIDES:
            raise ValueError(f"side must be one of {HAND_SIDES}, got {side!r}")
        if not self._initialized:
            self.initialize()
        if not self.enabled_sides[side] or side not in self._publishers:
            raise RuntimeError(f"{side} hand is not enabled")

    @staticmethod
    def _coerce_angles(angles: Iterable[int | float]) -> list[int]:
        values = list(angles)
        if len(values) != messages.INSPIRE_HAND_DOF:
            raise ValueError(f"expected {messages.INSPIRE_HAND_DOF} angles, got {len(values)}")
        return [
            int(max(messages.ANGLE_MIN, min(messages.ANGLE_MAX, round(value))))
            for value in values
        ]


__all__ = [
    "CLOSE_ANGLES",
    "HAND_SIDES",
    "LEFT_CMD_TOPIC",
    "LEFT_STATE_TOPIC",
    "OPEN_ANGLES",
    "PINCH_ANGLES",
    "RIGHT_CMD_TOPIC",
    "RIGHT_STATE_TOPIC",
    "HandSide",
    "InspireHandSDK",
    "InspireHandState",
]
