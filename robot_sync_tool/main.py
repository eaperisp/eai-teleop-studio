from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .sync_worker import SyncError, worker


MODULE_DIR = Path(__file__).resolve().parent
STATIC_DIR = MODULE_DIR / "static"

app = FastAPI(title="Robot Sync Tool")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup_event() -> None:
    worker.log("info", "http service started")


@app.middleware("http")
async def access_log_middleware(request: Request, call_next: Any):
    started = time.perf_counter()
    client = request.client.host if request.client else "-"
    status_code = 500
    error = None
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        worker.access_log(
            client=client,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            error=error,
        )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    return worker.snapshot()


@app.post("/api/start")
async def start_sync() -> dict[str, Any]:
    try:
        return worker.start()
    except SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/stop")
async def stop_sync() -> dict[str, Any]:
    return worker.stop()


@app.post("/api/scan")
async def scan_once() -> dict[str, Any]:
    try:
        return worker.scan_once()
    except SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/test-connection")
async def test_connection() -> dict[str, Any]:
    try:
        return worker.test_connection()
    except SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/config")
async def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return worker.save_config(payload.get("config", payload))
    except SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run(
        "robot_sync_tool.main:app",
        host=os.environ.get("ROBOT_SYNC_HOST", "0.0.0.0"),
        port=int(os.environ.get("ROBOT_SYNC_PORT", "18090")),
    )

