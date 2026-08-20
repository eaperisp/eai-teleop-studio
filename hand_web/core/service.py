"""One-active-device session manager for the standalone debug tool."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from hand_web.core.models import HandAdapter, ValidationError
from hand_web.core.registry import adapter_class, device_capabilities
from teleop.utils.daily_file_logger import DailyFileLogger


DEFAULT_CONFIG: dict[str, Any] = {
    "web": {"host": "127.0.0.1", "port": 18089},
    "vision": {
        "camera": 0,
        "side": "right",
        "width": 960,
        "height": 540,
        "preview_fps": 10,
        "min_detection_confidence": 0.65,
        "min_tracking_confidence": 0.65,
        "filter_min_cutoff": 1.2,
        "filter_beta": 0.08,
        "filter_derivative_cutoff": 1.0,
        "max_velocity": 3.0,
        "endpoint_snap": 0.025,
        "deadband": 0.005,
        "change_threshold": 0.01,
        "min_interval": 0.12,
        "duration_ms": 180,
        "warmup_frames": 5,
        "calibration_samples": 24,
        "lost_timeout": 0.6,
        "joint_limits": [[0.0, 0.98], [0.0, 0.7], [0.0, 0.98], [0.0, 0.98], [0.0, 0.98], [0.0, 0.98]],
    },
    "default_device": "brainco_revo2",
    "devices": {
        "brainco_revo2": {
            "default_transport": "modbus",
            "modbus": {"port": "", "slave_id": 127, "side": "right"},
            "dds": {
                "network_interface": "",
                "domain": 0,
                "sides": "both",
                "publish_hz": 50,
            },
        }
    },
}


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except json.JSONDecodeError as exc:
        raise ValidationError(f"配置文件不是有效 JSON: {exc}") from exc
    if not isinstance(stored, dict):
        raise ValidationError("配置文件根节点必须是对象")
    for key, value in stored.items():
        if key == "devices" and isinstance(value, dict):
            for device_id, device_config in value.items():
                current = config["devices"].setdefault(device_id, {})
                if isinstance(device_config, dict):
                    for device_key, device_value in device_config.items():
                        if isinstance(device_value, dict) and isinstance(current.get(device_key), dict):
                            current[device_key].update(device_value)
                        else:
                            current[device_key] = device_value
        elif key in {"web", "vision"} and isinstance(value, dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


class HandControlService:
    def __init__(self, config_path: Path, logger: DailyFileLogger | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.logger = logger
        self._lock = threading.RLock()
        self._adapter: HandAdapter | None = None
        self._device_id: str | None = None
        self._transport: str | None = None
        self._connected_at: float | None = None
        self._control_owner: str | None = None
        self._last_continuous_log_at = 0.0
        self._log("info", "hand control service initialized", config_file=str(config_path))

    def devices(self) -> dict[str, Any]:
        return {
            "ok": True,
            "devices": device_capabilities(),
            "default_device": self.config.get("default_device", "brainco_revo2"),
            "defaults": self.config.get("devices", {}),
        }

    def connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = self._text(payload.get("device_id") or self.config.get("default_device"), "device_id")
        device_config = self.config.get("devices", {}).get(device_id, {})
        transport = self._text(
            payload.get("transport") or device_config.get("default_transport"),
            "transport",
        )
        requested_options = payload.get("options") or {}
        if not isinstance(requested_options, dict):
            raise ValidationError("options 必须是对象")
        defaults = device_config.get(transport, {})
        options = dict(defaults) if isinstance(defaults, dict) else {}
        options.update(requested_options)
        connection_fields = {
            key: options[key]
            for key in ("port", "slave_id", "side", "network_interface", "domain", "sides", "publish_hz")
            if key in options
        }
        self._log(
            "info",
            "hand connection requested",
            device_id=device_id,
            transport=transport,
            options=connection_fields,
        )

        adapter_type = adapter_class(device_id)
        adapter = adapter_type()
        with self._lock:
            if self._control_owner is not None:
                raise RuntimeError(f"{self._owner_label(self._control_owner)}正在控制灵巧手，请先停止该控制源")
            if self._adapter is not None:
                self._adapter.disconnect()
            self._adapter = None
            self._device_id = None
            self._transport = None
            try:
                result = adapter.connect(transport, options)
            except Exception as exc:
                try:
                    adapter.disconnect()
                except Exception:
                    pass
                self._log(
                    "error",
                    "hand connection failed",
                    device_id=device_id,
                    transport=transport,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise
            self._adapter = adapter
            self._device_id = device_id
            self._transport = transport
            self._connected_at = time.time()
            self._log(
                "info",
                "hand connected",
                device_id=device_id,
                transport=transport,
                port=result.get("port") if isinstance(result, dict) else None,
            )
            return {**result, "device_id": device_id, "transport": transport}

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            if self._control_owner is not None:
                raise RuntimeError(f"{self._owner_label(self._control_owner)}正在控制灵巧手，请先停止该控制源")
            if self._adapter is None:
                return {"ok": True, "message": "当前没有已连接设备"}
            device_id = self._device_id
            transport = self._transport
            result = self._adapter.disconnect()
            self._adapter = None
            self._device_id = None
            self._transport = None
            self._connected_at = None
            self._log("info", "hand disconnected", device_id=device_id, transport=transport)
            return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._adapter is None:
                return {
                    "ok": True,
                    "connected": False,
                    "device_id": None,
                    "transport": None,
                    "hands": {},
                    "error": "",
                    "control_owner": self._control_owner,
                }
            status = self._adapter.status()
            return {
                **status,
                "device_id": self._device_id,
                "transport": self._transport,
                "connected_at": self._connected_at,
                "control_owner": self._control_owner,
            }

    def acquire_control(self, owner: str) -> None:
        owner = self._text(owner, "owner")
        with self._lock:
            if self._control_owner not in (None, owner):
                raise RuntimeError(f"{self._owner_label(self._control_owner)}正在控制灵巧手")
            self._control_owner = owner
            self._log("info", "hand control acquired", owner=owner)

    def release_control(self, owner: str) -> None:
        with self._lock:
            if self._control_owner == owner:
                self._control_owner = None
                self._log("info", "hand control released", owner=owner)

    def command(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = self._text(payload.get("source") or "manual", "source")
        side = self._text(payload.get("side"), "side")
        positions = payload.get("positions")
        if not isinstance(positions, list):
            raise ValidationError("positions 必须是数组")
        try:
            duration_ms = int(payload.get("duration_ms", 500))
        except (TypeError, ValueError) as exc:
            raise ValidationError("duration_ms 必须是整数") from exc
        continuous = payload.get("continuous") is True
        with self._lock:
            if self._control_owner not in (None, source):
                raise RuntimeError(f"{self._owner_label(self._control_owner)}正在控制灵巧手，请先停止后再手动操作")
            if self._adapter is None:
                raise ValidationError("请先连接灵巧手")
            result = self._adapter.command(side, positions, duration_ms)
            now = time.monotonic()
            if not continuous or now - self._last_continuous_log_at >= 1.0:
                self._log(
                    "command",
                    "hand continuous command sampled" if continuous else "hand command sent",
                    device_id=self._device_id,
                    transport=self._transport,
                    side=side,
                    positions=positions,
                    duration_ms=duration_ms,
                    source=source,
                )
                if continuous:
                    self._last_continuous_log_at = now
            return result

    def stop(self, source: str = "manual") -> dict[str, Any]:
        with self._lock:
            if self._control_owner not in (None, source):
                raise RuntimeError(f"{self._owner_label(self._control_owner)}正在控制灵巧手，请先停止该控制源")
            if self._adapter is None:
                raise ValidationError("请先连接灵巧手")
            result = self._adapter.stop()
            self._log(
                "command",
                "hand stop sent",
                device_id=self._device_id,
                transport=self._transport,
                source=source,
            )
            return result

    def close(self) -> None:
        try:
            self.disconnect()
        except Exception:
            pass

    @staticmethod
    def _text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} 不能为空")
        return value.strip()

    @staticmethod
    def _owner_label(owner: str) -> str:
        return {"vision": "视觉控制", "manual": "手动控制"}.get(owner, owner)

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self.logger is not None:
            self.logger.write(level, message, **fields)


__all__ = ["DEFAULT_CONFIG", "HandControlService", "load_config"]
