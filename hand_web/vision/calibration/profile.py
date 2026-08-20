"""Build and persist per-device, per-side hand feature ranges."""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from hand_web.core.models import ValidationError
from hand_web.vision.features import FEATURE_NAMES
from teleop.utils.daily_file_logger import DailyFileLogger


CALIBRATION_STAGES = ("open", "fist", "thumb_abducted", "thumb_adducted")
MINIMUM_SPANS = {
    "thumb_flex": 12.0,
    "thumb_aux": 8.0,
    "index": 25.0,
    "middle": 25.0,
    "ring": 25.0,
    "pinky": 25.0,
}


class VisionCalibrationStore:
    def __init__(self, path: Path, logger: DailyFileLogger | None = None) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.RLock()

    @staticmethod
    def median_sample(samples: list[dict[str, float]]) -> dict[str, float]:
        if not samples:
            raise ValidationError("没有可用于标定的手部数据")
        return {
            name: round(statistics.median(float(sample[name]) for sample in samples), 4)
            for name in FEATURE_NAMES
        }

    def profile(self, device_id: str, side: str) -> dict[str, Any] | None:
        device_id, side = self._identity(device_id, side)
        with self._lock:
            profile = self._read().get("devices", {}).get(device_id, {}).get(side)
        return json.loads(json.dumps(profile)) if isinstance(profile, dict) else None

    def save(self, device_id: str, side: str, captures: dict[str, dict[str, float]]) -> dict[str, Any]:
        device_id, side = self._identity(device_id, side)
        missing = [stage for stage in CALIBRATION_STAGES if stage not in captures]
        if missing:
            raise ValidationError(f"标定步骤未完成: {', '.join(missing)}")
        ranges = {
            "thumb_flex": [captures["open"]["thumb_flex"], captures["fist"]["thumb_flex"]],
            "thumb_aux": [captures["thumb_abducted"]["thumb_aux"], captures["thumb_adducted"]["thumb_aux"]],
            "index": [captures["open"]["index"], captures["fist"]["index"]],
            "middle": [captures["open"]["middle"], captures["fist"]["middle"]],
            "ring": [captures["open"]["ring"], captures["fist"]["ring"]],
            "pinky": [captures["open"]["pinky"], captures["fist"]["pinky"]],
        }
        for name, (opened, closed) in ranges.items():
            if abs(closed - opened) < MINIMUM_SPANS[name]:
                raise ValidationError(f"{name} 标定动作幅度不足，请重新采集")
        profile = {
            "version": 1,
            "ranges": {name: [round(float(value), 4) for value in values] for name, values in ranges.items()},
            "captures": captures,
            "updated_at": time.time(),
        }
        with self._lock:
            data = self._read()
            data.setdefault("devices", {}).setdefault(device_id, {})[side] = profile
            self._write(data)
        self._log("info", "vision calibration saved", device_id=device_id, side=side)
        return json.loads(json.dumps(profile))

    def reset(self, device_id: str, side: str) -> None:
        device_id, side = self._identity(device_id, side)
        with self._lock:
            data = self._read()
            device = data.setdefault("devices", {}).setdefault(device_id, {})
            if side in device:
                del device[side]
                self._write(data)
        self._log("info", "vision calibration reset", device_id=device_id, side=side)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "devices": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"视觉标定文件读取失败: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("devices", {}), dict):
            raise ValidationError("视觉标定文件格式不正确")
        data.setdefault("version", 1)
        data.setdefault("devices", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise RuntimeError(f"视觉标定文件保存失败: {exc}") from exc

    @staticmethod
    def _identity(device_id: str, side: str) -> tuple[str, str]:
        device_id = str(device_id or "").strip()
        side = str(side or "").strip().lower()
        if not device_id:
            raise ValidationError("device_id 不能为空")
        if side not in {"left", "right"}:
            raise ValidationError("side 必须是 left 或 right")
        return device_id, side

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self.logger is not None:
            self.logger.write(level, message, **fields)


__all__ = ["CALIBRATION_STAGES", "MINIMUM_SPANS", "VisionCalibrationStore"]
