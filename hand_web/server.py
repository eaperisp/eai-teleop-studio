#!/usr/bin/env python3
"""Standalone HTTP server for dexterous-hand debugging."""

from __future__ import annotations

import argparse
import json
import mimetypes
import socket
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hand_web.core.models import ValidationError  # noqa: E402
from hand_web.core.service import HandControlService, load_config  # noqa: E402
from teleop.utils.daily_file_logger import DailyFileLogger  # noqa: E402


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
BRAINCO_ASSET_ROOT = PROJECT_ROOT / "assets" / "brainco_hand"
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
STATIC_FILES = {"index.html", "app.js", "hand-preview.js", "styles.css"}


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Reject duplicate listeners instead of sharing a Windows TCP port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class HandWebHandler(BaseHTTPRequestHandler):
    service: HandControlService
    logger: DailyFileLogger

    def do_GET(self) -> None:
        started = time.perf_counter()
        self._last_status = HTTPStatus.OK
        path = urlparse(self.path).path
        error: str | None = None
        try:
            if path == "/api/devices":
                self._json(self.service.devices())
                return
            if path == "/api/status":
                self._json(self.service.status())
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
                result = self.service.connect(payload)
            elif path == "/api/disconnect":
                result = self.service.disconnect()
            elif path == "/api/command":
                result = self.service.command(payload)
            elif path == "/api/stop":
                result = self.service.stop()
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

    def _static(self, path: str) -> None:
        asset_prefix = "/assets/brainco_hand/"
        if path.startswith(asset_prefix):
            relative_path = unquote(path[len(asset_prefix) :])
            self._file(BRAINCO_ASSET_ROOT, relative_path)
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
        if method == "GET" and path == "/api/status" and status < 400 and not error:
            return
        if method == "POST" and path == "/api/command" and self._continuous_command and status < 400 and not error:
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
    parser.add_argument("--port", type=int, default=None, help="HTTP 端口，覆盖 config.json")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="按天保存的平台日志目录")
    args = parser.parse_args()

    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    web_config = config.get("web", {})
    host = args.host or web_config.get("host", "127.0.0.1")
    port = args.port or int(web_config.get("port", 18089))
    log_dir = args.log_dir.expanduser().resolve()

    logger = DailyFileLogger(log_dir, filename_prefix="hand_web")
    service = HandControlService(config_path, logger=logger)
    HandWebHandler.service = service
    HandWebHandler.logger = logger
    try:
        server = ExclusiveThreadingHTTPServer((host, port), HandWebHandler)
    except OSError as exc:
        logger.write("error", "hand web server bind failed", host=host, port=port, error=str(exc))
        raise SystemExit(f"无法监听 http://{host}:{port}，请检查是否已有调试服务运行：{exc}") from exc
    logger.write(
        "info",
        "hand web server started",
        host=host,
        port=port,
        config_file=str(config_path),
        log_dir=str(log_dir),
    )
    print(f"灵巧手调试工具: http://{host}:{port}")
    print("提示: 官方上位机、遥操与本工具不能同时控制同一只灵巧手。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logger.write("info", "hand web server stopping")
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()
