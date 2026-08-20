"""Lifecycle and safety coordinator for browser-visible camera control."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from hand_web.core.models import ValidationError
from hand_web.core.service import HandControlService
from hand_web.vision.calibration import CALIBRATION_STAGES, VisionCalibrationStore
from hand_web.vision.mappers import create_mapper
from hand_web.vision.mediapipe_runtime import MediaPipeRuntime
from teleop.utils.daily_file_logger import DailyFileLogger


RuntimeFactory = Callable[[dict[str, Any]], Any]


class VisionManager:
    def __init__(
        self,
        service: HandControlService,
        logger: DailyFileLogger | None = None,
        runtime_factory: RuntimeFactory = MediaPipeRuntime,
        calibration_path: Path | None = None,
    ) -> None:
        self.service = service
        self.logger = logger
        self.runtime_factory = runtime_factory
        self.calibrations = VisionCalibrationStore(
            calibration_path or service.config_path.with_name("vision_calibration.json"),
            logger=logger,
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._starting = False
        self._running = False
        self._stopping = False
        self._dry_run = False
        self._source = "browser"
        self._side = "right"
        self._device_id: str | None = None
        self._started_at: float | None = None
        self._last_frame_at: float | None = None
        self._last_input_at: float | None = None
        self._last_detection_at: float | None = None
        self._frame: bytes | None = None
        self._positions: list[float] | None = None
        self._gesture: str | None = None
        self._features: dict[str, float] | None = None
        self._tracking_space = "none"
        self._error = ""
        self._frame_count = 0
        self._detection_count = 0
        self._command_count = 0
        self._available: bool | None = None
        self._mapper: Any = None
        self._runtime: Any = None
        self._calibration_profile: dict[str, Any] | None = None
        self._calibration_stage: str | None = None
        self._calibration_samples: list[dict[str, float]] = []
        self._calibration_target = 24
        self._calibration_captures: dict[str, dict[str, float]] = {}
        self._calibration_error = ""

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._starting or self._running or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("视觉控制已经在运行")
            self._starting = True
        try:
            return self._start(payload)
        finally:
            with self._lock:
                self._starting = False

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = dict(self.service.config.get("vision", {}))
        status = self.service.status()
        device_id = str(payload.get("device_id") or status.get("device_id") or self.service.config.get("default_device"))
        device_overrides = config.pop("devices", {})
        if isinstance(device_overrides, dict):
            selected_overrides = device_overrides.get(device_id, {})
            if isinstance(selected_overrides, dict):
                config.update(selected_overrides)
        for key in (
            "source", "camera", "side", "width", "height", "preview_fps", "min_detection_confidence",
            "min_tracking_confidence", "filter_min_cutoff", "filter_beta", "filter_derivative_cutoff",
            "max_velocity", "endpoint_snap", "deadband", "change_threshold", "min_interval",
            "duration_ms", "warmup_frames", "lost_timeout", "joint_limits", "calibration_samples",
        ):
            if key in payload:
                config[key] = payload[key]
        config["side"] = str(config.get("side", "right")).lower()
        if config["side"] not in {"left", "right"}:
            raise ValidationError("side 必须是 left 或 right")
        config["source"] = str(config.get("source", "browser")).strip().lower()
        if config["source"] not in {"browser", "server"}:
            raise ValidationError("source 必须是 browser 或 server")
        try:
            config["camera"] = int(config.get("camera", 0))
            config["duration_ms"] = max(50, min(2000, int(config.get("duration_ms", 180))))
        except (TypeError, ValueError) as exc:
            raise ValidationError("camera 和 duration_ms 必须是整数") from exc
        dry_run_value = payload.get("dry_run", True)
        if not isinstance(dry_run_value, bool):
            raise ValidationError("dry_run 必须是布尔值")
        dry_run = dry_run_value
        if not dry_run and payload.get("control_enabled") is not True:
            raise ValidationError("启动视觉控制需要明确确认 control_enabled=true")
        if not dry_run:
            if not status.get("connected"):
                raise ValidationError("请先连接灵巧手，或启用仅识别模式")
            hand = (status.get("hands") or {}).get(config["side"])
            if hand is None:
                raise ValidationError(f"当前连接未启用{'左' if config['side'] == 'left' else '右'}手")

        profile = self.calibrations.profile(device_id, config["side"])
        mapper = create_mapper(device_id, config, profile)
        runtime = self.runtime_factory(config)
        if not dry_run:
            try:
                self.service.acquire_control("vision")
            except Exception:
                runtime.close()
                raise

        with self._lock:
            self._stop_event.clear()
            self._running = True
            self._stopping = False
            self._dry_run = dry_run
            self._source = config["source"]
            self._side = config["side"]
            self._device_id = device_id
            self._started_at = time.time()
            self._last_frame_at = None
            self._last_input_at = None
            self._last_detection_at = None
            self._frame = None
            self._positions = None
            self._gesture = None
            self._features = None
            self._tracking_space = "none"
            self._error = ""
            self._frame_count = self._detection_count = self._command_count = 0
            self._mapper = mapper
            self._runtime = runtime
            self._calibration_profile = profile
            self._calibration_target = max(12, min(90, int(config.get("calibration_samples", 24))))
            self._calibration_stage = None
            self._calibration_samples = []
            self._calibration_captures = {}
            self._calibration_error = ""
            self._thread = threading.Thread(
                target=self._run,
                args=(runtime, mapper, config),
                name="hand-vision-control",
                daemon=True,
            )
            self._thread.start()
            self._starting = False
        self._log(
            "info", "vision control started", device_id=device_id, side=self._side,
            dry_run=dry_run, source=self._source,
            camera=config["camera"] if self._source == "server" else None,
        )
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._starting:
                raise RuntimeError("摄像头正在启动，请稍后再试")
            thread = self._thread
            if not self._running and (thread is None or not thread.is_alive()):
                return {**self.status(), "message": "视觉控制未运行"}
            self._stopping = True
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            if thread is not None and thread.is_alive():
                raise RuntimeError("视觉控制正在停止，请稍后再试")
            result = self.status()
        return {**result, "message": "视觉控制已停止"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "available": self._dependencies_available(),
                "starting": self._starting,
                "running": self._running,
                "stopping": self._stopping,
                "dry_run": self._dry_run,
                "source": self._source,
                "side": self._side,
                "device_id": self._device_id,
                "started_at": self._started_at,
                "last_frame_at": self._last_frame_at,
                "last_detection_at": self._last_detection_at,
                "positions": self._positions,
                "features": self._features,
                "tracking_space": self._tracking_space,
                "gesture": self._gesture,
                "error": self._error,
                "frames": self._frame_count,
                "detections": self._detection_count,
                "commands": self._command_count,
                "calibration": self._calibration_status_locked(),
            }

    def calibration_status(self, device_id: str, side: str) -> dict[str, Any]:
        device_id = str(device_id or "").strip()
        side = str(side or "").strip().lower()
        with self._lock:
            active = device_id == self._device_id and side == self._side
            if active:
                calibration = self._calibration_status_locked()
            else:
                profile = self.calibrations.profile(device_id, side)
                calibration = {
                    "profile": profile,
                    "profile_available": profile is not None,
                    "active_stage": None,
                    "sample_count": 0,
                    "sample_target": self._calibration_target,
                    "completed_stages": [],
                    "error": "",
                }
        return {"ok": True, "device_id": device_id, "side": side, **calibration}

    def capture_calibration(self, payload: dict[str, Any]) -> dict[str, Any]:
        stage = str(payload.get("stage") or "").strip()
        if stage not in CALIBRATION_STAGES:
            raise ValidationError(f"未知标定步骤: {stage}")
        try:
            sample_target = max(12, min(90, int(payload.get("sample_count", 24))))
        except (TypeError, ValueError) as exc:
            raise ValidationError("sample_count 必须是整数") from exc
        with self._lock:
            if not self._running or not self._dry_run:
                raise RuntimeError("请先以“仅识别”模式启动摄像头")
            if self._calibration_stage is not None:
                raise RuntimeError("当前标定步骤仍在采集中")
            self._calibration_stage = stage
            self._calibration_samples = []
            self._calibration_target = sample_target
            self._calibration_error = ""
            result = self._calibration_status_locked()
        self._log("info", "vision calibration capture started", stage=stage, sample_target=sample_target)
        return {"ok": True, **result}

    def reset_calibration(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id") or self._device_id or self.service.config.get("default_device"))
        side = str(payload.get("side") or self._side or "right").lower()
        self.calibrations.reset(device_id, side)
        with self._lock:
            if device_id == self._device_id and side == self._side:
                self._calibration_profile = None
                self._calibration_stage = None
                self._calibration_samples = []
                self._calibration_captures = {}
                self._calibration_error = ""
                if self._mapper is not None:
                    self._mapper.set_profile(None)
            result = self._calibration_status_locked()
        return {"ok": True, "device_id": device_id, "side": side, **result}

    def frame(self) -> bytes | None:
        with self._lock:
            return self._frame

    def submit_frame(self, payload: bytes) -> dict[str, Any]:
        with self._lock:
            if not self._running or self._source != "browser" or self._runtime is None:
                raise RuntimeError("浏览器视觉控制未运行")
            runtime = self._runtime
            self._last_input_at = time.time()
        runtime.submit_frame(payload)
        return {"ok": True}

    def close(self) -> None:
        try:
            self.stop()
        except Exception as exc:
            self._log("warning", "vision control close failed", error=str(exc))

    def _run(self, runtime: Any, mapper: Any, config: dict[str, Any]) -> None:
        last_sent: list[float] | None = None
        last_sent_at = 0.0
        last_preview_at = 0.0
        detected_frames = 0
        motion_sent = False
        loss_stopped = False
        try:
            while not self._stop_event.is_set():
                frame, detection = runtime.read()
                now = time.monotonic()
                if frame is None:
                    with self._lock:
                        last_input_at = self._last_input_at
                    if (
                        not self._dry_run and motion_sent and not loss_stopped
                        and last_input_at is not None
                        and time.time() - last_input_at >= float(config.get("lost_timeout", 0.6))
                    ):
                        self.service.stop("vision")
                        loss_stopped = True
                        last_sent = None
                    continue
                positions = None
                if detection is not None:
                    detected_frames += 1
                    tracking_landmarks = detection.world_landmarks or detection.landmarks
                    tracking_space = "world" if detection.world_landmarks is not None else "normalized"
                    positions = mapper.map(tracking_landmarks, detection.gesture, now)
                    features = mapper.last_features.to_dict() if mapper.last_features is not None else None
                    if features is not None:
                        self._collect_calibration(features, mapper)
                    loss_stopped = False
                    with self._lock:
                        self._last_detection_at = time.time()
                        self._detection_count += 1
                        self._gesture = detection.gesture
                        self._positions = positions
                        self._features = features
                        self._tracking_space = tracking_space
                    should_send = (
                        not self._dry_run
                        and detected_frames >= max(1, int(config.get("warmup_frames", 5)))
                        and now - last_sent_at >= max(0.05, float(config.get("min_interval", 0.12)))
                        and (
                            last_sent is None
                            or any(abs(a - b) >= float(config.get("change_threshold", 0.01)) for a, b in zip(positions, last_sent))
                        )
                    )
                    if should_send:
                        self.service.command({
                            "side": self._side,
                            "positions": positions,
                            "duration_ms": config["duration_ms"],
                            "continuous": True,
                            "source": "vision",
                        })
                        last_sent = positions[:]
                        last_sent_at = now
                        motion_sent = True
                        with self._lock:
                            self._command_count += 1
                else:
                    detected_frames = 0
                    mapper.reset()
                    with self._lock:
                        self._gesture = None
                        self._features = None
                        self._tracking_space = "none"
                        last_detection_at = self._last_detection_at
                    if (
                        not self._dry_run and motion_sent and not loss_stopped and last_detection_at is not None
                        and time.time() - last_detection_at >= float(config.get("lost_timeout", 0.6))
                    ):
                        self.service.stop("vision")
                        loss_stopped = True
                        last_sent = None

                preview_interval = 1.0 / max(1.0, float(config.get("preview_fps", 10)))
                if now - last_preview_at >= preview_interval:
                    lines = [
                        f"{self._side.title()} hand: {detection.gesture if detection else 'Not detected'}",
                        f"Mode: {'recognition only' if self._dry_run else 'controlling'}",
                    ]
                    if positions is not None:
                        lines.append("Joints: " + " ".join(f"{value:.2f}" for value in positions))
                    encoded = runtime.encode(frame, detection, lines)
                    with self._lock:
                        self._frame = encoded
                        self._last_frame_at = time.time()
                        self._frame_count += 1
                    last_preview_at = now
        except Exception as exc:
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            self._log("error", "vision control failed", error=str(exc), error_type=type(exc).__name__)
        finally:
            try:
                if not self._dry_run and motion_sent:
                    self.service.stop("vision")
            except Exception as exc:
                self._log("warning", "vision final stop failed", error=str(exc))
            try:
                runtime.close()
            finally:
                if not self._dry_run:
                    self.service.release_control("vision")
                with self._lock:
                    self._running = False
                    self._stopping = False
                    self._mapper = None
                    self._runtime = None
                self._log(
                    "info", "vision control stopped", frames=self._frame_count,
                    detections=self._detection_count, commands=self._command_count, error=self._error,
                )

    def _collect_calibration(self, features: dict[str, float], mapper: Any) -> None:
        with self._lock:
            stage = self._calibration_stage
            if stage is None:
                return
            self._calibration_samples.append(features)
            if len(self._calibration_samples) < self._calibration_target:
                return
            capture = self.calibrations.median_sample(self._calibration_samples)
            self._calibration_captures[stage] = capture
            self._calibration_stage = None
            self._calibration_samples = []
            if all(item in self._calibration_captures for item in CALIBRATION_STAGES):
                try:
                    profile = self.calibrations.save(self._device_id or "", self._side, self._calibration_captures)
                except (ValidationError, RuntimeError) as exc:
                    self._calibration_error = str(exc)
                else:
                    self._calibration_profile = profile
                    self._calibration_error = ""
                    mapper.set_profile(profile)
                    self._log("info", "vision calibration applied", device_id=self._device_id, side=self._side)

    def _calibration_status_locked(self) -> dict[str, Any]:
        return {
            "profile": self._calibration_profile,
            "profile_available": self._calibration_profile is not None,
            "active_stage": self._calibration_stage,
            "sample_count": len(self._calibration_samples),
            "sample_target": self._calibration_target,
            "completed_stages": [stage for stage in CALIBRATION_STAGES if stage in self._calibration_captures],
            "error": self._calibration_error,
        }

    def _dependencies_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import cv2  # noqa: F401
            import mediapipe  # noqa: F401
        except Exception:
            self._available = False
        else:
            self._available = True
        return self._available

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self.logger is not None:
            self.logger.write(level, message, **fields)


__all__ = ["VisionManager"]
