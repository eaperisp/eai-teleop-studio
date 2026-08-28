"""Web adapters for Inspire DFX and FTP dexterous hands."""

from __future__ import annotations

import time
from typing import Any, Iterable

from teleop.robot_control.devices.base import JointSpec, normalize_positions, validate_side
from teleop.robot_control.devices.inspire_dfx import InspireDFXHandSDK
from teleop.robot_control.devices.inspire_ftp import InspireHandSDK


HAND_DOF = 6
INSPIRE_JOINTS = (
    JointSpec("thumb", "大拇指弯曲", "Thumb Bend", "thumb"),
    JointSpec("thumb_aux", "大拇指旋转", "Thumb Rotation", "thumb"),
    JointSpec("index", "食指", "Index"),
    JointSpec("middle", "中指", "Middle"),
    JointSpec("ring", "无名指", "Ring"),
    JointSpec("pinky", "小指", "Pinky"),
)

# Inspire DDS order: pinky, ring, middle, index, thumb bend, thumb rotation.
WEB_TO_HARDWARE = (5, 4, 3, 2, 0, 1)
HARDWARE_TO_WEB = (4, 5, 3, 2, 1, 0)

INSPIRE_PREVIEW = {
    "model_root": "/assets/inspire_hand",
    "urdf": "inspire_hand_{side}.urdf",
    "side_prefix": {"left": "L", "right": "R"},
    "material_mode": "inspire",
    "joints": [
        {"index": 0, "targets": [
            {"suffix": "thumb_proximal_pitch_joint", "lower": 0.0, "upper": 0.5},
            {"suffix": "thumb_intermediate_joint", "lower": 0.0, "upper": 0.8},
            {"suffix": "thumb_distal_joint", "lower": 0.0, "upper": 1.2},
        ]},
        {"index": 1, "targets": [
            {"suffix": "thumb_proximal_yaw_joint", "lower": -0.1, "upper": 1.3},
        ]},
        {"index": 2, "targets": [
            {"suffix": "index_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "index_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
        {"index": 3, "targets": [
            {"suffix": "middle_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "middle_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
        {"index": 4, "targets": [
            {"suffix": "ring_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "ring_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
        {"index": 5, "targets": [
            {"suffix": "pinky_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "pinky_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
    ],
}


def _preview_copy() -> dict[str, Any]:
    return {
        **INSPIRE_PREVIEW,
        "side_prefix": dict(INSPIRE_PREVIEW["side_prefix"]),
        "joints": [
            {**joint, "targets": [dict(target) for target in joint["targets"]]}
            for joint in INSPIRE_PREVIEW["joints"]
        ],
    }


def _capabilities(device_id: str, model: str, transport_name: str, description: str) -> dict[str, Any]:
    return {
        "id": device_id,
        "manufacturer": "Inspire Robots",
        "model": model,
        "name": f"因时 {model}",
        "position_convention": {
            "minimum": 0.0,
            "maximum": 1.0,
            "open": 0.0,
            "closed": 1.0,
        },
        "joints": [joint.to_dict() for joint in INSPIRE_JOINTS],
        "quick_actions": [
            {"id": "open", "name": "张开", "name_en": "Open", "positions": [0.0] * HAND_DOF},
            {"id": "half", "name": "半握", "name_en": "Half Grip", "positions": [0.5] * HAND_DOF},
            {"id": "close", "name": "握合", "name_en": "Grip", "positions": [0.85] * HAND_DOF},
        ],
        "preview": _preview_copy(),
        "transports": [{
            "id": "dds",
            "name": transport_name,
            "description": description,
            "supports_duration": False,
            "connection_fields": [
                {
                    "id": "network_interface",
                    "label": "网卡",
                    "type": "text",
                    "placeholder": "例如 enp86s0",
                    "required": False,
                },
                {"id": "domain", "label": "DDS Domain", "type": "number", "value": 0, "minimum": 0, "maximum": 232},
                {
                    "id": "sides",
                    "label": "控制对象",
                    "type": "select",
                    "value": "both",
                    "options": [
                        {"value": "both", "label": "双手"},
                        {"value": "left", "label": "仅左手"},
                        {"value": "right", "label": "仅右手"},
                    ],
                },
            ],
        }],
    }


class _InspireAdapterBase:
    device_id = ""
    model = ""
    transport_name = ""
    transport_description = ""
    sdk_type: type[Any]
    raw_scale = 1.0
    command_burst_seconds = 0.0

    def __init__(self) -> None:
        self._sdk: Any = None
        self._enabled_sides: tuple[str, ...] = ()
        self._connected = False
        self._states: dict[str, tuple[list[float], float] | None] = {}
        self._targets: dict[str, list[float]] = {}
        self._command_active: dict[str, bool] = {}
        self._error = ""

    @classmethod
    def capabilities(cls) -> dict[str, Any]:
        return _capabilities(
            cls.device_id,
            cls.model,
            cls.transport_name,
            cls.transport_description,
        )

    def connect(self, transport: str, options: dict[str, Any]) -> dict[str, Any]:
        if transport != "dds":
            raise ValueError(f"因时 {self.model} 不支持通信方式: {transport}")
        sides = str(options.get("sides", "both"))
        if sides not in {"left", "right", "both"}:
            raise ValueError("sides 必须是 left、right 或 both")
        self._enabled_sides = ("left", "right") if sides == "both" else (sides,)
        self._sdk = self.sdk_type(
            network_interface=str(options.get("network_interface") or "").strip() or None,
            domain=max(0, int(options.get("domain", 0))),
            enable_left="left" in self._enabled_sides,
            enable_right="right" in self._enabled_sides,
        )
        self._sdk.initialize()
        self._states = {side: None for side in self._enabled_sides}
        self._targets = {side: [0.0] * HAND_DOF for side in self._enabled_sides}
        self._command_active = {side: False for side in self._enabled_sides}
        self._connected = True
        self._error = ""
        self._read_states(timeout=0.02)
        return {
            "ok": True,
            "message": f"因时 {self.model} DDS 通道已初始化",
            "transport": "dds",
            "sides": list(self._enabled_sides),
        }

    def disconnect(self) -> dict[str, Any]:
        if self._sdk is not None and hasattr(self._sdk, "cancel_command_burst"):
            self._sdk.cancel_command_burst()
        self._sdk = None
        self._connected = False
        self._enabled_sides = ()
        self._states.clear()
        self._targets.clear()
        self._command_active.clear()
        return {"ok": True, "message": f"因时 {self.model} DDS 通道已断开"}

    def status(self) -> dict[str, Any]:
        if self._connected:
            self._read_states(timeout=0.0)
        now = time.time()
        hands = {}
        for side in self._enabled_sides:
            state = self._states.get(side)
            hands[side] = {
                "enabled": True,
                "online": state is not None and now - state[1] < 2.0,
                "positions": state[0][:] if state else None,
                "targets": self._targets[side][:],
                "command_active": self._command_active[side],
                "last_state_at": state[1] if state else None,
            }
        return {
            "ok": True,
            "connected": self._connected,
            "transport": "dds",
            "mode": "hardware",
            "hands": hands,
            "error": self._error,
        }

    def command(
        self,
        side: str,
        positions: Iterable[int | float],
        duration_ms: int,
    ) -> dict[str, Any]:
        del duration_ms
        side = validate_side(side)
        self._require_side(side)
        normalized = normalize_positions(positions, HAND_DOF)
        hardware_positions = self._to_hardware(normalized)
        if self.command_burst_seconds > 0.0 and hasattr(self._sdk, "command_burst"):
            self._sdk.command_burst(
                side,
                hardware_positions,
                duration=self.command_burst_seconds,
                rate=20.0,
            )
        else:
            self._sdk.command(side, hardware_positions)
        self._targets[side] = normalized[:]
        self._command_active[side] = True
        return {
            "ok": True,
            "message": f"已向{'左' if side == 'left' else '右'}手下发姿态指令",
            "side": side,
            "positions": normalized,
        }

    def stop(self) -> dict[str, Any]:
        self._require_connected()
        active = [side for side in self._enabled_sides if self._command_active[side]]
        if not active:
            return {"ok": True, "message": "当前没有执行中的运动"}
        self._read_states(timeout=0.02)
        held = []
        for side in active:
            state = self._states.get(side)
            if state is not None:
                hardware_positions = self._to_hardware(state[0])
                if self.command_burst_seconds > 0.0 and hasattr(self._sdk, "command_burst"):
                    self._sdk.command_burst(
                        side,
                        hardware_positions,
                        duration=self.command_burst_seconds,
                        rate=20.0,
                    )
                else:
                    self._sdk.command(side, hardware_positions)
                self._targets[side] = state[0][:]
                held.append(side)
            self._command_active[side] = False
        if held:
            return {"ok": True, "message": "已停止更新目标并保持当前位置"}
        return {"ok": True, "message": "已停止发送目标，未收到可用于保持的实时状态"}

    def _read_states(self, timeout: float) -> None:
        try:
            raw_states = self._sdk.read_states(timeout=timeout)
            timestamp = time.time()
            offline = []
            for side, state in raw_states.items():
                lost = tuple(getattr(state, "lost", ())) if state is not None else ()
                if state is not None and not any(lost):
                    self._states[side] = (self._from_hardware(state.angles), timestamp)
                else:
                    self._states[side] = None
                    offline.append(side)
            self._error = (
                "因时串口未收到" + "、".join("左手" if side == "left" else "右手" for side in offline) + "反馈"
                if offline
                else ""
            )
        except Exception as exc:
            self._error = f"读取因时手状态失败: {exc}"

    def _to_hardware(self, positions: list[float]) -> list[int] | list[float]:
        values = [(1.0 - positions[index]) * self.raw_scale for index in WEB_TO_HARDWARE]
        if self.raw_scale == 1.0:
            return [max(0.0, min(1.0, value)) for value in values]
        return [int(max(0, min(int(self.raw_scale), round(value)))) for value in values]

    def _from_hardware(self, angles: Iterable[int | float]) -> list[float]:
        values = list(angles)
        if len(values) != HAND_DOF:
            raise ValueError(f"expected {HAND_DOF} Inspire angles, got {len(values)}")
        return [
            max(0.0, min(1.0, 1.0 - float(values[index]) / self.raw_scale))
            for index in HARDWARE_TO_WEB
        ]

    def _require_connected(self) -> None:
        if not self._connected or self._sdk is None:
            raise RuntimeError(f"因时 {self.model} 尚未连接")

    def _require_side(self, side: str) -> None:
        self._require_connected()
        if side not in self._enabled_sides:
            raise RuntimeError(f"{side} 手未启用")


class InspireDFXAdapter(_InspireAdapterBase):
    device_id = "inspire_dfx"
    model = "DFX"
    transport_name = "机器人 DDS（DFX）"
    transport_description = "通过 inspire_h1 串口服务发布的 rt/inspire/cmd 与 rt/inspire/state 控制"
    sdk_type = InspireDFXHandSDK
    raw_scale = 1.0
    command_burst_seconds = 0.5


class InspireFTPAdapter(_InspireAdapterBase):
    device_id = "inspire_ftp"
    model = "FTP"
    transport_name = "机器人 DDS（FTP）"
    transport_description = "通过左右手独立的 rt/inspire_hand DDS 主题控制"
    sdk_type = InspireHandSDK
    raw_scale = 1000.0


__all__ = [
    "HAND_DOF",
    "HARDWARE_TO_WEB",
    "INSPIRE_JOINTS",
    "InspireDFXAdapter",
    "InspireFTPAdapter",
    "WEB_TO_HARDWARE",
]
