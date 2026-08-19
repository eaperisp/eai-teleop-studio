"""Unitree DDS transport for BrainCo Revo2 hands."""

from __future__ import annotations

import threading
import time
from typing import Any

from teleop.robot_control.devices.base import HAND_SIDES, HandSide

from .sdk import BraincoHandState, HAND_DOF


LEFT_COMMAND_TOPIC = "rt/brainco/left/cmd"
LEFT_STATE_TOPIC = "rt/brainco/left/state"
RIGHT_COMMAND_TOPIC = "rt/brainco/right/cmd"
RIGHT_STATE_TOPIC = "rt/brainco/right/state"


class BraincoDDSTransport:
    """Control one or two BrainCo hands through the robot DDS bridge."""

    def __init__(
        self,
        network_interface: str | None = None,
        domain: int = 0,
        sides: str = "both",
        initialize_factory: bool = True,
        continuous_publish: bool = False,
        publish_hz: float = 50.0,
    ) -> None:
        if sides not in {"left", "right", "both"}:
            raise ValueError("sides must be left, right, or both")
        self.network_interface = str(network_interface or "").strip() or None
        self.domain = max(0, int(domain))
        self.enabled_sides: tuple[HandSide, ...] = HAND_SIDES if sides == "both" else (sides,)  # type: ignore[assignment]
        self.initialize_factory = bool(initialize_factory)
        self.continuous_publish = bool(continuous_publish)
        self.publish_hz = max(1.0, min(100.0, float(publish_hz)))

        self._lock = threading.RLock()
        self._publishers: dict[HandSide, Any] = {}
        self._subscribers: dict[HandSide, Any] = {}
        self._messages: dict[HandSide, Any] = {}
        self._targets: dict[HandSide, list[float]] = {
            side: [0.0] * HAND_DOF for side in self.enabled_sides
        }
        self._command_active: dict[HandSide, bool] = {
            side: False for side in self.enabled_sides
        }
        self._states: dict[HandSide, BraincoHandState | None] = {
            side: None for side in self.enabled_sides
        }
        self._connected = False
        self._running = False
        self._worker: threading.Thread | None = None
        self._error = ""

    def connect(self) -> dict[str, Any]:
        with self._lock:
            if self._connected:
                return {"ok": True, "message": "DDS 已初始化"}

            from unitree_sdk2py.core.channel import (
                ChannelFactoryInitialize,
                ChannelPublisher,
                ChannelSubscriber,
            )
            from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_, MotorStates_

            if self.initialize_factory:
                if self.network_interface:
                    ChannelFactoryInitialize(self.domain, networkInterface=self.network_interface)
                else:
                    ChannelFactoryInitialize(self.domain)

            topics = {
                "left": (LEFT_COMMAND_TOPIC, LEFT_STATE_TOPIC),
                "right": (RIGHT_COMMAND_TOPIC, RIGHT_STATE_TOPIC),
            }
            for side in self.enabled_sides:
                command_topic, state_topic = topics[side]
                publisher = ChannelPublisher(command_topic, MotorCmds_)
                publisher.Init()
                subscriber = ChannelSubscriber(state_topic, MotorStates_)
                subscriber.Init()
                message = MotorCmds_()
                message.cmds = [unitree_go_msg_dds__MotorCmd_() for _ in range(HAND_DOF)]
                for command in message.cmds:
                    command.q = 0.0
                    command.dq = 1.0
                self._publishers[side] = publisher
                self._subscribers[side] = subscriber
                self._messages[side] = message

            self._connected = True
            self._error = ""
            if self.continuous_publish:
                self._running = True
                self._worker = threading.Thread(target=self._io_loop, name="brainco-dds", daemon=True)
                self._worker.start()

        return {
            "ok": True,
            "message": "BrainCo DDS 通道已初始化",
            "transport": "dds",
            "sides": list(self.enabled_sides),
        }

    def disconnect(self) -> dict[str, Any]:
        self._running = False
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        with self._lock:
            self._worker = None
            self._publishers.clear()
            self._subscribers.clear()
            self._messages.clear()
            self._connected = False
        return {"ok": True, "message": "DDS 通道已断开"}

    def command(self, side: HandSide, positions: list[float], duration_ms: int = 0) -> list[float]:
        return self.command_both({side: positions}, duration_ms)[side]

    def command_both(
        self,
        commands: dict[HandSide, list[float]],
        duration_ms: int = 0,
    ) -> dict[str, list[float]]:
        del duration_ms  # DDS bridge accepts normalized targets, not motion durations.
        with self._lock:
            self._require_connected()
            for side, positions in commands.items():
                if side not in self.enabled_sides:
                    raise RuntimeError(f"{side} hand is not enabled")
                self._targets[side] = positions[:]
                self._update_message(side, positions)
                self._command_active[side] = True
            if not self.continuous_publish:
                for side in commands:
                    self._publishers[side].Write(self._messages[side])
        return {side: values[:] for side, values in commands.items()}

    def read_states(self, timeout: float = 0.0) -> dict[HandSide, BraincoHandState | None]:
        with self._lock:
            self._require_connected()
            subscribers = dict(self._subscribers)

        for side, subscriber in subscribers.items():
            try:
                try:
                    raw = subscriber.Read(timeout)
                except TypeError:
                    raw = subscriber.Read()
                if raw is None or len(raw.states) < HAND_DOF:
                    continue
                positions = tuple(
                    max(0.0, min(1.0, float(raw.states[index].q)))
                    for index in range(HAND_DOF)
                )
                with self._lock:
                    self._states[side] = BraincoHandState(side, positions, time.time())
            except Exception as exc:
                with self._lock:
                    self._error = f"读取 {side} 手状态失败: {exc}"
        with self._lock:
            return dict(self._states)

    def status(self) -> dict[str, Any]:
        if self._connected and not self.continuous_publish:
            self.read_states(timeout=0.0)
        now = time.time()
        with self._lock:
            hands = {}
            for side in self.enabled_sides:
                state = self._states[side]
                hands[side] = {
                    "enabled": True,
                    "online": state is not None and now - state.timestamp < 2.0,
                    "positions": list(state.positions) if state else None,
                    "targets": self._targets[side][:],
                    "command_active": self._command_active[side],
                    "last_state_at": state.timestamp if state else None,
                }
            return {
                "ok": True,
                "connected": self._connected,
                "transport": "dds",
                "mode": "hardware",
                "hands": hands,
                "network_interface": self.network_interface,
                "domain": self.domain,
                "error": self._error,
            }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected()
            active_sides = [side for side in self.enabled_sides if self._command_active[side]]
            if not active_sides:
                return {"ok": True, "message": "当前没有执行中的运动"}
            held_sides: list[HandSide] = []
            for side in active_sides:
                state = self._states[side]
                if state is not None:
                    values = list(state.positions)
                    self._targets[side] = values
                    self._update_message(side, values)
                    self._publishers[side].Write(self._messages[side])
                    held_sides.append(side)
                self._command_active[side] = False
        if not held_sides:
            return {"ok": True, "message": "已停止发送目标，未收到可用于保持的实时状态"}
        return {"ok": True, "message": "已停止更新目标并保持当前位置"}

    def _io_loop(self) -> None:
        interval = 1.0 / self.publish_hz
        while self._running:
            started = time.monotonic()
            try:
                with self._lock:
                    for side in self.enabled_sides:
                        if self._command_active[side]:
                            self._publishers[side].Write(self._messages[side])
                self.read_states(timeout=0.0)
            except Exception as exc:
                with self._lock:
                    self._error = f"DDS 通信失败: {exc}"
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    def _update_message(self, side: HandSide, positions: list[float]) -> None:
        message = self._messages[side]
        for index, value in enumerate(positions):
            message.cmds[index].q = value

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("DDS transport is not connected")


__all__ = [
    "LEFT_COMMAND_TOPIC",
    "LEFT_STATE_TOPIC",
    "RIGHT_COMMAND_TOPIC",
    "RIGHT_STATE_TOPIC",
    "BraincoDDSTransport",
]
