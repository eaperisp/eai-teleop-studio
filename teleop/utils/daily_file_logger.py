"""Shared daily file logger for dependency-free web services."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class DailyFileLogger:
    def __init__(self, log_dir: Path, filename_prefix: str = "teleop") -> None:
        self.log_dir = log_dir
        self.filename_prefix = filename_prefix
        self._lock = threading.RLock()

    def path_for_today(self) -> Path:
        date = time.strftime("%Y-%m-%d")
        return self.log_dir / "system" / f"{self.filename_prefix}_{date}.log"

    def _path_for_today(self) -> Path:
        return self.path_for_today()

    def write(self, level: str, message: str, **fields: Any) -> None:
        with self._lock:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            extra = ""
            if fields:
                try:
                    extra = " " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
                except TypeError:
                    extra = f" {fields}"
            line = f"{timestamp} [{level.upper()}] {message}{extra}\n"
            try:
                path = self.path_for_today()
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as log_file:
                    log_file.write(line)
            except OSError as exc:
                print(f"{timestamp} [WARNING] failed to write log file {self.path_for_today()}: {exc}")
            print(line, end="")


__all__ = ["DailyFileLogger"]
