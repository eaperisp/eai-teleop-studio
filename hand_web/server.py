#!/usr/bin/env python3
"""Standalone HTTP server for dexterous-hand debugging."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import ssl
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hand_web.core.models import ValidationError  # noqa: E402
from hand_web.core.pose_store import PoseStore  # noqa: E402
from hand_web.core.service import HandControlService, load_config  # noqa: E402
from hand_web.vision import VisionManager  # noqa: E402
from teleop.utils.daily_file_logger import DailyFileLogger  # noqa: E402


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
BRAINCO_ASSET_ROOT = PROJECT_ROOT / "assets" / "brainco_hand"
INSPIRE_ASSET_ROOT = PROJECT_ROOT / "assets" / "inspire_hand"
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
HAND_WEB_PORT = 18089
ROBOT_SYNC_PORT = 18090
INSTANCE_LOCK_PATH = PROJECT_ROOT / "logs" / "app" / ".hand-web.lock"
STATIC_FILES = {"index.html", "app.js", "hand-preview.js", "styles.css"}


class SingleInstanceLock:
    """Prevent debug servers on different HTTP ports from sharing one hand."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if lock_file.tell() == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            lock_file.close()
            raise RuntimeError("已有灵巧手调试服务运行，请关闭旧服务后再启动") from exc
        self._file = lock_file

    def release(self) -> None:
        lock_file, self._file = self._file, None
        if lock_file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def validate_hand_web_port(port: int) -> int:
    if port != HAND_WEB_PORT:
        detail = "；18090 已保留给数据同步服务" if port == ROBOT_SYNC_PORT else ""
        raise ValueError(f"灵巧手调试工具固定使用端口 {HAND_WEB_PORT}{detail}")
    return port


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Reject duplicate listeners instead of sharing a Windows TCP port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class HandWebHandler(BaseHTTPRequestHandler):
    service: HandControlService
    vision: VisionManager
    poses: PoseStore
    logger: DailyFileLogger

    def do_GET(self) -> None:
        started = time.perf_counter()
        self._last_status = HTTPStatus.OK
        parsed = urlparse(self.path)
        path = parsed.path
        error: str | None = None
        try:
            if path == "/api/devices":
                self._json(self.service.devices())
                return
            if path == "/api/status":
                self._json(self.service.status())
                return
            if path == "/api/poses":
                device_id = (parse_qs(parsed.query).get("device_id") or [""])[0]
                self._json(self.poses.list(device_id))
                return
            if path == "/api/vision/status":
                self._json(self.vision.status())
                return
            if path == "/api/vision/calibration":
                query = parse_qs(parsed.query)
                device_id = (query.get("device_id") or [self.service.config.get("default_device", "brainco_revo2")])[0]
                side = (query.get("side") or ["right"])[0]
                self._json(self.vision.calibration_status(str(device_id), str(side)))
                return
            if path == "/api/vision/frame":
                frame = self.vision.frame()
                if frame is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                else:
                    self._binary(frame, "image/jpeg")
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._static(path)
        except ValidationError as exc:
            error = str(exc)
            self.logger.write(
                "warning",
                "http get validation failed",
                path=path,
                client=self.client_address[0],
                error=error,
            )
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            error = str(exc)
            self._report_error(exc)
        finally:
            self._log_access("GET", path, started, error=error)

    def do_POST(self) -> None:
        started = time.perf_counter()
        self._last_status = HTTPStatus.OK
        self._continuous_command = False
        path = urlparse(self.path).path
        error: str | None = None
        try:
            if path == "/api/vision/frame":
                self._continuous_command = True
                result = self.vision.submit_frame(self._read_binary(2 * 1024 * 1024))
                self._json(result)
                return

            payload = self._read_json()
            self._continuous_command = path == "/api/command" and payload.get("continuous") is True
            if not self._continuous_command:
                self.logger.write(
                    "info",
                    "http post received",
                    path=path,
                    client=self.client_address[0],
                )
            if path == "/api/connect":
                self._require_vision_stopped()
                result = self.service.connect(payload)
            elif path == "/api/disconnect":
                self._require_vision_stopped()
                result = self.service.disconnect()
            elif path == "/api/command":
                payload["source"] = "manual"
                result = self.service.command(payload)
            elif path == "/api/stop":
                result = self.vision.stop() if self.vision.status()["running"] else self.service.stop()
            elif path == "/api/vision/start":
                result = self.vision.start(payload)
            elif path == "/api/vision/stop":
                result = self.vision.stop()
            elif path == "/api/vision/calibration/capture":
                result = self.vision.capture_calibration(payload)
            elif path == "/api/vision/calibration/reset":
                result = self.vision.reset_calibration(payload)
            elif path == "/api/poses/save":
                result = self.poses.save(payload)
            elif path == "/api/poses/delete":
                result = self.poses.delete(payload)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(result)
        except (ValidationError, ValueError) as exc:
            error = str(exc)
            self.logger.write(
                "warning",
                "http post validation failed",
                path=path,
                client=self.client_address[0],
                error=error,
            )
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            error = str(exc)
            self.logger.write(
                "warning",
                "http post operation failed",
                path=path,
                client=self.client_address[0],
                error=error,
            )
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            error = str(exc)
            self._report_error(exc)
        finally:
            self._log_access("POST", path, started, error=error)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("Content-Length 不正确") from exc
        if length < 0 or length > 64 * 1024:
            raise ValidationError("请求内容过大")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("请求不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("请求根节点必须是对象")
        return payload

    def _read_binary(self, maximum: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("Content-Length 不正确") from exc
        if length <= 0:
            raise ValidationError("摄像头帧为空")
        if length > maximum:
            raise ValidationError("摄像头帧过大")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"image/jpeg", "image/webp"}:
            raise ValidationError("摄像头帧必须为 JPEG 或 WebP")
        return self.rfile.read(length)

    def _require_vision_stopped(self) -> None:
        if self.vision.status()["running"]:
            raise RuntimeError("请先停止视觉控制")

    def _static(self, path: str) -> None:
        asset_roots = {
            "/assets/brainco_hand/": BRAINCO_ASSET_ROOT,
            "/assets/inspire_hand/": INSPIRE_ASSET_ROOT,
        }
        for asset_prefix, asset_root in asset_roots.items():
            if path.startswith(asset_prefix):
                relative_path = unquote(path[len(asset_prefix) :])
                self._file(asset_root, relative_path)
                return

        filename = "index.html" if path == "/" else path.removeprefix("/")
        if filename not in STATIC_FILES:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._file(STATIC_ROOT, filename)

    def _file(self, root: Path, relative_path: str) -> None:
        root = root.resolve()
        file_path = (root / relative_path).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = file_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix.lower() == ".urdf":
            content_type = "application/xml"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._last_status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, body: bytes, content_type: str) -> None:
        self._last_status = HTTPStatus.OK
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _report_error(self, exc: Exception) -> None:
        self.logger.write(
            "error",
            "http request failed",
            path=urlparse(self.path).path,
            client=self.client_address[0],
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
        )
        self._json(
            {"ok": False, "error": str(exc) or type(exc).__name__},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._last_status = HTTPStatus(code)
        super().send_error(code, message, explain)

    def _log_access(self, method: str, path: str, started: float, *, error: str | None = None) -> None:
        status = int(getattr(self, "_last_status", HTTPStatus.OK))
        if method == "GET" and path in {"/api/status", "/api/vision/status", "/api/vision/frame", "/api/vision/calibration"} and status < 400 and not error:
            return
        if method == "POST" and path in {"/api/command", "/api/vision/frame"} and self._continuous_command and status < 400 and not error:
            return
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        level = "error" if status >= 500 else "warning" if status >= 400 else "access"
        fields: dict[str, Any] = {
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "client": self.client_address[0],
        }
        if error:
            fields["error"] = error
        self.logger.write(level, "http request", **fields)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="灵巧手调试工具")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="配置文件")
    parser.add_argument("--host", default=None, help="监听地址，覆盖 config.json")
    parser.add_argument("--port", type=int, default=None, help="HTTP 端口，必须为 18089")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="按天保存的平台日志目录")
    parser.add_argument("--scheme", choices=("http", "https"), default=None, help="监听协议")
    parser.add_argument("--cert", type=Path, default=None, help="HTTPS 证书")
    parser.add_argument("--key", type=Path, default=None, help="HTTPS 私钥")
    args = parser.parse_args()

    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    web_config = config.get("web", {})
    host = args.host or web_config.get("host", "127.0.0.1")
    requested_port = args.port if args.port is not None else int(web_config.get("port", HAND_WEB_PORT))
    try:
        port = validate_hand_web_port(requested_port)
    except ValueError as exc:
        parser.error(str(exc))
    log_dir = args.log_dir.expanduser().resolve()
    scheme = args.scheme or os.environ.get("HAND_WEB_SCHEME", "http").strip().lower()
    if scheme not in {"http", "https"}:
        parser.error("HAND_WEB_SCHEME 必须是 http 或 https")
    cert_value = args.cert or os.environ.get("HAND_WEB_CERT")
    key_value = args.key or os.environ.get("HAND_WEB_KEY")
    cert_path = Path(cert_value).expanduser().resolve() if cert_value else None
    key_path = Path(key_value).expanduser().resolve() if key_value else None
    if scheme == "https" and (
        cert_path is None or key_path is None or not cert_path.is_file() or not key_path.is_file()
    ):
        parser.error("HTTPS 需要有效的 HAND_WEB_CERT 和 HAND_WEB_KEY")

    logger = DailyFileLogger(log_dir, filename_prefix="hand_web")
    instance_lock = SingleInstanceLock(INSTANCE_LOCK_PATH)
    try:
        instance_lock.acquire()
    except RuntimeError as exc:
        logger.write("error", "hand web single instance check failed", error=str(exc))
        raise SystemExit(str(exc)) from exc
    service = HandControlService(config_path, logger=logger)
    vision = VisionManager(service, logger=logger)
    poses = PoseStore(config_path.with_name("poses.json"), logger=logger)
    HandWebHandler.service = service
    HandWebHandler.vision = vision
    HandWebHandler.poses = poses
    HandWebHandler.logger = logger
    try:
        server = ExclusiveThreadingHTTPServer((host, port), HandWebHandler)
        if scheme == "https":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
    except OSError as exc:
        logger.write("error", "hand web server bind failed", host=host, port=port, error=str(exc))
        service.close()
        instance_lock.release()
        raise SystemExit(f"无法监听 {scheme}://{host}:{port}，请检查是否已有调试服务运行：{exc}") from exc
    logger.write(
        "info",
        "hand web server started",
        host=host,
        port=port,
        scheme=scheme,
        config_file=str(config_path),
        operation_log=str(logger.path_for_today()),
    )
    print(f"灵巧手调试工具: {scheme}://{host}:{port}")
    print(f"操作日志: {logger.path_for_today()}")
    print("提示: 官方上位机、遥操与本工具不能同时控制同一只灵巧手。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logger.write("info", "hand web server stopping")
        vision.close()
        service.close()
        server.server_close()
        instance_lock.release()


if __name__ == "__main__":
    main()
