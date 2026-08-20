"""Persistent, device-scoped hand pose library."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from hand_web.core.models import ValidationError
from hand_web.core.registry import device_capabilities
from teleop.robot_control.devices.base import normalize_positions
from teleop.utils.daily_file_logger import DailyFileLogger


class PoseStore:
    def __init__(self, path: Path, logger: DailyFileLogger | None = None) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.RLock()

    def list(self, device_id: str) -> dict[str, Any]:
        device_id = self._device_id(device_id)
        with self._lock:
            poses = self._poses_for(self._read(), device_id)
        return {"ok": True, "device_id": device_id, "poses": poses}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = self._device_id(payload.get("device_id"))
        name_en = self._text(payload.get("name_en"), "英文名称", 64)
        description_zh = self._text(payload.get("description_zh"), "中文描述", 64)
        positions = self._positions(device_id, payload.get("positions"))
        pose_id = str(payload.get("id") or "").strip()

        with self._lock:
            data = self._read()
            poses = self._poses_for(data, device_id)
            if pose_id:
                index = next((index for index, pose in enumerate(poses) if pose["id"] == pose_id), None)
                if index is None:
                    raise ValidationError("要编辑的姿态不存在")
                pose = {"id": pose_id, "name_en": name_en, "description_zh": description_zh, "positions": positions}
                poses[index] = pose
                operation = "updated"
            else:
                if len(poses) >= 100:
                    raise ValidationError("每种设备最多保存 100 个姿态")
                pose = {
                    "id": f"pose_{uuid.uuid4().hex[:12]}",
                    "name_en": name_en,
                    "description_zh": description_zh,
                    "positions": positions,
                }
                poses.append(pose)
                operation = "created"
            data.setdefault("devices", {})[device_id] = poses
            self._write(data)

        self._log("info", f"hand pose {operation}", device_id=device_id, pose_id=pose["id"], name_en=name_en)
        return {"ok": True, "device_id": device_id, "pose": pose, "poses": poses}

    def delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = self._device_id(payload.get("device_id"))
        pose_id = self._text(payload.get("id"), "姿态 ID", 80)
        with self._lock:
            data = self._read()
            poses = self._poses_for(data, device_id)
            remaining = [pose for pose in poses if pose["id"] != pose_id]
            if len(remaining) == len(poses):
                raise ValidationError("要删除的姿态不存在")
            data.setdefault("devices", {})[device_id] = remaining
            self._write(data)
        self._log("info", "hand pose deleted", device_id=device_id, pose_id=pose_id)
        return {"ok": True, "device_id": device_id, "deleted_id": pose_id, "poses": remaining}

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "devices": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"姿态文件读取失败: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("devices", {}), dict):
            raise ValidationError("姿态文件格式不正确")
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
            raise RuntimeError(f"姿态文件保存失败: {exc}") from exc

    def _poses_for(self, data: dict[str, Any], device_id: str) -> list[dict[str, Any]]:
        stored = data.get("devices", {}).get(device_id)
        if stored is None:
            return self._default_poses(device_id)
        if not isinstance(stored, list):
            raise ValidationError(f"设备 {device_id} 的姿态列表格式不正确")
        result: list[dict[str, Any]] = []
        for item in stored:
            if not isinstance(item, dict):
                raise ValidationError(f"设备 {device_id} 包含无效姿态")
            result.append({
                "id": self._text(item.get("id"), "姿态 ID", 80),
                "name_en": self._text(item.get("name_en"), "英文名称", 64),
                "description_zh": self._text(item.get("description_zh"), "中文描述", 64),
                "positions": self._positions(device_id, item.get("positions")),
            })
        return result

    def _default_poses(self, device_id: str) -> list[dict[str, Any]]:
        device = self._device(device_id)
        return [
            {
                "id": str(action["id"]),
                "name_en": str(action.get("name_en") or action.get("english_name") or action["id"]),
                "description_zh": str(action.get("description_zh") or action.get("name") or action["id"]),
                "positions": self._positions(device_id, action.get("positions")),
            }
            for action in device.get("quick_actions", [])
        ]

    def _positions(self, device_id: str, value: Any) -> list[float]:
        if not isinstance(value, list):
            raise ValidationError("姿态关节数据必须是数组")
        dof = len(self._device(device_id).get("joints", []))
        try:
            return normalize_positions(value, dof)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"姿态关节数据不正确: {exc}") from exc

    def _device_id(self, value: Any) -> str:
        device_id = self._text(value, "device_id", 80)
        self._device(device_id)
        return device_id

    @staticmethod
    def _device(device_id: str) -> dict[str, Any]:
        device = next((item for item in device_capabilities() if item.get("id") == device_id), None)
        if device is None:
            raise ValidationError(f"未知设备: {device_id}")
        return device

    @staticmethod
    def _text(value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} 不能为空")
        result = value.strip()
        if len(result) > maximum:
            raise ValidationError(f"{field} 不能超过 {maximum} 个字符")
        return result

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self.logger is not None:
            self.logger.write(level, message, **fields)


__all__ = ["PoseStore"]
