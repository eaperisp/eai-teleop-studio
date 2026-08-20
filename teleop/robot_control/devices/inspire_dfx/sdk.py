"""High-level SDK for the Inspire DFX dexterous hand DDS service."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable, Literal


HandSide = Literal["left", "right"]

CMD_TOPIC = "rt/inspire/cmd"
STATE_TOPIC = "rt/inspire/state"
HAND_DOF = 6
TOTAL_DOF = 12
HAND_SIDES: tuple[HandSide, ...] = ("left", "right")

RIGHT_OFFSET = 0
LEFT_OFFSET = 6

OPEN_ANGLES = [1.0] * HAND_DOF
CLOSE_ANGLES = [0.0] * HAND_DOF
PINCH_ANGLES = [1.0, 1.0, 1.0, 0.25, 0.25, 0.55]


@dataclass(frozen=True)
class InspireDFXHandState:
    """One sampled DFX hand state.

    ``angles`` are normalized DFX command/state values in the inclusive range
    0.0-1.0. The DFX service treats 1.0 as open and 0.0 as closed.
    """

    side: HandSide
    angles: tuple[float, float, float, float, float, float]


class InspireDFXHandSDK:
    """Control and inspect Inspire DFX hands over Unitree DDS."""

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
        self._publisher = None
        self._subscriber = None
        self._cmd_msg = None
        self._last_command = [1.0] * TOTAL_DOF
        self._state_synced = False
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        from unitree_sdk2py.core.channel import (  # pylint: disable=import-outside-toplevel
            ChannelFactoryInitialize,
            ChannelPublisher,
            ChannelSubscriber,
        )
        from unitree_sdk2py.idl.default import (  # pylint: disable=import-outside-toplevel
            unitree_go_msg_dds__MotorCmd_,
        )
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import (  # pylint: disable=import-outside-toplevel
            MotorCmds_,
            MotorStates_,
        )

        if self.network_interface:
            ChannelFactoryInitialize(self.domain, networkInterface=self.network_interface)
        else:
            ChannelFactoryInitialize(self.domain)

        self._publisher = ChannelPublisher(CMD_TOPIC, MotorCmds_)
        self._publisher.Init()
        self._subscriber = ChannelSubscriber(STATE_TOPIC, MotorStates_)
        self._subscriber.Init()
        self._cmd_msg = MotorCmds_()
        self._cmd_msg.cmds = [unitree_go_msg_dds__MotorCmd_() for _ in range(TOTAL_DOF)]
        for index, value in enumerate(self._last_command):
            self._cmd_msg.cmds[index].q = value

        self._initialized = True

    def read_state(self, side: HandSide, timeout: float = 0.02) -> InspireDFXHandState | None:
        self._require_side(side)
        state = self._read_raw_state(timeout)
        if state is None:
            return None

        offset = self._offset(side)
        angles = [float(state.states[offset + index].q) for index in range(HAND_DOF)]
        return InspireDFXHandState(side=side, angles=tuple(angles))

    def read_states(self, timeout: float = 0.02) -> dict[HandSide, InspireDFXHandState | None]:
        if not self._initialized:
            self.initialize()
        state = self._read_raw_state(timeout)
        result: dict[HandSide, InspireDFXHandState | None] = {}
        for side in HAND_SIDES:
            if not self.enabled_sides[side]:
                continue
            if state is None or len(state.states) < TOTAL_DOF:
                result[side] = None
                continue
            offset = self._offset(side)
            angles = tuple(float(state.states[offset + index].q) for index in range(HAND_DOF))
            result[side] = InspireDFXHandState(side=side, angles=angles)  # type: ignore[arg-type]
        return result

    def command(self, side: HandSide, angles: Iterable[int | float]) -> list[float]:
        self._require_side(side)
        if not self._state_synced:
            self._sync_last_command_from_state(timeout=0.05)
        normalized = self._coerce_angles(angles)
        offset = self._offset(side)
        for index, value in enumerate(normalized):
            self._last_command[offset + index] = value
            self._cmd_msg.cmds[offset + index].q = value
        self._publisher.Write(self._cmd_msg)
        return normalized

    def move_to(
        self,
        side: HandSide,
        target_angles: Iterable[int | float],
        duration: float = 1.0,
        rate: float = 50.0,
        start_angles: Iterable[int | float] | None = None,
    ) -> list[float]:
        self._require_side(side)
        if not self._state_synced:
            self._sync_last_command_from_state(timeout=0.05)
        target = self._coerce_angles(target_angles)
        start = self._resolve_start_angles(side, start_angles)
        steps = max(1, int(max(duration, 0.0) * max(rate, 1.0)))
        sleep_time = 1.0 / max(rate, 1.0)

        last = target
        for step in range(1, steps + 1):
            alpha = step / steps
            blended = [
                start[index] + (target[index] - start[index]) * alpha
                for index in range(HAND_DOF)
            ]
            last = self.command(side, blended)
            time.sleep(sleep_time)
        return last

    def open(self, side: HandSide, duration: float = 1.0, rate: float = 50.0) -> list[float]:
        return self.move_to(side, OPEN_ANGLES, duration=duration, rate=rate)

    def close(self, side: HandSide, duration: float = 1.0, rate: float = 50.0) -> list[float]:
        return self.move_to(side, CLOSE_ANGLES, duration=duration, rate=rate)

    def pinch(self, side: HandSide, duration: float = 1.0, rate: float = 50.0) -> list[float]:
        return self.move_to(side, PINCH_ANGLES, duration=duration, rate=rate)

    def _resolve_start_angles(
        self,
        side: HandSide,
        start_angles: Iterable[int | float] | None,
    ) -> list[float]:
        if start_angles is not None:
            return self._coerce_angles(start_angles)
        state = self.read_state(side, timeout=0.05)
        if state is not None:
            return list(state.angles)
        offset = self._offset(side)
        return self._last_command[offset:offset + HAND_DOF]

    def _read_raw_state(self, timeout: float):
        try:
            return self._subscriber.Read(timeout)
        except TypeError:
            return self._subscriber.Read()

    def _sync_last_command_from_state(self, timeout: float) -> None:
        state = self._read_raw_state(timeout)
        if state is not None and len(state.states) >= TOTAL_DOF:
            for index in range(TOTAL_DOF):
                self._last_command[index] = max(0.0, min(1.0, float(state.states[index].q)))
                self._cmd_msg.cmds[index].q = self._last_command[index]
        self._state_synced = True

    def _require_side(self, side: HandSide) -> None:
        if side not in HAND_SIDES:
            raise ValueError(f"side must be one of {HAND_SIDES}, got {side!r}")
        if not self._initialized:
            self.initialize()
        if not self.enabled_sides[side]:
            raise RuntimeError(f"{side} hand is not enabled")

    @staticmethod
    def _offset(side: HandSide) -> int:
        return LEFT_OFFSET if side == "left" else RIGHT_OFFSET

    @staticmethod
    def _coerce_angles(angles: Iterable[int | float]) -> list[float]:
        values = list(angles)
        if len(values) != HAND_DOF:
            raise ValueError(f"expected {HAND_DOF} angles, got {len(values)}")
        return [max(0.0, min(1.0, float(value))) for value in values]


__all__ = [
    "CLOSE_ANGLES",
    "CMD_TOPIC",
    "HAND_DOF",
    "HAND_SIDES",
    "LEFT_OFFSET",
    "OPEN_ANGLES",
    "PINCH_ANGLES",
    "RIGHT_OFFSET",
    "STATE_TOPIC",
    "HandSide",
    "InspireDFXHandSDK",
    "InspireDFXHandState",
]
