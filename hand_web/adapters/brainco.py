"""Web adapter for BrainCo Revo2."""

from __future__ import annotations

from typing import Any, Iterable

from teleop.robot_control.devices.brainco import BraincoHandSDK


class BraincoAdapter:
    def __init__(self) -> None:
        self._sdk: BraincoHandSDK | None = None
        self._transport: str | None = None

    @classmethod
    def capabilities(cls) -> dict[str, Any]:
        return BraincoHandSDK.capabilities()

    def connect(self, transport: str, options: dict[str, Any]) -> dict[str, Any]:
        if transport == "dds":
            sdk_options = {
                "network_interface": options.get("network_interface") or None,
                "domain": options.get("domain", 0),
                "sides": options.get("sides", "both"),
                "initialize_factory": True,
                "continuous_publish": True,
                "publish_hz": options.get("publish_hz", 50),
            }
        elif transport == "modbus":
            sdk_options = {
                "port": options.get("port") or None,
                "slave_id": options.get("slave_id", 127),
                "side": options.get("side", "right"),
            }
        else:
            raise ValueError(f"强脑 Revo2 不支持通信方式: {transport}")

        self._sdk = BraincoHandSDK(transport, **sdk_options)
        self._transport = transport
        return self._sdk.connect()

    def disconnect(self) -> dict[str, Any]:
        if self._sdk is None:
            return {"ok": True, "message": "当前没有已连接设备"}
        try:
            return self._sdk.disconnect()
        finally:
            self._sdk = None
            self._transport = None

    def status(self) -> dict[str, Any]:
        if self._sdk is None:
            return {"ok": True, "connected": False, "hands": {}, "error": ""}
        return self._sdk.status()

    def command(
        self,
        side: str,
        positions: Iterable[int | float],
        duration_ms: int,
    ) -> dict[str, Any]:
        if self._sdk is None:
            raise RuntimeError("Revo2 尚未连接")
        sent = self._sdk.command(side, positions, duration_ms)
        return {
            "ok": True,
            "message": f"已向{'左' if side == 'left' else '右'}手下发姿态指令",
            "side": side,
            "positions": sent,
        }

    def stop(self) -> dict[str, Any]:
        if self._sdk is None:
            raise RuntimeError("Revo2 尚未连接")
        return self._sdk.stop()


__all__ = ["BraincoAdapter"]
