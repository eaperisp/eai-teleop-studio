"""Official BrainCo SDK Modbus transport for a directly attached Revo2."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
import sys
import threading
import time
from typing import Any

from teleop.robot_control.devices.base import HandSide, validate_side

from .sdk import HAND_DOF


def _invoke(function: Any, *args: Any, **kwargs: Any) -> Any:
    async def call() -> Any:
        result = function(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return asyncio.run(call())


def _normalize_port(value: Any) -> str:
    port = str(value or "").strip()
    return port.upper() if re.fullmatch(r"COM\d+", port, re.IGNORECASE) else port


class BraincoModbusTransport:
    def __init__(
        self,
        port: str | None = None,
        slave_id: int = 127,
        side: str = "right",
    ) -> None:
        self.port = _normalize_port(port) or None
        self.slave_id = max(1, min(247, int(slave_id)))
        self.side = validate_side(side)
        self._lock = threading.RLock()
        self._library: Any = None
        self._open_modbus: Any = None
        self._client: Any = None
        self._connected = False
        self._error = ""
        self._sdk_version = "未安装"
        self._device_info: dict[str, Any] = {}
        self._positions = [0.0] * HAND_DOF
        self._targets = [0.0] * HAND_DOF
        self._command_active = False
        self._load_sdk()

    @property
    def sdk_available(self) -> bool:
        return self._library is not None and self._open_modbus is not None

    def connect(self) -> dict[str, Any]:
        with self._lock:
            if self._connected:
                return {"ok": True, "message": "Revo2 已连接", "port": self.port}
            if not self.sdk_available:
                raise RuntimeError(
                    "当前 Python 环境未安装官方 bc_stark_sdk，无法使用 USB / Modbus 模式："
                    f"{sys.executable}"
                )

            baudrate = getattr(self._library, "Baudrate", None)
            baud = getattr(baudrate, "Baud460800", baudrate)
            available = self.available_ports()
            candidates = ([self.port] if self.port else []) + [
                candidate for candidate in available if candidate != self.port
            ]
            if not candidates:
                raise RuntimeError("未发现可用串口，请连接设备或手动填写端口")

            failures: list[str] = []
            for candidate in candidates:
                client = None
                try:
                    client = _invoke(self._open_modbus, port_name=candidate, baudrate=baud)
                    info = _invoke(client.get_device_info, self.slave_id)
                    motor_status = _invoke(client.get_motor_status, self.slave_id)
                    positions = self._raw_positions(motor_status)
                    self._client = client
                    self.port = candidate
                    self._positions = positions
                    self._targets = positions[:]
                    self._command_active = False
                    self._device_info = {
                        "serial_number": str(getattr(info, "serial_number", "")),
                        "firmware_version": str(getattr(info, "firmware_version", "")),
                        "hardware_type": str(getattr(info, "hardware_type", "")),
                        "hand_type": str(getattr(info, "hand_type", "")),
                    }
                    self._connected = True
                    self._error = ""
                    return {
                        "ok": True,
                        "message": f"Revo2 已通过 {candidate} 连接",
                        "transport": "modbus",
                        "port": candidate,
                        "side": self.side,
                    }
                except Exception as exc:
                    self._close_client(client)
                    failures.append(f"{candidate}: {exc}")
            self._error = "；".join(failures)
            raise RuntimeError(f"连接 Revo2 失败：{self._error}")

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self._close_client(self._client)
            self._client = None
            self._connected = False
            self._command_active = False
        return {"ok": True, "message": "Revo2 已断开"}

    def command(self, side: HandSide, positions: list[float], duration_ms: int = 500) -> list[float]:
        with self._lock:
            self._require_connected()
            if side != self.side:
                raise RuntimeError(f"当前直连设备配置为 {self.side} 手")
            duration = max(50, min(5000, int(duration_ms)))
            if all(abs(target - current) <= 0.0005 for target, current in zip(positions, self._positions)):
                self._targets = self._positions[:]
                self._command_active = False
                return positions[:]
            raw = [round(value * 1000) for value in positions]
            _invoke(
                self._client.set_finger_positions_and_durations,
                self.slave_id,
                raw,
                [duration] * HAND_DOF,
            )
            self._targets = positions[:]
            self._command_active = True
            return positions[:]

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._connected:
                try:
                    raw = _invoke(self._client.get_motor_status, self.slave_id)
                    self._positions = self._raw_positions(raw)
                    if self._command_active and all(
                        abs(target - current) <= 0.002
                        for target, current in zip(self._targets, self._positions)
                    ):
                        self._command_active = False
                    self._error = ""
                except Exception as exc:
                    self._error = f"读取状态失败: {exc}"
            return {
                "ok": True,
                "connected": self._connected,
                "transport": "modbus",
                "mode": "hardware",
                "hands": {
                    self.side: {
                        "enabled": True,
                        "online": self._connected and not self._error,
                        "positions": self._positions[:],
                        "targets": self._targets[:],
                        "command_active": self._command_active,
                        "last_state_at": time.time() if self._connected else None,
                    }
                },
                "port": self.port,
                "slave_id": self.slave_id,
                "sdk_available": self.sdk_available,
                "sdk_version": self._sdk_version,
                "device": dict(self._device_info),
                "error": self._error,
            }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._require_connected()
            if not self._command_active:
                return {"ok": True, "message": "当前没有执行中的运动"}
            _invoke(self._client.set_finger_speeds, self.slave_id, [0] * HAND_DOF)
            try:
                raw = _invoke(self._client.get_motor_status, self.slave_id)
                self._positions = self._raw_positions(raw)
            except Exception:
                pass
            self._targets = self._positions[:]
            self._command_active = False
        return {"ok": True, "message": "已发送停止指令"}

    def available_ports(self) -> list[str]:
        ports: list[str] = []
        list_ports = getattr(self._library, "list_available_ports", None)
        if list_ports is not None:
            try:
                raw = _invoke(list_ports)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                entries = json.loads(raw) if isinstance(raw, str) else raw
                for entry in entries or []:
                    value = entry.get("port_name") if isinstance(entry, dict) else str(entry)
                    if value:
                        ports.append(_normalize_port(value))
            except Exception:
                pass

        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                    ports.append(_normalize_port(value))
                    index += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except (ImportError, OSError):
            pass

        unique = list(dict.fromkeys(ports))
        channel_b = self._ftdi_channel_b_ports(unique)

        def port_number(value: str) -> int:
            match = re.search(r"\d+", value)
            return int(match.group()) if match else 0

        ordered_b = sorted(set(channel_b), key=port_number, reverse=True)
        remaining = sorted((value for value in unique if value not in ordered_b), key=port_number, reverse=True)
        return ordered_b + remaining

    def _load_sdk(self) -> None:
        try:
            self._library = importlib.import_module("bc_stark_sdk.main_mod")
            self._sdk_version = str(getattr(self._library, "__version__", "2.x"))
        except Exception:
            return
        for module_name in ("revo2.revo2_utils", "revo2_utils"):
            try:
                module = importlib.import_module(module_name)
                self._open_modbus = getattr(module, "open_modbus_revo2")
                break
            except Exception:
                continue
        if self._open_modbus is None:
            self._open_modbus = getattr(self._library, "modbus_open", None)

    def _ftdi_channel_b_ports(self, available: list[str]) -> list[str]:
        result: list[str] = []
        try:
            import winreg

            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\FTDIBUS")
            index = 0
            while True:
                try:
                    instance = winreg.EnumKey(root, index)
                    index += 1
                except OSError:
                    break
                if "VID_0403+PID_6010" not in instance.upper() or not instance.endswith("&2"):
                    continue
                try:
                    params = winreg.OpenKey(root, instance + r"\0000\Device Parameters")
                    value, _ = winreg.QueryValueEx(params, "PortName")
                    winreg.CloseKey(params)
                    candidate = str(value).upper()
                    if candidate in available:
                        result.append(candidate)
                except OSError:
                    continue
            winreg.CloseKey(root)
        except (ImportError, OSError):
            pass
        return result

    def _raw_positions(self, status: Any) -> list[float]:
        raw = [int(value) for value in list(status.positions)[:HAND_DOF]]
        if len(raw) != HAND_DOF:
            raise RuntimeError("设备未返回完整的 6 路关节状态")
        return [max(0.0, min(1.0, value / 1000.0)) for value in raw]

    def _close_client(self, client: Any) -> None:
        if client is None:
            return
        try:
            close = getattr(self._library, "modbus_close", None)
            if close is not None:
                _invoke(close, client)
            elif hasattr(client, "close"):
                client.close()
        except Exception:
            pass

    def _require_connected(self) -> None:
        if not self._connected or self._client is None:
            raise RuntimeError("Revo2 尚未连接")


__all__ = ["BraincoModbusTransport"]
