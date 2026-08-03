#!/usr/bin/env python3
"""HTTP API wrapper for the H2 OpenPI switch-flip VLA task."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_BIN = "/home/robot/miniconda3/envs/teleop/bin/python3"
SUPPORTED_CLOSE_TO_REMOTE = "Change the switch from close to remote"
SUPPORTED_REMOTE_TO_CLOSE = "Change the switch from remote to close"
SUPPORTED_LANGUAGES = [SUPPORTED_CLOSE_TO_REMOTE, SUPPORTED_REMOTE_TO_CLOSE]
APP_LOG_ROOT = PROJECT_ROOT / "logs" / "app"
TASK_LOG_ROOT = PROJECT_ROOT / "logs" / "tasks" / "h2_switch_flip_api"
MAX_TASK_LOG_DIRS = 20
LOGGER = logging.getLogger("h2_switch_flip_api")

ERROR_CODES = {
    0: "OK",
    1: "NOT_IMPLEMENTED",
    2: "PRECONDITION",
    3: "ALIGN_FAILED",
    4: "MEASURE_FAILED",
    5: "YOLO_FAILED",
    6: "IK_FAILED",
    7: "EXEC_FAILED",
    8: "VERIFY_FAILED",
    9: "ABORTED",
    10: "POSE_UNAVAILABLE",
    -1: "DISPATCH_ERROR",
}


@dataclass
class TaskState:
    state: str = "idle"
    task_id: str | None = None
    language: str | None = None
    retries: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    process: subprocess.Popen[str] | None = None
    log_jsonl: str | None = None
    debug_image_dir: str | None = None
    stdout_log: str | None = None
    task_log_dir: str | None = None
    abort_requested: bool = False


STATE = TaskState()
STATE_LOCK = threading.RLock()


def now_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def result_payload(code: int, message: str, **detail: Any) -> dict[str, Any]:
    return {
        "ok": code == 0,
        "code": code,
        "code_name": ERROR_CODES.get(code, "UNKNOWN"),
        "message": message,
        "detail": detail,
    }


def append_log(line: str) -> None:
    with STATE_LOCK:
        STATE.log.append(line.rstrip())


def resolve_project_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return resolved


def setup_logging(log_file: str | Path) -> Path:
    log_path = resolve_project_path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)
    return log_path


def task_snapshot() -> dict[str, Any]:
    with STATE_LOCK:
        running_pid = STATE.process.pid if STATE.process is not None and STATE.process.poll() is None else None
        return {
            "ok": True,
            "state": STATE.state,
            "task_id": STATE.task_id,
            "language": STATE.language,
            "retries": STATE.retries,
            "started_at": now_iso(STATE.started_at),
            "finished_at": now_iso(STATE.finished_at),
            "result": STATE.result,
            "log": list(STATE.log),
            "process": {"pid": running_pid},
            "artifacts": {
                "task_log_dir": STATE.task_log_dir,
                "log_jsonl": STATE.log_jsonl,
                "debug_image_dir": STATE.debug_image_dir,
                "stdout_log": STATE.stdout_log,
            },
        }


def cleanup_task_logs() -> None:
    TASK_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    task_dirs = [
        path
        for path in TASK_LOG_ROOT.glob("*/*")
        if path.is_dir()
    ]
    task_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for old_dir in task_dirs[MAX_TASK_LOG_DIRS:]:
        try:
            shutil.rmtree(old_dir)
            LOGGER.info("removed old task log dir: %s", old_dir)
        except OSError as exc:
            append_log(f"failed to remove old task log dir {old_dir}: {exc!r}")
            LOGGER.warning("failed to remove old task log dir %s: %r", old_dir, exc)
    for date_dir in TASK_LOG_ROOT.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            next(date_dir.iterdir())
        except StopIteration:
            date_dir.rmdir()
        except OSError:
            pass


def build_vla_command(language: str, task_id: str) -> tuple[list[str], str, str, str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    date_dir = datetime.now().strftime("%Y-%m-%d")
    short_id = task_id.split("-", 1)[0]
    task_log_dir = f"logs/tasks/h2_switch_flip_api/{date_dir}/{timestamp}_{short_id}"
    log_jsonl = f"{task_log_dir}/vla.jsonl"
    debug_image_dir = f"{task_log_dir}/debug_images"
    stdout_log = f"{task_log_dir}/stdout.out"

    cmd = [
        PYTHON_BIN,
        "tools/h2_openpi_dataset_image_vla.py",
        "--image-source",
        "camera",
        "--server",
        "http://192.168.61.228:8080",
        "--instruction",
        language,
        "--network-interface",
        "enp86s0",
        "--img-server-ip",
        "127.0.0.1",
        "--image-camera",
        "head_camera",
        "--left-wrist-camera",
        "left_wrist_camera",
        "--right-wrist-camera",
        "right_wrist_camera",
        "--left-wrist-static-image",
        "/home/robot/data/datasets/robot/h2_switch_close_to_remote/episode_0001/colors/000000_color_1.jpg",
        "--missing-camera-policy",
        "error",
        "--steps",
        "200",
        "--action-horizon",
        "16",
        "--exe-steps",
        "16",
        "--control-freq",
        "14",
        "--control-arm",
        "right",
        "--state-tail-zeros",
        "2",
        "--extra-action-dims-policy",
        "crop",
        "--max-command-delta",
        "0.10",
        "--max-command-velocity",
        "0.45",
        "--reject-action-delta",
        "0",
        "--gravity-model-cache",
        "/home/robot/eai-teleop-studio/models/h2_model_cache.pkl",
        "--gravity-ramp-seconds",
        "3.0",
        "--arm-feedback-gain",
        "0.16",
        "--arm-feedback-ki",
        "0.04",
        "--arm-feedback-max-offset",
        "0.05",
        "--arm-feedback-integral-zone",
        "0.15",
        "--arm-feedback-max-integral",
        "0.012",
        "--pre-vla-trajectory-csv",
        "",
        "--restore-pose-file",
        "config/h2_pose_init.json",
        "--restore-duration",
        "6",
        "--debug-image-dir",
        debug_image_dir,
        "--log-jsonl",
        log_jsonl,
        "--execute",
        "--confirm-execute",
    ]
    return cmd, task_log_dir, log_jsonl, debug_image_dir, stdout_log


def classify_failure(exit_code: int, output_tail: list[str]) -> dict[str, Any]:
    text = "\n".join(output_tail[-60:]).lower()
    if "gravity model cache not found" in text:
        return result_payload(2, "重力补偿模型文件不存在", exit_code=exit_code)
    if "failed to read image" in text or "missing camera" in text or "camera" in text and "timeout" in text:
        return result_payload(2, "相机或静态图片输入不可用", exit_code=exit_code)
    if "pose" in text and ("not found" in text or "unavailable" in text):
        return result_payload(10, "恢复位姿文件不可用", exit_code=exit_code)
    return result_payload(7, "VLA 执行失败", exit_code=exit_code)


def run_task(language: str, retries: int, task_id: str) -> None:
    cmd, task_log_dir, log_jsonl, debug_image_dir, stdout_log = build_vla_command(language, task_id)
    stdout_path = PROJECT_ROOT / stdout_log
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    cleanup_task_logs()
    LOGGER.info("task %s starting: language=%r retries=%s log_dir=%s", task_id, language, retries, stdout_path.parent)

    with STATE_LOCK:
        STATE.state = "starting"
        STATE.task_log_dir = str(PROJECT_ROOT / task_log_dir)
        STATE.log_jsonl = str(PROJECT_ROOT / log_jsonl)
        STATE.debug_image_dir = str(PROJECT_ROOT / debug_image_dir)
        STATE.stdout_log = str(stdout_path)
        STATE.log.clear()
        STATE.log.append("starting VLA subprocess")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:  # noqa: BLE001
        with STATE_LOCK:
            STATE.state = "done"
            STATE.finished_at = time.time()
            STATE.result = result_payload(-1, "启动 VLA 子进程失败", error=repr(exc))
            STATE.process = None
            STATE.log.append(f"dispatch error: {exc!r}")
        LOGGER.exception("task %s dispatch failed", task_id)
        return

    with STATE_LOCK:
        STATE.state = "running"
        STATE.process = proc
        STATE.log.append(f"pid={proc.pid}")
    LOGGER.info("task %s subprocess started: pid=%s", task_id, proc.pid)

    tail: list[str] = []
    try:
        with stdout_path.open("w", encoding="utf-8") as out_file:
            if proc.stdout is not None:
                for line in proc.stdout:
                    tail.append(line.rstrip())
                    tail = tail[-200:]
                    out_file.write(line)
                    out_file.flush()
                    append_log(line)
        exit_code = proc.wait()
    except Exception as exc:  # noqa: BLE001
        append_log(f"reader error: {exc!r}")
        exit_code = proc.poll()
        if exit_code is None:
            exit_code = -999

    with STATE_LOCK:
        aborted = STATE.abort_requested
        STATE.state = "done"
        STATE.finished_at = time.time()
        STATE.process = None
        if aborted:
            STATE.result = result_payload(9, "任务已中止", exit_code=exit_code)
        elif exit_code == 0:
            STATE.result = result_payload(
                0,
                "VLA 执行完成",
                exit_code=exit_code,
                task_log_dir=STATE.task_log_dir,
                log_jsonl=STATE.log_jsonl,
                debug_image_dir=STATE.debug_image_dir,
                stdout_log=STATE.stdout_log,
            )
        else:
            STATE.result = classify_failure(exit_code, tail)
        result = STATE.result
    LOGGER.info("task %s finished: exit_code=%s result=%s", task_id, exit_code, result)


def abort_running_task() -> tuple[bool, dict[str, Any], int]:
    with STATE_LOCK:
        proc = STATE.process
        if STATE.state not in {"starting", "running"} or proc is None or proc.poll() is not None:
            return False, {"ok": False, "error": "no running task"}, HTTPStatus.CONFLICT
        STATE.abort_requested = True
        pid = proc.pid
        STATE.log.append(f"abort requested for pid={pid}")
    LOGGER.warning("abort requested for task_id=%s pid=%s", STATE.task_id, pid)

    try:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:  # noqa: BLE001
        append_log(f"abort error: {exc!r}")
        LOGGER.exception("abort failed")

    return True, {"ok": True, "task_id": STATE.task_id, "state": STATE.state}, HTTPStatus.OK


class Handler(BaseHTTPRequestHandler):
    server_version = "H2SwitchFlipAPI/0.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/task/status":
            self._send_json(HTTPStatus.OK, task_snapshot())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/task/abort":
            _ok, payload, status = abort_running_task()
            self._send_json(status, payload)
            return
        if path != "/task/flip":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        try:
            payload = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid json: {exc}"})
            return

        language = str(payload.get("language", "")).strip()
        retries_raw = payload.get("retries", 3)
        try:
            retries = int(retries_raw)
        except Exception:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": "retries must be int"})
            return
        if retries < 1 or retries > 20:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": "retries must be 1..20"})
            return

        if language == SUPPORTED_REMOTE_TO_CLOSE:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "ok": False,
                    "code": 1,
                    "code_name": "NOT_IMPLEMENTED",
                    "message": "remote to close is not implemented",
                    "error": "remote to close is not implemented",
                    "supported": SUPPORTED_LANGUAGES,
                },
            )
            return
        if language != SUPPORTED_CLOSE_TO_REMOTE:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "ok": False,
                    "error": f"unsupported language: {language!r}",
                    "supported": SUPPORTED_LANGUAGES,
                },
            )
            return

        with STATE_LOCK:
            if STATE.state in {"starting", "running"}:
                LOGGER.warning("reject task request because task is running: task_id=%s", STATE.task_id)
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "error": "task already running", "task_id": STATE.task_id, "state": STATE.state},
                )
                return

            task_id = uuid.uuid4().hex
            STATE.state = "starting"
            STATE.task_id = task_id
            STATE.language = language
            STATE.retries = retries
            STATE.started_at = time.time()
            STATE.finished_at = None
            STATE.result = None
            STATE.process = None
            STATE.log_jsonl = None
            STATE.debug_image_dir = None
            STATE.stdout_log = None
            STATE.task_log_dir = None
            STATE.abort_requested = False
            STATE.log.clear()

        thread = threading.Thread(target=run_task, args=(language, retries, task_id), daemon=True)
        thread.start()
        LOGGER.info("accepted task request: task_id=%s language=%r retries=%s", task_id, language, retries)
        self._send_json(HTTPStatus.OK, {"ok": True, "task_id": task_id})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("[HTTP] %s %s", self.address_string(), fmt % args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17001)
    parser.add_argument("--log-file", default="", help="Service runtime log file. Relative paths are under project root.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_file = args.log_file or APP_LOG_ROOT / f"h2_switch_flip_api_{args.port}.log"
    log_path = setup_logging(log_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info("H2 switch flip API listening on http://%s:%s", args.host, args.port)
    LOGGER.info("project root: %s", PROJECT_ROOT)
    LOGGER.info("service log: %s", log_path)
    LOGGER.info("task log root: %s", TASK_LOG_ROOT)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
