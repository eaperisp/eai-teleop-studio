from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
import json
import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import SyncConfig
from .sort_utils import natural_key
from .storage import SyncStore


class SyncError(RuntimeError):
    pass


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def display_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def remote_shell_path(path: str) -> str:
    clean = path.strip()
    if clean == "~":
        return '"$HOME"'
    if clean.startswith("~/"):
        return '"$HOME"/' + shlex.quote(clean[2:])
    return shlex.quote(clean)


class SyncWorker:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parents[1]
        self.config_dir = self.base_dir / "config"
        self.data_dir = self.base_dir / "data"
        self.logs_dir = self.base_dir / "logs"
        for directory in (self.config_dir, self.data_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.config = SyncConfig()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._logs: deque[dict[str, str]] = deque(maxlen=200)
        self._ensure_config_file()
        self._load_config_file()
        self.store = SyncStore(self._records_path())
        self.running = False
        self.stopping = False
        self.connection_ok = False
        self.connection_message = "未测试"
        self.connection_checked_at: str | None = None
        self.current_record: str | None = None
        self.current_records: set[str] = set()
        self.last_scan_time: str | None = None
        self.last_sync_time: str | None = None
        restored = self.store.reset_incomplete_syncing()
        if restored:
            self.log("warn", f"restored {restored} stale syncing records to pending")
        self.log("info", "sync service initialized")

    def _tool_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def _records_path(self) -> Path:
        return self._tool_path(self.config.records_file)

    def _log_path(self) -> Path:
        configured = self._tool_path(self.config.log_file)
        date_suffix = datetime.now().strftime("%Y-%m-%d")
        if configured.suffix:
            return configured.with_name(f"{configured.stem}_{date_suffix}{configured.suffix}")
        return configured.with_name(f"{configured.name}_{date_suffix}.log")

    def _config_path(self) -> Path:
        return self._tool_path(self.config.config_file)

    def _ensure_config_file(self) -> None:
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        payload = asdict(self.config)
        payload["ssh_password"] = ""
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _load_config_file(self) -> None:
        path = self._config_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.log("warn", f"config file is not readable: {path}")
            return
        if not isinstance(payload, dict):
            self.log("warn", f"config file is not an object: {path}")
            return
        for field in self.config.__dataclass_fields__:
            if field not in payload:
                continue
            if field == "ssh_password" and not payload.get(field):
                continue
            setattr(self.config, field, payload[field])

    def save_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise SyncError("同步任务运行中，停止后再修改配置")
            allowed = {
                "remote_host",
                "jump_host",
                "remote_dir",
                "local_dir",
                "interval_seconds",
                "settle_seconds",
                "concurrent_syncs",
                "record_depth",
                "graceful_stop",
                "excludes",
                "ssh_password",
            }
            for key, value in raw.items():
                if key not in allowed:
                    continue
                if key in {"interval_seconds", "settle_seconds", "record_depth", "concurrent_syncs"}:
                    value = int(value)
                    if value <= 0:
                        raise SyncError(f"{key} 必须大于 0")
                if key == "excludes":
                    if not isinstance(value, list):
                        raise SyncError("excludes 必须是数组")
                    value = [str(item).strip() for item in value if str(item).strip()]
                if key in {"remote_host", "remote_dir", "local_dir"} and not str(value).strip():
                    raise SyncError(f"{key} 不能为空")
                if key == "ssh_password" and value == "__KEEP__":
                    continue
                setattr(self.config, key, value)
            self._write_config_file()
            self.store = SyncStore(self._records_path())
            self.connection_ok = False
            self.connection_message = "配置已修改，请重新测试连接"
            self.connection_checked_at = None
            self.log("ok", "config saved")
            return self.snapshot()

    def _write_config_file(self) -> None:
        payload = asdict(self.config)
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def log(self, level: str, message: str) -> None:
        with self._lock:
            entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}
            self._logs.append(entry)
            log_line = f"{now_text()} [{level.upper()}] {message}\n"
            try:
                self._log_path().parent.mkdir(parents=True, exist_ok=True)
                with self._log_path().open("a", encoding="utf-8") as log_file:
                    log_file.write(log_line)
            except OSError:
                pass

    def access_log(
        self,
        client: str,
        method: str,
        path: str,
        status_code: int,
        elapsed_ms: float,
        error: str | None = None,
    ) -> None:
        message = f"{client} {method} {path} {status_code} {elapsed_ms:.1f}ms"
        if error:
            message = f"{message} error={error}"
        self.log("access" if status_code < 400 else "error", message)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = self.store.list_records()
            synced = sum(1 for item in records if item["status"] == "synced")
            pending = sum(1 for item in records if item["status"] == "pending")
            waiting = sum(1 for item in records if item["status"] == "waiting")
            failed = sum(1 for item in records if item["status"] == "failed")
            queue_records = [item for item in records if item["status"] != "synced"]
            queue = [self._record_public(item) for item in queue_records]
            cfg = asdict(self.config)
            cfg["ssh_password"] = "已配置" if self.config.ssh_password else ""
            has_syncing = bool(self.current_records) or any(item["status"] == "syncing" for item in records)
            if self.stopping and has_syncing:
                status_text = "停止中（等待同步完成）"
            elif self.running or has_syncing:
                status_text = "运行中"
            else:
                status_text = "停止中"
            return {
                "running": self.running,
                "stopping": self.stopping,
                "statusText": status_text,
                "currentRecord": ", ".join(sorted(self.current_records, key=natural_key)) or self.current_record,
                "currentRecords": sorted(self.current_records, key=natural_key),
                "syncedCount": synced,
                "newCount": pending,
                "waitingCount": waiting,
                "failedCount": failed,
                "lastScanTime": self.last_scan_time,
                "lastSyncTime": self.last_sync_time,
                "connection": {
                    "ok": self.connection_ok,
                    "message": self.connection_message,
                    "checkedAt": self.connection_checked_at,
                },
                "config": cfg,
                "queue": queue,
                "records": [self._record_public(item) for item in records],
                "logs": list(self._logs),
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                self.log("warn", "start ignored because worker is already running")
                return self.snapshot()
            if not self.connection_ok:
                raise SyncError("请先测试连接，连接通过后才能开始同步")
            self.running = True
            self.stopping = False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="robot-sync-worker", daemon=True)
            self._thread.start()
            self.log("ok", "sync worker started")
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                self.log("warn", "stop ignored because worker is not running")
                return self.snapshot()
            self.stopping = True
            self._stop_event.set()
            self.log("warn", "stop requested; no new sync job will be started")
            return self.snapshot()

    def scan_once(self) -> dict[str, Any]:
        if not self.connection_ok:
            raise SyncError("请先测试连接，连接通过后才能扫描")
        self._scan_and_sync_once(allow_sync=False)
        return self.snapshot()

    def test_connection(self) -> dict[str, Any]:
        try:
            command = f"if test -d {remote_shell_path(self.config.remote_dir)}; then echo OK; else echo MISSING; fi"
            output = self._run_ssh(command, timeout=20).strip()
            if output == "MISSING":
                raise SyncError(f"远端目录不存在或不可访问: {self.config.remote_dir}")
            if output != "OK":
                raise SyncError(f"连接测试返回异常: {output or 'empty output'}")
            with self._lock:
                self.connection_ok = True
                self.connection_message = "连接成功"
                self.connection_checked_at = datetime.now().strftime("%H:%M:%S")
            self.log("ok", f"connection test passed: {self.config.remote_host}:{self.config.remote_dir}")
        except Exception as exc:
            with self._lock:
                self.connection_ok = False
                self.connection_message = str(exc)
                self.connection_checked_at = datetime.now().strftime("%H:%M:%S")
            self.log("error", f"connection test failed: {exc}")
            raise SyncError(str(exc)) from exc
        return self.snapshot()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._scan_and_sync_once(allow_sync=True)
            self._stop_event.wait(max(1, self.config.interval_seconds))
        with self._lock:
            self.running = False
            self.stopping = False
            self.current_record = None
            self.current_records.clear()

    def _scan_and_sync_once(self, allow_sync: bool) -> None:
        scan_time = now_text()
        with self._lock:
            self.last_scan_time = datetime.now().strftime("%H:%M:%S")
        try:
            if allow_sync:
                self._sync_root_files()
            details = self._remote_record_details()
            details.sort(key=lambda item: natural_key(item["path"]))
            pruned = self.store.prune_absent_non_synced({item["path"] for item in details})
            if pruned:
                self.log("warn", f"removed {pruned} invalid or disappeared non-synced records")
            self.log("info", f"scan remote {self.config.remote_dir}, found {len(details)} records")
            pending_count = 0
            waiting_count = 0
            ready_to_sync: list[dict[str, Any]] = []
            for detail in details:
                if self._stop_event.is_set():
                    break
                row = self.store.upsert_seen(detail, scan_time)
                if row and row.get("status") != "synced" and self._local_record_matches(detail):
                    self.store.mark_status(detail["path"], "synced", now=row.get("last_sync_at") or scan_time)
                    continue
                age = max(0, time.time() - float(detail["max_mtime"]))
                if age < self.config.settle_seconds:
                    self.store.mark_status(detail["path"], "waiting", error=None)
                    waiting_count += 1
                    continue
                if row and row.get("status") == "synced":
                    continue
                pending_count += 1
                if allow_sync:
                    ready_to_sync.append(detail)
            self.log("info", f"scan summary pending={pending_count}, waiting={waiting_count}")
            if allow_sync and ready_to_sync and not self._stop_event.is_set():
                self._sync_records_concurrently(ready_to_sync)
        except Exception as exc:
            self.log("error", str(exc))

    def _sync_records_concurrently(self, records: list[dict[str, Any]]) -> None:
        workers = max(1, int(self.config.concurrent_syncs))
        self.log("info", f"sync batch records={len(records)}, concurrency={workers}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for record in records:
                if self._stop_event.is_set():
                    break
                futures.append(executor.submit(self._sync_record, record))
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                try:
                    future.result()
                except Exception as exc:
                    self.log("error", f"sync worker failed: {exc}")

    def _is_excluded_path(self, rel_path: str) -> bool:
        parts = Path(rel_path).parts
        for part in parts:
            for pattern in self.config.excludes:
                if fnmatch.fnmatchcase(part.lower(), pattern.lower()):
                    return True
        return False

    def _local_record_matches(self, record: dict[str, Any]) -> bool:
        root = Path(self.config.local_dir).expanduser() / record["path"]
        if not root.is_dir():
            return False
        file_count = 0
        total_size = 0
        try:
            for path in root.rglob("*"):
                rel_path = str(path.relative_to(root))
                if self._is_excluded_path(rel_path):
                    continue
                if not path.is_file():
                    continue
                file_count += 1
                total_size += path.stat().st_size
        except OSError as exc:
            self.log("warn", f"failed to inspect local record {record['path']}: {exc}")
            return False
        return file_count == int(record["file_count"]) and total_size == int(record["total_size"])

    def _sync_root_files(self) -> None:
        local_root = Path(self.config.local_dir).expanduser()
        local_root.mkdir(parents=True, exist_ok=True)
        remote = f"{self.config.remote_host}:{self.config.remote_dir.rstrip('/')}/"
        command: list[str] = []
        if self.config.ssh_password:
            if not shutil.which("sshpass"):
                raise SyncError("已配置 SSH 密码，但系统未安装 sshpass；请安装 sshpass 或配置免密 SSH")
            command.extend(["sshpass", "-p", self.config.ssh_password])
        command.extend(["rsync", "-a", "--partial"])
        for pattern in self.config.excludes:
            command.append(f"--exclude={pattern}")
        command.extend(["--exclude=*/", "-e", self._rsync_ssh_arg(), remote, str(local_root)])
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            error = result.stderr.strip() or f"root file rsync failed with exit code {result.returncode}"
            self.log("error", f"sync root files failed: {error}")
            return
        self.log("ok", "synced root files")

    def _record_public(self, item: dict[str, Any]) -> dict[str, Any]:
        status_map = {
            "pending": ("waiting", "等待同步"),
            "waiting": ("waiting", "等待稳定"),
            "syncing": ("syncing", "同步中"),
            "synced": ("done", "已同步"),
            "failed": ("failed", "失败"),
        }
        badge, text = status_map.get(item["status"], ("waiting", item["status"]))
        return {
            "path": item["path"],
            "status": badge,
            "statusText": text,
            "fileCount": item.get("file_count", 0),
            "totalSize": item.get("total_size", 0),
            "sizeText": display_size(int(item.get("total_size") or 0)),
            "lastSyncAt": item.get("last_sync_at") or "-",
            "error": item.get("error") or "",
            "meta": f"{display_size(int(item.get('total_size') or 0))}，{item.get('file_count', 0)} 个文件",
        }

    def _ssh_base(self) -> list[str]:
        command: list[str] = []
        if self.config.ssh_password:
            if not shutil.which("sshpass"):
                raise SyncError("已配置 SSH 密码，但系统未安装 sshpass；请安装 sshpass 或配置免密 SSH")
            command.extend(["sshpass", "-p", self.config.ssh_password])
        command.extend(["ssh"])
        if self.config.jump_host:
            command.extend(["-J", self.config.jump_host])
        command.extend(["-o", "StrictHostKeyChecking=accept-new", self.config.remote_host])
        return command

    def _run_ssh(self, remote_command: str, timeout: int = 120) -> str:
        result = subprocess.run(
            [*self._ssh_base(), remote_command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise SyncError(result.stderr.strip() or f"ssh failed with exit code {result.returncode}")
        return result.stdout

    def _exclude_find_expr(self) -> str:
        parts = []
        for pattern in self.config.excludes:
            parts.append(f"-iname {shlex.quote(pattern)}")
        return r"\( " + " -o ".join(parts) + r" \)"

    def _remote_records(self) -> list[dict[str, Any]]:
        remote_dir = remote_shell_path(self.config.remote_dir)
        depth = int(self.config.record_depth)
        command = (
            f"find {remote_dir} {self._exclude_find_expr()} -prune -o "
            f"-mindepth {depth} -maxdepth {depth} -type d -printf '%P\\t%T@\\n'"
        )
        rows = []
        for line in self._run_ssh(command).splitlines():
            if not line.strip() or "\t" not in line:
                continue
            path, mtime = line.split("\t", 1)
            rows.append({"path": path.strip("/"), "mtime": float(mtime or 0)})
        rows.sort(key=lambda item: natural_key(item["path"]))
        return rows

    def _remote_record_details(self) -> list[dict[str, Any]]:
        remote_dir = remote_shell_path(self.config.remote_dir)
        depth = int(self.config.record_depth)
        valid_record_paths = {item["path"] for item in self._remote_records()}
        if not valid_record_paths:
            return []
        command = (
            f"find {remote_dir} {self._exclude_find_expr()} -prune -o "
            "-type f -printf '%P\\t%T@\\t%s\\n'"
        )
        records: dict[str, dict[str, Any]] = {}
        for line in self._run_ssh(command).splitlines():
            if not line.strip() or "\t" not in line:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            rel_file, mtime_text, size_text = parts
            rel_parts = [part for part in rel_file.split("/") if part]
            if len(rel_parts) < depth:
                continue
            record_path = "/".join(rel_parts[:depth])
            if record_path not in valid_record_paths:
                continue
            try:
                mtime = float(mtime_text or 0)
                size = int(float(size_text or 0))
            except ValueError:
                continue
            item = records.setdefault(
                record_path,
                {
                    "path": record_path,
                    "file_count": 0,
                    "total_size": 0,
                    "max_mtime": 0.0,
                },
            )
            item["file_count"] += 1
            item["total_size"] += size
            item["max_mtime"] = max(float(item["max_mtime"]), mtime)
        result = list(records.values())
        result.sort(key=lambda item: natural_key(item["path"]))
        return result

    def _remote_record_detail(self, rel_path: str) -> dict[str, Any]:
        remote_path = f"{self.config.remote_dir.rstrip('/')}/{rel_path}"
        command = (
            f"find {remote_shell_path(remote_path)} {self._exclude_find_expr()} -prune -o "
            "-type f -printf '%T@\\t%s\\n' | "
            "awk -F '\\t' '{count+=1; size+=$2; if ($1>mtime) mtime=$1} "
            "END {printf \"%d\\t%d\\t%.6f\\n\", count, size, mtime}'"
        )
        output = self._run_ssh(command).strip()
        count, size, max_mtime = (output.split("\t") + ["0", "0", "0"])[:3]
        return {
            "path": rel_path,
            "file_count": int(float(count or 0)),
            "total_size": int(float(size or 0)),
            "max_mtime": float(max_mtime or 0),
        }

    def _rsync_ssh_arg(self) -> str:
        parts = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
        if self.config.jump_host:
            parts.extend(["-J", self.config.jump_host])
        return shlex.join(parts)

    def _sync_record(self, record: dict[str, Any]) -> None:
        rel_path = record["path"]
        with self._lock:
            self.current_record = rel_path
            self.current_records.add(rel_path)
        self.store.mark_status(rel_path, "syncing")
        self.log("ok", f"start rsync {rel_path}")
        local_root = Path(self.config.local_dir).expanduser()
        final_dir = local_root / rel_path
        target_dir = final_dir if final_dir.exists() else final_dir.with_name(final_dir.name + ".syncing")
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        remote = f"{self.config.remote_host}:{self.config.remote_dir.rstrip('/')}/{rel_path}/"
        command: list[str] = []
        if self.config.ssh_password:
            if not shutil.which("sshpass"):
                raise SyncError("已配置 SSH 密码，但系统未安装 sshpass；请安装 sshpass 或配置免密 SSH")
            command.extend(["sshpass", "-p", self.config.ssh_password])
        command.extend(["rsync", "-a", "--partial", "--human-readable", "--info=progress2"])
        for pattern in self.config.excludes:
            command.append(f"--exclude={pattern}")
        command.extend(["-e", self._rsync_ssh_arg(), remote, str(target_dir)])
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            error = result.stderr.strip() or f"rsync failed with exit code {result.returncode}"
            self.store.mark_status(rel_path, "failed", error=error)
            self.log("error", f"rsync failed {rel_path}: {error}")
            with self._lock:
                self.current_records.discard(rel_path)
                self.current_record = next(iter(self.current_records), None)
            return
        if target_dir != final_dir and not final_dir.exists():
            target_dir.replace(final_dir)
        finished = now_text()
        self.last_sync_time = datetime.now().strftime("%H:%M:%S")
        self.store.mark_status(rel_path, "synced", now=finished)
        self.log("ok", f"synced {rel_path}")
        with self._lock:
            self.current_records.discard(rel_path)
            self.current_record = next(iter(self.current_records), None)


worker = SyncWorker()
