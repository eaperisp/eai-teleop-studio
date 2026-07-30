from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .sort_utils import natural_key


class SyncStore:
    def __init__(self, records_path: Path) -> None:
        self.records_path = records_path
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.records_path.exists():
            self._save({"records": {}})

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.records_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"records": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), dict):
            return {"records": {}}
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        tmp_path = self.records_path.with_suffix(self.records_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.records_path)

    def upsert_seen(self, record: dict[str, Any], now: str) -> dict[str, Any]:
        with self._lock:
            payload = self._load()
            records = payload["records"]
            existing = records.get(record["path"])
            if existing is None:
                item = {
                    "path": record["path"],
                    "status": "pending",
                    "file_count": record["file_count"],
                    "total_size": record["total_size"],
                    "max_mtime": record["max_mtime"],
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_sync_at": None,
                    "error": None,
                }
            else:
                changed = (
                    int(existing.get("file_count") or 0) != int(record["file_count"])
                    or int(existing.get("total_size") or 0) != int(record["total_size"])
                    or float(existing.get("max_mtime") or 0) != float(record["max_mtime"])
                )
                status = "pending" if changed and existing.get("status") == "synced" else existing.get("status", "pending")
                item = {
                    **existing,
                    "path": record["path"],
                    "status": status,
                    "file_count": record["file_count"],
                    "total_size": record["total_size"],
                    "max_mtime": record["max_mtime"],
                    "last_seen_at": now,
                    "error": None,
                }
            records[record["path"]] = item
            self._save(payload)
            return dict(item)

    def mark_status(self, path: str, status: str, now: str | None = None, error: str | None = None) -> None:
        with self._lock:
            payload = self._load()
            records = payload["records"]
            item = records.get(path)
            if item is None:
                return
            item["status"] = status
            if status == "synced":
                item["last_sync_at"] = now
                item["error"] = None
            else:
                item["error"] = error
            records[path] = item
            self._save(payload)

    def get(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._load()["records"].get(path)
            return dict(item) if isinstance(item, dict) else None

    def reset_incomplete_syncing(self) -> int:
        with self._lock:
            payload = self._load()
            count = 0
            for item in payload["records"].values():
                if isinstance(item, dict) and item.get("status") == "syncing":
                    item["status"] = "pending"
                    item["error"] = "服务重启或中断后恢复为等待同步"
                    count += 1
            if count:
                self._save(payload)
            return count

    def prune_absent_non_synced(self, valid_paths: set[str]) -> int:
        if not valid_paths:
            return 0
        with self._lock:
            payload = self._load()
            records = payload["records"]
            stale_paths = [
                path
                for path, item in records.items()
                if path not in valid_paths and isinstance(item, dict) and item.get("status") != "synced"
            ]
            for path in stale_paths:
                records.pop(path, None)
            if stale_paths:
                self._save(payload)
            return len(stale_paths)

    def list_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._load()["records"].values())
        records.sort(key=lambda item: natural_key(str(item.get("path") or "")))
        if limit is None:
            return [dict(item) for item in records]
        return [dict(item) for item in records[:limit]]
