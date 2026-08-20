"""Transport-neutral BrainCo Revo2 SDK facade.

Applications use normalized positions where 0.0 is open and 1.0 is closed.
Transport modules own the conversion to DDS or vendor Modbus units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from teleop.robot_control.devices.base import HandSide, JointSpec, normalize_positions, validate_side


HAND_DOF = 6
BRAINCO_JOINTS = (
    JointSpec("thumb", "大拇指屈伸", "Thumb Flex", "thumb"),
    JointSpec("thumb_aux", "大拇指内收/外展", "Thumb Aux", "thumb"),
    JointSpec("index", "食指", "Index"),
    JointSpec("middle", "中指", "Middle"),
    JointSpec("ring", "无名指", "Ring"),
    JointSpec("pinky", "小指", "Pinky"),
)
TRANSPORTS = ("modbus", "dds")
BRAINCO_PREVIEW = {
    "model_root": "/assets/brainco_hand",
    "urdf": "brainco_{side}.urdf",
    "side_prefix": {"left": "left", "right": "right"},
    "material_mode": "brainco",
    "joints": [
        {"index": 0, "targets": [{"suffix": "thumb_proximal_joint", "lower": 0.0, "upper": 1.0472}]},
        {"index": 1, "targets": [{"suffix": "thumb_metacarpal_joint", "lower": 0.0, "upper": 1.5184}]},
        {"index": 2, "targets": [{"suffix": "index_proximal_joint", "lower": 0.0, "upper": 1.4661}]},
        {"index": 3, "targets": [{"suffix": "middle_proximal_joint", "lower": 0.0, "upper": 1.4661}]},
        {"index": 4, "targets": [{"suffix": "ring_proximal_joint", "lower": 0.0, "upper": 1.4661}]},
        {"index": 5, "targets": [{"suffix": "pinky_proximal_joint", "lower": 0.0, "upper": 1.4661}]},
    ],
}


@dataclass(frozen=True)
class BraincoHandState:
    side: HandSide
    positions: tuple[float, float, float, float, float, float]
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "positions": list(self.positions),
            "timestamp": self.timestamp,
        }


class BraincoHandSDK:
    """Small facade that keeps web and teleoperation code transport agnostic."""

    def __init__(self, transport: str, **options: Any) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"unsupported BrainCo transport: {transport}")
        self.transport_name = transport
        if transport == "dds":
            from .dds_transport import BraincoDDSTransport

            self._transport = BraincoDDSTransport(**options)
        else:
            from .modbus_transport import BraincoModbusTransport

            self._transport = BraincoModbusTransport(**options)

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "id": "brainco_revo2",
            "manufacturer": "BrainCo",
            "model": "Revo2",
            "name": "强脑 Revo2",
            "position_convention": {
                "minimum": 0.0,
                "maximum": 1.0,
                "open": 0.0,
                "closed": 1.0,
            },
            "joints": [joint.to_dict() for joint in BRAINCO_JOINTS],
            "preview": BRAINCO_PREVIEW,
            "quick_actions": [
                {"id": "open", "name": "张开", "name_en": "Open", "positions": [0.0] * HAND_DOF},
                {"id": "half", "name": "半握", "name_en": "Half Grip", "positions": [0.45] * HAND_DOF},
                {"id": "close", "name": "握合", "name_en": "Grip", "positions": [0.82] * HAND_DOF},
            ],
            "transports": [
                {
                    "id": "modbus",
                    "name": "USB / 官方 SDK",
                    "description": "通过官方 bc_stark_sdk 直连灵巧手",
                    "supports_duration": True,
                    "connection_fields": [
                        {"id": "port", "label": "串口", "type": "text", "placeholder": "自动检测或 COM6", "required": False},
                        {"id": "slave_id", "label": "设备 ID", "type": "number", "value": 127, "minimum": 1, "maximum": 247},
                        {"id": "side", "label": "安装侧", "type": "select", "value": "right", "options": [
                            {"value": "left", "label": "左手"},
                            {"value": "right", "label": "右手"},
                        ]},
                    ],
                },
                {
                    "id": "dds",
                    "name": "机器人 DDS",
                    "description": "通过机器人现有 BrainCo DDS 服务控制",
                    "supports_duration": False,
                    "connection_fields": [
                        {"id": "network_interface", "label": "网卡", "type": "text", "placeholder": "例如 eth0", "required": False},
                        {"id": "domain", "label": "DDS Domain", "type": "number", "value": 0, "minimum": 0, "maximum": 232},
                        {"id": "sides", "label": "控制对象", "type": "select", "value": "both", "options": [
                            {"value": "both", "label": "双手"},
                            {"value": "left", "label": "仅左手"},
                            {"value": "right", "label": "仅右手"},
                        ]},
                    ],
                },
            ],
        }

    def connect(self) -> dict[str, Any]:
        return self._transport.connect()

    def disconnect(self) -> dict[str, Any]:
        return self._transport.disconnect()

    def status(self) -> dict[str, Any]:
        return self._transport.status()

    def read_states(self, timeout: float = 0.0) -> dict[HandSide, BraincoHandState | None]:
        reader = getattr(self._transport, "read_states", None)
        if reader is not None:
            return reader(timeout)
        status = self._transport.status()
        timestamp = status.get("timestamp") or 0.0
        states: dict[HandSide, BraincoHandState | None] = {}
        for side, hand in status.get("hands", {}).items():
            positions = hand.get("positions")
            states[side] = (
                BraincoHandState(side, tuple(positions), timestamp)  # type: ignore[arg-type]
                if positions is not None
                else None
            )
        return states

    def command(
        self,
        side: str,
        positions: Iterable[int | float],
        duration_ms: int = 500,
    ) -> list[float]:
        hand_side = validate_side(side)
        values = normalize_positions(positions, HAND_DOF)
        duration = max(0, min(5000, int(duration_ms)))
        return self._transport.command(hand_side, values, duration)

    def command_both(
        self,
        left_positions: Iterable[int | float] | None = None,
        right_positions: Iterable[int | float] | None = None,
        duration_ms: int = 0,
    ) -> dict[str, list[float]]:
        commands: dict[HandSide, list[float]] = {}
        if left_positions is not None:
            commands["left"] = normalize_positions(left_positions, HAND_DOF)
        if right_positions is not None:
            commands["right"] = normalize_positions(right_positions, HAND_DOF)
        if not commands:
            raise ValueError("at least one hand command is required")
        duration = max(0, min(5000, int(duration_ms)))
        command_both = getattr(self._transport, "command_both", None)
        if command_both is not None:
            return command_both(commands, duration)
        return {
            side: self._transport.command(side, values, duration)
            for side, values in commands.items()
        }

    def stop(self) -> dict[str, Any]:
        return self._transport.stop()


__all__ = [
    "BRAINCO_JOINTS",
    "BRAINCO_PREVIEW",
    "HAND_DOF",
    "TRANSPORTS",
    "BraincoHandSDK",
    "BraincoHandState",
]
