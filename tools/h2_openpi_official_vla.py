#!/usr/bin/env python3
"""H2 OpenPI VLA deployment client using Unitree's official rt/arm_sdk path."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.h2_action_calibration import ActionCalibrator, add_deployment_args  # noqa: E402
from tools.h2_official_arm_sdk_control import (  # noqa: E402
    ARM_Q_MAX,
    ARM_Q_MIN,
    ARM_NAMES,
    H2GravityCompensator,
    OfficialH2ArmSdk,
    format_q,
    import_unitree_sdk,
)

DEFAULT_TAU_RATE_LIMIT_NM_PER_S = np.asarray(
    [50.0, 40.0, 40.0, 35.0, 20.0, 20.0, 20.0, 50.0, 40.0, 40.0, 35.0, 20.0, 20.0, 20.0],
    dtype=np.float32,
)
DEFAULT_TAU_RATE_LIMIT_CSV = ",".join(f"{float(value):g}" for value in DEFAULT_TAU_RATE_LIMIT_NM_PER_S)


def prepare_action_calibration(args: argparse.Namespace) -> ActionCalibrator | None:
    model_path = getattr(args, "action_calibration_model", "")
    if not model_path:
        setattr(args, "_action_calibrator", None)
        return None
    max_offset = float(getattr(args, "action_calibration_max_offset", 0.0))
    blend = float(getattr(args, "action_calibration_blend", 1.0))
    if max_offset <= 0.0:
        raise ValueError("--action-calibration-max-offset must be positive when calibration is enabled")
    if not 0.0 <= blend <= 1.0:
        raise ValueError("--action-calibration-blend must be in [0,1]")
    path = Path(model_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    calibrator = ActionCalibrator.load(path)
    setattr(args, "_action_calibrator", calibrator)
    print(
        f"[CALIBRATION] enabled {calibrator.summary()} "
        f"blend={blend:.3f} max_offset={max_offset:.4f}",
        flush=True,
    )
    return calibrator


def calibrate_selected_action(
    current: np.ndarray,
    selected: np.ndarray,
    args: argparse.Namespace,
) -> tuple[np.ndarray, dict[str, Any] | None]:
    calibrator = getattr(args, "_action_calibrator", None)
    if calibrator is None or args.control_arm == "left":
        return selected.copy(), None
    predicted_right = calibrator.predict_realized(current[7:14], selected[7:14])
    raw_offset = predicted_right.astype(np.float64) - selected[7:14].astype(np.float64)
    max_offset = float(args.action_calibration_max_offset)
    clipped_offset = np.clip(raw_offset, -max_offset, max_offset)
    applied_offset = clipped_offset * float(args.action_calibration_blend)
    calibrated = selected.copy()
    calibrated[7:14] = selected[7:14] + applied_offset.astype(np.float32)
    info = {
        "model": str(calibrator.path),
        "kind": calibrator.kind,
        "raw_offset": raw_offset,
        "clipped_offset": clipped_offset,
        "applied_offset": applied_offset,
        "clipped_indices_right": np.flatnonzero(np.abs(raw_offset - clipped_offset) > 1e-8),
    }
    return calibrated, info


def _request_shutdown(signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


@dataclass
class Observation:
    state: np.ndarray
    image: np.ndarray
    left_wrist_image: np.ndarray
    right_wrist_image: np.ndarray
    state_tail: np.ndarray | None = None


class MotorPolicyController:
    """Translate normalized policy outputs into short, fail-safe motor pulses."""

    def __init__(
        self,
        url: str,
        action_indices: list[int],
        *,
        left_max: float = 0.25,
        right_min: float = 0.75,
        timeout: float = 0.25,
    ) -> None:
        self.url = url
        self.action_indices = action_indices
        self.left_max = left_max
        self.right_min = right_min
        self.timeout = timeout
        self.current_value = 0.5
        self.active_action = "stop"
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.action_indices)

    def pulse(self, policy_action: np.ndarray, hold_seconds: float) -> str:
        if not self.enabled:
            return "stop"
        values = np.asarray(policy_action, dtype=np.float32)[self.action_indices]
        value = float(np.mean(values))
        if float(np.max(values) - np.min(values)) > 0.35:
            action = "stop"
            value = 0.5
        elif value <= self.left_max:
            action = "left"
            value = 0.0
        elif value >= self.right_min:
            action = "right"
            value = 1.0
        else:
            action = "stop"
            value = 0.5
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._send(action)
            self.active_action = action
            self.current_value = value
            if action != "stop":
                self._timer = threading.Timer(max(0.05, hold_seconds), self.stop)
                self._timer.daemon = True
                self._timer.start()
        return action

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self.enabled and self.active_action != "stop":
                self._send("stop")
            self.active_action = "stop"
            self.current_value = 0.5

    def _send(self, action: str) -> None:
        body = json.dumps({"action": action}, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                response.read()
        except (OSError, error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"motor policy command failed: action={action}, error={exc}") from exc


def configure_policy_layout(args: argparse.Namespace) -> MotorPolicyController:
    tail_text = str(getattr(args, "state_tail_values", "") or "").strip()
    if tail_text:
        try:
            tail = np.asarray([float(value.strip()) for value in tail_text.split(",")], dtype=np.float32)
        except ValueError as exc:
            raise ValueError("--state-tail-values must be comma-separated numbers") from exc
    else:
        tail_count = int(getattr(args, "state_tail_zeros", 0))
        if tail_count < 0:
            raise ValueError("--state-tail-zeros must be >= 0")
        tail = np.zeros(tail_count, dtype=np.float32)
    index_text = str(getattr(args, "motor_action_indices", "") or "").strip()
    try:
        indices = [int(value.strip()) for value in index_text.split(",") if value.strip()]
    except ValueError as exc:
        raise ValueError("--motor-action-indices must be comma-separated integers") from exc
    expected_dim = 14 + len(tail)
    if any(index < 14 or index >= expected_dim for index in indices):
        raise ValueError(
            f"--motor-action-indices must be within the state tail [14,{expected_dim - 1}]"
        )
    for index in indices:
        tail[index - 14] = 0.5
    motor_url = str(getattr(args, "motor_control_url", "") or "").strip()
    left_max = float(getattr(args, "motor_left_max", 0.25))
    right_min = float(getattr(args, "motor_right_min", 0.75))
    if indices and not motor_url.startswith(("http://", "https://")):
        raise ValueError("--motor-control-url must be an http(s) URL when motor control is enabled")
    if not 0.0 <= left_max < right_min <= 1.0:
        raise ValueError("motor thresholds must satisfy 0 <= left-max < right-min <= 1")
    args._state_tail = tail
    args._expected_real_action_dim = expected_dim
    return MotorPolicyController(
        motor_url,
        indices,
        left_max=left_max,
        right_min=right_min,
    )


def attach_policy_state(obs: Observation, args: argparse.Namespace, motor: MotorPolicyController) -> None:
    tail = np.asarray(getattr(args, "_state_tail", np.empty(0)), dtype=np.float32).copy()
    for index in motor.action_indices:
        tail[index - 14] = motor.current_value
    obs.state_tail = tail


def to_jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.astype(float).tolist()
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def log_event(args: argparse.Namespace, event: str, **payload: Any) -> None:
    log_path = getattr(args, "log_jsonl", "")
    if not log_path:
        return
    path = Path(log_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.time(),
        "event": event,
        **payload,
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(to_jsonable(record), separators=(",", ":")) + "\n")


def parse_q14_float_csv(value: str | np.ndarray, *, name: str) -> np.ndarray:
    if isinstance(value, np.ndarray):
        out = np.asarray(value, dtype=np.float32).copy()
    else:
        parts = [part.strip() for part in str(value).split(",") if part.strip()]
        try:
            out = np.asarray([float(part) for part in parts], dtype=np.float32)
        except ValueError as exc:
            raise ValueError(f"{name} must be a comma-separated list of numbers") from exc
    if out.shape != (14,) or not np.all(np.isfinite(out)):
        raise ValueError(f"{name} must contain 14 finite values")
    return out


def resolve_tau_activation_blend_seconds(args: argparse.Namespace) -> float:
    new_value = getattr(args, "tau_activation_blend_seconds", None)
    old_value = getattr(args, "gravity_ramp_seconds", None)
    if new_value is not None and old_value is not None:
        if abs(float(new_value) - float(old_value)) > 1e-9:
            raise ValueError("Use only one of --tau-activation-blend-seconds or deprecated --gravity-ramp-seconds")
        print("[WARN] --gravity-ramp-seconds is deprecated; use --tau-activation-blend-seconds", flush=True)
    value = 0.5 if new_value is None and old_value is None else new_value if new_value is not None else old_value
    value = float(value)
    if value < 0.0:
        raise ValueError("--tau-activation-blend-seconds must be >= 0")
    args.tau_activation_blend_seconds = value
    args.gravity_ramp_seconds = value
    return value


def finalize_arm_safety_args(args: argparse.Namespace) -> None:
    tau_activation_blend_s = resolve_tau_activation_blend_seconds(args)
    if min(
        args.arm_feedback_gain,
        args.arm_feedback_max_offset,
        args.arm_feedback_ki,
        args.arm_feedback_integral_zone,
        args.arm_feedback_max_integral,
        tau_activation_blend_s,
    ) < 0.0:
        raise ValueError("arm feedback and tau activation parameters must be >= 0")
    if args.unsafe_disable_command_limits:
        if args.execute:
            raise ValueError("--unsafe-disable-command-limits is not allowed with --execute")
        print("[WARN] command q continuity limits are disabled for this non-execute run", flush=True)
    else:
        if not np.isfinite(args.max_command_delta) or args.max_command_delta <= 0.0:
            raise ValueError("--max-command-delta must be positive; 0 no longer disables q continuity limits")
        if not np.isfinite(args.max_command_velocity) or args.max_command_velocity <= 0.0:
            raise ValueError("--max-command-velocity must be positive; 0 no longer disables q continuity limits")
    if args.unsafe_disable_tau_rate_limit:
        if args.execute:
            raise ValueError("--unsafe-disable-tau-rate-limit is not allowed with --execute")
        args.tau_rate_limit_array = np.full(14, np.inf, dtype=np.float32)
        print("[WARN] tau rate limiting is disabled for this non-execute run", flush=True)
    else:
        tau_rate_limit = parse_q14_float_csv(args.tau_rate_limit, name="--tau-rate-limit")
        if np.any(tau_rate_limit <= 0.0):
            raise ValueError("--tau-rate-limit values must be positive Nm/s")
        args.tau_rate_limit_array = tau_rate_limit


def image_stats(image: np.ndarray) -> dict[str, float | list[int]]:
    arr = np.asarray(image)
    return {
        "shape": list(arr.shape),
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr)),
        "black_fraction": float(np.mean(np.all(arr <= 2, axis=-1))) if arr.ndim == 3 else float("nan"),
    }


def save_debug_images(args: argparse.Namespace, obs: Observation, step: int) -> None:
    image_dir = getattr(args, "debug_image_dir", "")
    if not image_dir:
        return
    path = Path(image_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    for name, image in (
        ("image", obs.image),
        ("left_wrist_image", obs.left_wrist_image),
        ("right_wrist_image", obs.right_wrist_image),
    ):
        bgr = cv2.cvtColor(np.asarray(image, dtype=np.uint8), cv2.COLOR_RGB2BGR)
        out = path / f"step_{step:03d}_{name}.jpg"
        if not cv2.imwrite(str(out), bgr):
            raise RuntimeError(f"Failed to write debug image: {out}")


class ArmSdkTargetHold:
    def __init__(
        self,
        arm_client: OfficialH2ArmSdk,
        publish_hz: float = 250.0,
        max_velocity: float = 0.3,
        telemetry_period_s: float = 1.0,
        feedback_gain: float = 0.6,
        feedback_max_offset: float = 0.12,
        feedback_arm: str = "both",
        feedback_ki: float = 0.8,
        feedback_integral_zone: float = 0.15,
        feedback_max_integral: float = 0.08,
        gravity_model_cache: str = "",
        tau_activation_blend_s: float | None = None,
        tau_rate_limit: np.ndarray | None = None,
        unsafe_disable_command_limits: bool = False,
        unsafe_disable_tau_rate_limit: bool = False,
        gravity_ramp_s: float | None = None,
    ) -> None:
        if publish_hz <= 0.0:
            raise ValueError("publish_hz must be positive")
        if telemetry_period_s < 0.0:
            raise ValueError("telemetry_period_s must be >= 0")
        if tau_activation_blend_s is not None and gravity_ramp_s is not None:
            if abs(float(tau_activation_blend_s) - float(gravity_ramp_s)) > 1e-9:
                raise ValueError("Use only one of tau_activation_blend_s or deprecated gravity_ramp_s")
        if tau_activation_blend_s is None:
            tau_activation_blend_s = 0.5 if gravity_ramp_s is None else gravity_ramp_s
        if min(
            feedback_gain,
            feedback_max_offset,
            feedback_ki,
            feedback_integral_zone,
            feedback_max_integral,
            tau_activation_blend_s,
        ) < 0.0:
            raise ValueError("feedback and tau activation parameters must be >= 0")
        if feedback_arm not in {"right", "left", "both"}:
            raise ValueError("feedback_arm must be right, left, or both")
        if max_velocity <= 0.0 and not unsafe_disable_command_limits:
            raise ValueError("max_velocity must be positive; use unsafe_disable_command_limits only for diagnostics")
        if tau_rate_limit is None:
            tau_rate_limit = DEFAULT_TAU_RATE_LIMIT_NM_PER_S
        tau_rate_limit = np.asarray(tau_rate_limit, dtype=np.float32)
        if tau_rate_limit.shape != (14,) or not np.all(np.isfinite(tau_rate_limit)):
            raise ValueError("tau_rate_limit must be a finite q14 array")
        if np.any(tau_rate_limit <= 0.0) and not unsafe_disable_tau_rate_limit:
            raise ValueError("tau_rate_limit values must be positive")
        self.arm_client = arm_client
        self.publish_hz = float(publish_hz)
        self.publish_dt = 1.0 / publish_hz
        self.max_step = float(max_velocity) * self.publish_dt
        self.unsafe_disable_command_limits = bool(unsafe_disable_command_limits)
        self.unsafe_disable_tau_rate_limit = bool(unsafe_disable_tau_rate_limit)
        self.telemetry_period_s = float(telemetry_period_s)
        self.feedback_gain = float(feedback_gain)
        self.feedback_max_offset = float(feedback_max_offset)
        self.feedback_arm = feedback_arm
        self.feedback_ki = float(feedback_ki)
        self.feedback_integral_zone = float(feedback_integral_zone)
        self.feedback_max_integral = float(feedback_max_integral)
        self.tau_activation_blend_s = float(tau_activation_blend_s)
        self.tau_rate_limit = tau_rate_limit.copy()
        self.gravity = H2GravityCompensator(gravity_model_cache) if gravity_model_cache else None
        self._lock = threading.Lock()
        self._desired: np.ndarray | None = None
        self._command: np.ndarray | None = None
        self._integral = np.zeros(14, dtype=np.float32)
        self._tau_model = np.zeros(14, dtype=np.float32)
        self._last_applied_tau = np.zeros(14, dtype=np.float32)
        self._activation_tau_start = np.zeros(14, dtype=np.float32)
        self._activation_started_at: float | None = None
        self._last_feedback_offset = np.zeros(14, dtype=np.float32)
        self._last_tau_scale = 0.0
        self._last_tau_rate_limited_joints: list[int] = []
        self._last_tau_rate_dt = self.publish_dt
        self._last_lowstate_age_ms: float | None = None
        self._tracking_active = False
        self._weight = 1.0
        self._started_at: float | None = None
        self._failure: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="h2-arm-sdk-hold", daemon=True)

    def start(self) -> None:
        print(
            f"[HOLD_CONFIG] publish_hz={self.publish_hz:.1f} "
            f"telemetry_period_s={self.telemetry_period_s:.2f} "
            f"max_velocity={self.max_step / self.publish_dt:.4f} "
            f"command_limits={'off' if self.unsafe_disable_command_limits else 'on'} "
            f"feedback_gain={self.feedback_gain:.3f} "
            f"feedback_ki={self.feedback_ki:.3f} "
            f"feedback_max_offset={self.feedback_max_offset:.4f} "
            f"feedback_arm={self.feedback_arm} "
            f"gravity={'on' if self.gravity is not None else 'off'} "
            f"tau_activation_blend_s={self.tau_activation_blend_s:.3f} "
            f"tau_rate_limit={'off' if self.unsafe_disable_tau_rate_limit else np.round(self.tau_rate_limit, 3).tolist()}",
            flush=True,
        )
        self._started_at = time.monotonic()
        self._thread.start()

    def set_target(self, target: np.ndarray, weight: float = 1.0, *, activate: bool = True) -> None:
        self.raise_if_failed()
        desired = np.asarray(target, dtype=np.float32).copy()
        if desired.shape != (14,) or not np.all(np.isfinite(desired)):
            raise ValueError("ArmSDK target must be a finite q14 array")
        if np.any(desired < ARM_Q_MIN) or np.any(desired > ARM_Q_MAX):
            offending = np.flatnonzero((desired < ARM_Q_MIN) | (desired > ARM_Q_MAX))
            raise ValueError(f"ArmSDK target exceeds H2 safety limits at q14 indices {offending.tolist()}")
        now = time.monotonic()
        with self._lock:
            previous = self._desired
            self._desired = desired
            if self._command is None:
                self._command = self.arm_client.read_arm_q()
            if previous is not None:
                jumped = np.abs(desired - previous) > self.feedback_integral_zone
                self._integral[jumped] = 0.0
            if activate and not self._tracking_active:
                self._tracking_active = True
                self._activation_started_at = now
                self._activation_tau_start = self._last_applied_tau.copy()
            self._weight = float(weight)

    def clear(self, *, reset_tau_history: bool = False) -> None:
        with self._lock:
            self._desired = None
            self._command = None
            self._integral.fill(0.0)
            self._tau_model.fill(0.0)
            if reset_tau_history:
                self._last_applied_tau.fill(0.0)
            self._activation_tau_start = self._last_applied_tau.copy()
            self._activation_started_at = None
            self._last_feedback_offset.fill(0.0)
            self._last_tau_scale = 0.0
            self._last_tau_rate_limited_joints = []
            self._tracking_active = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            gravity_tau = self._tau_model.copy()
            applied_tau = self._last_applied_tau.copy()
            feedback_offset = self._last_feedback_offset.copy()
            command = None if self._command is None else self._command.copy()
            desired = None if self._desired is None else self._desired.copy()
            tau_rate_limited_joints = list(self._last_tau_rate_limited_joints)
            return {
                "gravity_enabled": self.gravity is not None,
                "gravity_tau": gravity_tau,
                "gravity_tau_max": float(np.max(np.abs(gravity_tau))) if gravity_tau.size else 0.0,
                "applied_tau": applied_tau,
                "applied_tau_max": float(np.max(np.abs(applied_tau))) if applied_tau.size else 0.0,
                "gravity_tau_scale": float(self._last_tau_scale),
                "tau_activation_blend_s": self.tau_activation_blend_s,
                "tau_rate_limit": self.tau_rate_limit.copy(),
                "tau_rate_limit_enabled": not self.unsafe_disable_tau_rate_limit,
                "tau_rate_dt": float(self._last_tau_rate_dt),
                "tau_rate_limited_joints": tau_rate_limited_joints,
                "tracking_active": self._tracking_active,
                "lowstate_age_ms": self._last_lowstate_age_ms,
                "feedback_offset": feedback_offset,
                "feedback_offset_max": float(np.max(np.abs(feedback_offset))) if feedback_offset.size else 0.0,
                "holder_command": command,
                "holder_desired": desired,
            }

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("250 Hz ArmSDK control thread failed") from self._failure

    def _run(self) -> None:
        try:
            next_tick = time.monotonic()
            telemetry_started = next_tick
            last_publish_at: float | None = None
            last_tau_at: float | None = None
            publish_intervals: list[float] = []
            controlled = controlled_joint_slice(self.feedback_arm)
            while not self._stop.is_set():
                lowstate_age_ms: float | None = None
                try:
                    state_snapshot = self.arm_client.read_arm_state_snapshot()
                    actual = state_snapshot.q
                    lowstate_age_ms = (time.monotonic() - state_snapshot.received_at_monotonic) * 1000.0
                except AttributeError:
                    actual = self.arm_client.read_arm_q()
                now = time.monotonic()
                tau_dt = self.publish_dt if last_tau_at is None else max(0.0, min(now - last_tau_at, 2.0 * self.publish_dt))
                with self._lock:
                    if self._desired is None:
                        target = None
                        desired = None
                        tau = None
                        weight = self._weight
                        feedback_offset = np.zeros(14, dtype=np.float32)
                        self._last_lowstate_age_ms = lowstate_age_ms
                    else:
                        desired = self._desired.copy()
                        if self._command is None:
                            self._command = actual.copy()
                        error = desired - actual
                        active = np.abs(error[controlled]) <= self.feedback_integral_zone
                        integral = self._integral[controlled]
                        integral[active] = np.clip(
                            integral[active] + self.feedback_ki * error[controlled][active] * self.publish_dt,
                            -self.feedback_max_integral,
                            self.feedback_max_integral,
                        )
                        integral[~active] = 0.0
                        self._integral[controlled] = integral
                        feedback_offset = np.zeros(14, dtype=np.float32)
                        feedback_offset[controlled] = np.clip(
                            self.feedback_gain * error[controlled] + self._integral[controlled],
                            -self.feedback_max_offset,
                            self.feedback_max_offset,
                        )
                        corrected = desired + feedback_offset
                        if self.unsafe_disable_command_limits:
                            delta = corrected - self._command
                        else:
                            delta = np.clip(corrected - self._command, -self.max_step, self.max_step)
                        self._command = np.clip(
                            self._command + delta,
                            ARM_Q_MIN,
                            ARM_Q_MAX,
                        ).astype(np.float32)
                        target = self._command.copy()
                        weight = self._weight
                        tau_model = (
                            self.gravity.compute(target)
                            if self.gravity is not None
                            else np.zeros(14, dtype=np.float32)
                        )
                        if (
                            self._tracking_active
                            and self._activation_started_at is not None
                            and self.tau_activation_blend_s > 0.0
                        ):
                            tau_scale = min(1.0, (now - self._activation_started_at) / self.tau_activation_blend_s)
                            tau_target = self._activation_tau_start + tau_scale * (
                                tau_model - self._activation_tau_start
                            )
                            if tau_scale >= 1.0:
                                self._activation_started_at = None
                        else:
                            tau_scale = 1.0
                            tau_target = tau_model
                            if self._tracking_active and self._activation_started_at is not None:
                                self._activation_started_at = None
                        tau_error = tau_target - self._last_applied_tau
                        if self.unsafe_disable_tau_rate_limit:
                            tau = tau_target.astype(np.float32)
                            limited_joints: list[int] = []
                        else:
                            max_tau_delta = self.tau_rate_limit * np.float32(tau_dt)
                            clipped_tau_delta = np.clip(tau_error, -max_tau_delta, max_tau_delta)
                            tau = (self._last_applied_tau + clipped_tau_delta).astype(np.float32)
                            limited_joints = np.flatnonzero(np.abs(tau_error - clipped_tau_delta) > 1e-7).tolist()
                        if tau.shape != (14,) or not np.all(np.isfinite(tau)):
                            raise RuntimeError("computed invalid ArmSDK tau feedforward")
                        if float(np.max(np.abs(tau))) > 20.0:
                            raise RuntimeError("refusing rate-limited ArmSDK tau above 20 Nm")
                        self._tau_model = tau_model.copy()
                        self._last_applied_tau = tau.copy()
                        self._last_feedback_offset = feedback_offset.copy()
                        self._last_tau_scale = float(tau_scale)
                        self._last_tau_rate_limited_joints = limited_joints
                        self._last_tau_rate_dt = float(tau_dt)
                        self._last_lowstate_age_ms = lowstate_age_ms
                if target is not None:
                    self.arm_client._write_arm_command(target, weight=weight, tau14=tau)
                    published_at = time.monotonic()
                    if last_publish_at is not None:
                        publish_intervals.append(published_at - last_publish_at)
                    last_publish_at = published_at
                    last_tau_at = published_at
                    if (
                        self.telemetry_period_s > 0.0
                        and published_at - telemetry_started >= self.telemetry_period_s
                    ):
                        command_error = np.abs(actual - target)
                        desired_error = np.abs(actual - desired)
                        interval_ms = np.asarray(publish_intervals, dtype=np.float64) * 1000.0
                        measured_hz = 1000.0 / float(np.mean(interval_ms)) if interval_ms.size else 0.0
                        print(
                            f"[HOLD_RATE] configured_hz={self.publish_hz:.1f} measured_hz={measured_hz:.2f} "
                            f"interval_mean_ms={float(np.mean(interval_ms)) if interval_ms.size else 0.0:.3f} "
                            f"interval_p95_ms={float(np.percentile(interval_ms, 95)) if interval_ms.size else 0.0:.3f} "
                            f"interval_max_ms={float(np.max(interval_ms)) if interval_ms.size else 0.0:.3f} "
                            f"command_error_max={float(np.max(command_error)):.5f} "
                            f"desired_error_max={float(np.max(desired_error)):.5f} "
                            f"feedback_offset_max={float(np.max(np.abs(feedback_offset))):.5f} "
                            f"tau_max={float(np.max(np.abs(tau))):.4f} "
                            f"tau_model_max={float(np.max(np.abs(self._tau_model))):.4f} "
                            f"tau_rate_limited_joints={self._last_tau_rate_limited_joints} "
                            f"tau_dt_ms={self._last_tau_rate_dt * 1000.0:.3f} "
                            f"lowstate_age_ms={lowstate_age_ms if lowstate_age_ms is not None else -1.0:.3f} "
                            f"desired_error_by_joint={np.round(desired_error, 5).tolist()}",
                            flush=True,
                        )
                        telemetry_started = published_at
                        publish_intervals.clear()
                next_tick += self.publish_dt
                delay = next_tick - time.monotonic()
                if delay < -self.publish_dt:
                    next_tick = time.monotonic()
                    delay = 0.0
                time.sleep(max(0.0, delay))
        except BaseException as exc:
            self._failure = exc
            self._stop.set()


def init_dds(network_interface: str, domain_id: int) -> None:
    channel_factory_initialize = import_unitree_sdk()[0]
    channel_factory_initialize(domain_id, network_interface)


def frame_to_rgb(frame: Any, camera_name: str) -> np.ndarray:
    bgr = getattr(frame, "bgr", None)
    if bgr is None:
        raise RuntimeError(f"No decoded BGR frame from {camera_name}: {frame!r}")
    return cv2.cvtColor(np.asarray(bgr), cv2.COLOR_BGR2RGB)


def import_image_client() -> Any:
    from teleimager.image_client import ImageClient

    return ImageClient


def read_image_with_retry(client: Any, camera_name: str, timeout_s: float) -> np.ndarray:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return frame_to_rgb(client.get_camera_frame(camera_name), camera_name)
        except Exception as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for image from {camera_name}: {last_error}") from last_error


def load_rgb_image(path: str | Path) -> np.ndarray:
    image_path = resolve_project_path(path)
    bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError(f"Failed to read image: {image_path}")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def fallback_image(
    args: argparse.Namespace,
    reference: np.ndarray | None,
    camera_name: str,
    error: Exception | None = None,
) -> np.ndarray:
    left_wrist_fallback = getattr(args, "left_wrist_fallback_image", "")
    if left_wrist_fallback and camera_name == args.left_wrist_camera:
        image_path = resolve_project_path(left_wrist_fallback)
        image = load_rgb_image(image_path)
        print(f"[WARN] {camera_name} unavailable; using fallback image {image_path}", flush=True)
        return image
    if args.missing_camera_policy == "error":
        raise RuntimeError(f"Required camera is unavailable: {camera_name}") from error
    if args.missing_camera_policy == "duplicate-main" and reference is not None:
        print(f"[WARN] {camera_name} unavailable; duplicate main image", flush=True)
        return reference.copy()
    height = reference.shape[0] if reference is not None else args.fallback_image_height
    width = reference.shape[1] if reference is not None else args.fallback_image_width
    print(f"[WARN] {camera_name} unavailable; black fallback {height}x{width}", flush=True)
    return np.zeros((height, width, 3), dtype=np.uint8)


def read_optional_image(
    client: Any,
    camera_name: str,
    args: argparse.Namespace,
    reference: np.ndarray | None = None,
) -> np.ndarray:
    try:
        return read_image_with_retry(client, camera_name, args.image_timeout)
    except Exception as exc:
        return fallback_image(args, reference, camera_name, exc)


def read_observation(image_client: Any, arm_client: OfficialH2ArmSdk, args: argparse.Namespace) -> Observation:
    state = arm_client.read_arm_q()
    image = read_optional_image(image_client, args.image_camera, args)
    if args.left_wrist_static_image:
        left_wrist_image = load_rgb_image(args.left_wrist_static_image)
    else:
        left_wrist_image = read_optional_image(image_client, args.left_wrist_camera, args, reference=image)
    right_wrist_image = read_optional_image(image_client, args.right_wrist_camera, args, reference=image)
    return Observation(
        state=state,
        image=image,
        left_wrist_image=left_wrist_image,
        right_wrist_image=right_wrist_image,
    )


def encode_image(image: np.ndarray, jpeg_quality: int) -> dict[str, str]:
    bgr = cv2.cvtColor(np.asarray(image, dtype=np.uint8), cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)])
    if not ok:
        raise RuntimeError("Failed to encode JPEG image")
    return {"encoding": "jpeg", "data": base64.b64encode(encoded).decode("ascii")}


def model_state_vector(obs: Observation, state_tail_zeros: int) -> list[float]:
    state = obs.state.astype(np.float32)
    if obs.state_tail is not None:
        state = np.concatenate([state, np.asarray(obs.state_tail, dtype=np.float32)])
    elif state_tail_zeros > 0:
        state = np.concatenate([state, np.zeros(state_tail_zeros, dtype=np.float32)])
    return state.tolist()


def observation_payload_item(
    obs: Observation,
    instruction: str,
    jpeg_quality: int,
    state_tail_zeros: int,
) -> dict[str, Any]:
    return {
        "full_image": encode_image(obs.image, jpeg_quality),
        "left_wrist_image": encode_image(obs.left_wrist_image, jpeg_quality),
        "right_wrist_image": encode_image(obs.right_wrist_image, jpeg_quality),
        "state": model_state_vector(obs, state_tail_zeros),
        "instruction": instruction,
        "task_name": instruction,
    }


def build_payload(
    obs_history: list[Observation],
    instruction: str,
    jpeg_quality: int,
    state_tail_zeros: int,
) -> dict[str, Any]:
    if not obs_history:
        raise ValueError("obs_history is empty")
    return {
        "observations": [
            observation_payload_item(obs, instruction, jpeg_quality, state_tail_zeros)
            for obs in obs_history
        ]
    }


def request_actions(args: argparse.Namespace, obs_history: list[Observation]) -> np.ndarray:
    url = args.server.rstrip("/") + "/predict_action"
    body = json.dumps(
        build_payload(obs_history, args.instruction, args.jpeg_quality, args.state_tail_zeros),
        separators=(",", ":"),
    ).encode("utf-8")
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=args.request_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"policy server HTTP {exc.code}: {message}") from exc
    if payload.get("result") != "ok":
        raise RuntimeError(f"policy server returned error: {payload}")
    actions = np.asarray(payload.get("action"), dtype=np.float32)
    print(
        f"[HTTP] observations={len(obs_history)} payload_kib={len(body) / 1024:.1f} latency={time.monotonic() - started:.2f}s "
        f"action_shape={tuple(actions.shape)}",
        flush=True,
    )
    return actions


def validate_actions(actions: np.ndarray, args: argparse.Namespace) -> np.ndarray:
    expected_dim = int(getattr(args, "_expected_real_action_dim", 14))
    if actions.ndim != 2:
        raise ValueError(f"expected [T,{expected_dim}] H2 policy actions, got {actions.shape}")
    if actions.shape[1] != expected_dim:
        if args.extra_action_dims_policy == "crop" and actions.shape[1] > expected_dim:
            ignored = actions[:, expected_dim:]
            print(
                f"[WARN] cropping action dim {actions.shape[1]} -> {expected_dim}; "
                f"ignored_tail_min={float(np.min(ignored)):.4f} ignored_tail_max={float(np.max(ignored)):.4f}",
                flush=True,
            )
            actions = actions[:, :expected_dim]
        else:
            raise ValueError(f"expected [T,{expected_dim}] H2 policy actions, got {actions.shape}")
    if actions.shape[0] < 1:
        raise ValueError("policy returned empty action horizon")
    if not np.all(np.isfinite(actions)):
        raise ValueError("policy returned NaN or Inf")
    max_abs = float(np.max(np.abs(actions)))
    if args.max_abs_q > 0.0 and max_abs > args.max_abs_q:
        raise ValueError(f"policy action max_abs={max_abs:.4f} > --max-abs-q={args.max_abs_q:.4f}")
    return actions[: args.action_horizon]


def controlled_delta_by_index(actions: np.ndarray, state: np.ndarray, control_arm: str) -> list[float]:
    deltas: list[float] = []
    for action in actions:
        selected = keep_selected_arm(action[:14], state, control_arm)
        deltas.append(float(np.max(np.abs(selected - state))))
    return deltas


def choose_action_start_index(actions: np.ndarray, state: np.ndarray, args: argparse.Namespace) -> int:
    fixed_start = min(max(args.action_start_index, 0), actions.shape[0] - 1)
    if not args.auto_action_start:
        return fixed_start
    max_start = min(max(args.auto_action_max_start, fixed_start), actions.shape[0] - 1)
    deltas = controlled_delta_by_index(actions, state, args.control_arm)
    for idx in range(fixed_start, max_start + 1):
        if deltas[idx] >= args.auto_action_min_delta:
            return idx
    return fixed_start


def keep_selected_arm(target: np.ndarray, current: np.ndarray, control_arm: str) -> np.ndarray:
    out = np.asarray(target, dtype=np.float32).copy()
    if control_arm == "right":
        out[:7] = current[:7]
    elif control_arm == "left":
        out[7:] = current[7:]
    return out


def controlled_joint_slice(control_arm: str) -> slice:
    if control_arm == "right":
        return slice(7, 14)
    if control_arm == "left":
        return slice(0, 7)
    return slice(0, 14)


def resolve_project_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return resolved


def limit_target_delta(
    target: np.ndarray,
    current: np.ndarray,
    max_delta: float,
    *,
    allow_disable: bool = False,
) -> tuple[np.ndarray, float]:
    delta = np.asarray(target, dtype=np.float32) - np.asarray(current, dtype=np.float32)
    requested = float(np.max(np.abs(delta)))
    if max_delta <= 0.0:
        if allow_disable:
            return target.astype(np.float32), requested
        raise ValueError("max_delta must be positive; 0 no longer disables command delta limiting")
    if requested <= max_delta:
        return target.astype(np.float32), requested
    return (current + np.clip(delta, -max_delta, max_delta)).astype(np.float32), requested


def load_init_pose(path: str | Path) -> np.ndarray:
    pose_path = resolve_project_path(path)
    with pose_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if isinstance(payload.get("positions"), dict):
        positions = {
            (name[:-6] if str(name).endswith("_joint") else str(name)): value
            for name, value in payload["positions"].items()
        }
        missing = [name for name in ARM_NAMES if name not in positions]
        if missing:
            raise ValueError(f"init pose missing joints: {missing}")
        values = [positions[name] for name in ARM_NAMES]
    else:
        values = payload.get("qpos", payload.get("arm_q14"))
    pose = np.asarray(values, dtype=np.float32)
    if pose.shape != (14,) or not np.all(np.isfinite(pose)):
        raise ValueError(f"init pose must be 14 finite values, got {pose.shape}")
    return pose


CSV_TO_Q14_NAMES = (
    "left_shoulder_pitch_joint",
    "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",
    "left_elbow_joint",
    "left_wrist_roll_joint",
    "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_joint",
    "right_wrist_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
)


def load_player_csv_q14(path: str | Path) -> np.ndarray:
    csv_path = resolve_project_path(path)
    rows: list[list[float]] = []
    with csv_path.open("r", newline="", encoding="ascii") as stream:
        reader = csv.DictReader(stream)
        missing = [name for name in CSV_TO_Q14_NAMES if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{csv_path} is missing required columns: {missing}")
        for row in reader:
            rows.append([float(row[name]) for name in CSV_TO_Q14_NAMES])
    if not rows:
        raise ValueError(f"{csv_path} contains no trajectory frames")
    q14 = np.asarray(rows, dtype=np.float32)
    if not np.all(np.isfinite(q14)):
        raise ValueError(f"{csv_path} contains NaN or Inf")
    return q14


def slice_trajectory(q14: np.ndarray, *, start: int, end: int, stride: int, max_frames: int) -> np.ndarray:
    if stride <= 0:
        raise ValueError("--pre-vla-trajectory-stride must be positive")
    total = q14.shape[0]
    start = max(0, start)
    stop = total if end <= 0 else min(total, end)
    if start >= stop:
        raise ValueError(f"empty trajectory slice: start={start} end={end} total={total}")
    sliced = q14[start:stop:stride]
    if max_frames > 0:
        sliced = sliced[:max_frames]
    if sliced.shape[0] < 2:
        raise ValueError("pre VLA trajectory must contain at least 2 frames")
    return sliced


def execute_pre_vla_trajectory(arm_client: OfficialH2ArmSdk, args: argparse.Namespace) -> None:
    if not args.pre_vla_trajectory_csv:
        return
    q14 = load_player_csv_q14(args.pre_vla_trajectory_csv)
    q14 = slice_trajectory(
        q14,
        start=args.pre_vla_trajectory_start,
        end=args.pre_vla_trajectory_end,
        stride=args.pre_vla_trajectory_stride,
        max_frames=args.pre_vla_trajectory_max_frames,
    )
    current = arm_client.read_arm_q()
    control_arm = args.pre_vla_trajectory_control_arm or args.control_arm
    selected_frames: list[np.ndarray] = []
    previous = current
    for frame in q14:
        selected = keep_selected_arm(frame, previous, control_arm)
        selected_frames.append(selected)
        previous = selected
    q14 = np.asarray(selected_frames, dtype=np.float32)
    start_delta = float(np.max(np.abs(q14[0] - current)))
    step_delta = float(np.max(np.abs(np.diff(q14, axis=0)))) if q14.shape[0] > 1 else 0.0
    print(
        f"[PRE_VLA_TRAJ] csv={resolve_project_path(args.pre_vla_trajectory_csv)} "
        f"frames={q14.shape[0]} control_arm={control_arm} "
        f"start_delta={start_delta:.4f} step_delta={step_delta:.4f}",
        flush=True,
    )
    if args.pre_vla_trajectory_max_start_delta > 0 and start_delta > args.pre_vla_trajectory_max_start_delta:
        raise RuntimeError(
            f"pre VLA trajectory first frame is {start_delta:.4f} rad from current pose; "
            f"raise --pre-vla-trajectory-max-start-delta or move to a closer init pose"
        )
    if args.pre_vla_trajectory_max_step_delta > 0 and step_delta > args.pre_vla_trajectory_max_step_delta:
        raise RuntimeError(
            f"pre VLA trajectory step delta {step_delta:.4f} exceeds "
            f"--pre-vla-trajectory-max-step-delta={args.pre_vla_trajectory_max_step_delta:.4f}"
        )

    period = 1.0 / args.pre_vla_trajectory_freq
    command = current.copy()
    next_tick = time.monotonic()
    before = current.copy()
    holder: ArmSdkTargetHold | None = None
    if args.pre_vla_trajectory_publish_mode == "hold":
        holder = ArmSdkTargetHold(
            arm_client,
            publish_hz=args.pre_vla_trajectory_publish_hz,
            max_velocity=args.max_command_velocity,
            telemetry_period_s=args.arm_sdk_telemetry_period,
            feedback_gain=args.arm_feedback_gain,
            feedback_max_offset=args.arm_feedback_max_offset,
            feedback_arm="both",
            feedback_ki=args.arm_feedback_ki,
            feedback_integral_zone=args.arm_feedback_integral_zone,
            feedback_max_integral=args.arm_feedback_max_integral,
            gravity_model_cache=args.gravity_model_cache,
            tau_activation_blend_s=args.tau_activation_blend_seconds,
            tau_rate_limit=args.tau_rate_limit_array,
            unsafe_disable_command_limits=args.unsafe_disable_command_limits,
            unsafe_disable_tau_rate_limit=args.unsafe_disable_tau_rate_limit,
        )
        holder.start()
        holder.set_target(command, weight=1.0, activate=False)
    try:
        for frame_index, target in enumerate(q14):
            delta = target - command
            requested = float(np.max(np.abs(delta)))
            if args.pre_vla_trajectory_max_command_delta > 0 and requested > args.pre_vla_trajectory_max_command_delta:
                target = command + delta * (args.pre_vla_trajectory_max_command_delta / requested)
                print(
                    f"[PRE_VLA_TRAJ_LIMIT] frame={frame_index} requested={requested:.4f} "
                    f"clipped={args.pre_vla_trajectory_max_command_delta:.4f}",
                    flush=True,
                )
            command = target.astype(np.float32)
            if holder is None:
                arm_client._write_arm_command(command, weight=1.0)
            else:
                holder.set_target(command, weight=1.0)
            next_tick += period
            time.sleep(max(0.0, next_tick - time.monotonic()))

        hold_end = time.monotonic() + max(0.0, args.pre_vla_trajectory_hold)
        while time.monotonic() < hold_end:
            if holder is None:
                arm_client._write_arm_command(command, weight=1.0)
            else:
                holder.set_target(command, weight=1.0)
            time.sleep(min(period, 0.05))
    finally:
        if holder is not None:
            holder.clear()
            holder.stop()
    held = arm_client.read_arm_q()
    print(
        f"[PRE_VLA_TRAJ] moved_max={float(np.max(np.abs(held - before))):.4f} "
        f"target_error_max={float(np.max(np.abs(held - command))):.4f}",
        flush=True,
    )
    log_event(
        args,
        "pre_vla_trajectory",
        csv=args.pre_vla_trajectory_csv,
        frames=q14.shape[0],
        before=before,
        command=command,
        held=held,
        moved_max=float(np.max(np.abs(held - before))),
        target_error_max=float(np.max(np.abs(held - command))),
    )


def move_to_pose(
    arm_client: OfficialH2ArmSdk,
    *,
    pose_file: str | Path,
    label: str,
    control_arm: str,
    duration_s: float,
    hold_s: float,
    max_delta_allowed: float,
) -> np.ndarray:
    pose = load_init_pose(pose_file)
    current = arm_client.read_arm_q()
    target = keep_selected_arm(pose, current, control_arm)
    delta = float(np.max(np.abs(target - current)))
    format_q(f"before_{label}", current)
    format_q(f"target_{label}", target)
    print(
        f"[{label.upper()}] pose_file={resolve_project_path(pose_file)} "
        f"control_arm={control_arm} delta_max={delta:.4f} duration={duration_s:.2f}s "
        f"max_allowed={max_delta_allowed:.4f}",
        flush=True,
    )
    if delta > max_delta_allowed:
        raise RuntimeError(
            f"{label} delta {delta:.4f} exceeds max allowed {max_delta_allowed:.4f}"
        )
    result = arm_client.move_absolute(
        target,
        duration_s=duration_s,
        hold_s=hold_s,
        release_s=0.0,
    )
    print(
        f"[{label.upper()}] moved_max={result.moved_max:.4f} "
        f"target_error_max={result.target_error_max:.4f}",
        flush=True,
    )
    return result.held


def move_to_init_pose(arm_client: OfficialH2ArmSdk, args: argparse.Namespace) -> None:
    sequence = list(args.pre_init_pose_sequence or [])
    if sequence:
        for idx, pose_file in enumerate(sequence):
            held = move_to_pose(
                arm_client,
                pose_file=pose_file,
                label=f"pre_init_sequence_{idx:02d}",
                control_arm=args.pre_init_control_arm or args.control_arm,
                duration_s=args.pre_init_pose_duration,
                hold_s=args.pre_init_pose_hold,
                max_delta_allowed=args.max_pre_init_delta,
            )
            log_event(
                args,
                "pre_init_sequence",
                index=idx,
                pose_file=pose_file,
                held=held,
            )
        if args.skip_init_after_pre_sequence:
            return

    if args.pre_init_pose_file:
        held = move_to_pose(
            arm_client,
            pose_file=args.pre_init_pose_file,
            label="pre_init_pose",
            control_arm=args.pre_init_control_arm or args.control_arm,
            duration_s=args.pre_init_pose_duration,
            hold_s=args.pre_init_pose_hold,
            max_delta_allowed=args.max_pre_init_delta,
        )
        log_event(
            args,
            "pre_init_pose",
            pose_file=args.pre_init_pose_file,
            held=held,
        )
    held = move_to_pose(
        arm_client,
        pose_file=args.init_pose_file,
        label="init_pose",
        control_arm=args.init_control_arm or args.control_arm,
        duration_s=args.init_pose_duration,
        hold_s=args.init_pose_hold,
        max_delta_allowed=args.max_init_delta,
    )
    log_event(
        args,
        "init_pose",
        pose_file=args.init_pose_file,
        held=held,
    )


def execute_chunk(
    arm_client: OfficialH2ArmSdk,
    holder: ArmSdkTargetHold,
    actions: np.ndarray,
    args: argparse.Namespace,
    *,
    step: int,
    action_start_index: int | None = None,
    motor_controller: MotorPolicyController | None = None,
) -> np.ndarray:
    period = 1.0 / args.control_freq
    last_command: np.ndarray | None = None
    start_index = args.action_start_index if action_start_index is None else action_start_index
    end_index = min(actions.shape[0], start_index + args.exe_steps)
    for action_idx, policy_action in enumerate(actions[start_index:end_index], start=start_index):
        started = time.monotonic()
        current = arm_client.read_arm_q()
        raw_action = np.asarray(policy_action[:14], dtype=np.float32)
        selected = keep_selected_arm(raw_action, current, args.control_arm)
        raw_delta_max = float(np.max(np.abs(selected - current)))
        if args.reject_action_delta > 0.0 and raw_delta_max > args.reject_action_delta:
            raise RuntimeError(
                f"rejecting action jump at step={step} action_idx={action_idx}: "
                f"raw_delta_max={raw_delta_max:.4f} > --reject-action-delta={args.reject_action_delta:.4f}"
            )
        base_command, requested_delta = limit_target_delta(
            selected,
            current,
            args.max_command_delta,
            allow_disable=args.unsafe_disable_command_limits,
        )
        calibrated, calibration = calibrate_selected_action(current, base_command, args)
        if calibration is not None:
            print(
                f"[CALIBRATE] step={step} action_idx={action_idx} "
                f"offset_max={float(np.max(np.abs(calibration['applied_offset']))):.4f} "
                f"right_offset={np.round(calibration['applied_offset'], 5).tolist()} "
                f"clipped={calibration['clipped_indices_right'].tolist()}",
                flush=True,
            )
        command, calibrated_requested_delta = limit_target_delta(
            calibrated,
            current,
            args.max_command_delta,
            allow_disable=args.unsafe_disable_command_limits,
        )
        limited_command = np.clip(command, ARM_Q_MIN, ARM_Q_MAX).astype(np.float32)
        limited_joints = np.flatnonzero(np.abs(limited_command - command) > 1e-7)
        if limited_joints.size:
            print(
                f"[JOINT_LIMIT] step={step} action_idx={action_idx} "
                f"q14_indices={limited_joints.tolist()} "
                f"before={np.round(command[limited_joints], 5).tolist()} "
                f"after={np.round(limited_command[limited_joints], 5).tolist()}",
                flush=True,
            )
        command = limited_command
        command_delta = float(np.max(np.abs(command - current)))
        if command_delta + 1e-9 < requested_delta:
            print(
                f"[LIMIT] step={step} action_idx={action_idx} "
                f"requested_delta={requested_delta:.4f} command_delta={command_delta:.4f}",
                flush=True,
            )
        last_command = command.copy()
        print(f"[EXEC] step={step} action_idx={action_idx} target={np.round(command, 4).tolist()}", flush=True)
        if motor_controller is not None and motor_controller.enabled:
            motor_action = motor_controller.pulse(policy_action, 1.0 / args.control_freq)
            print(
                f"[MOTOR] step={step} action_idx={action_idx} action={motor_action} "
                f"indices={motor_controller.action_indices}",
                flush=True,
            )
        holder.set_target(command, weight=1.0)
        arrival_enabled = args.action_arrival_tolerance > 0.0
        arrival_ok = not arrival_enabled
        arrival_samples = 0
        controlled = controlled_joint_slice(args.control_arm)
        deadline = started + args.action_arrival_timeout
        measured = current
        error_by_joint = np.abs(measured - command)
        while True:
            holder.raise_if_failed()
            elapsed = time.monotonic() - started
            if not arrival_enabled:
                time.sleep(max(0.0, period - elapsed))
                measured = arm_client.read_arm_q()
                error_by_joint = np.abs(measured - command)
                break
            measured = arm_client.read_arm_q()
            error_by_joint = np.abs(measured - command)
            controlled_error_max = float(np.max(error_by_joint[controlled]))
            if elapsed >= period and controlled_error_max <= args.action_arrival_tolerance:
                arrival_samples += 1
                if arrival_samples >= args.action_arrival_settle_samples:
                    arrival_ok = True
                    break
            else:
                arrival_samples = 0
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0 / args.action_arrival_poll_hz)
        elapsed = time.monotonic() - started
        controlled_error_max = float(np.max(error_by_joint[controlled]))
        holder_snapshot = holder.snapshot()
        log_event(
            args,
            "execute_action",
            step=step,
            action_idx=action_idx,
            current=current,
            raw_action=raw_action,
            selected=selected,
            base_command=base_command,
            calibrated_action=calibrated,
            calibration=calibration,
            command=command,
            measured=measured,
            raw_delta_max=raw_delta_max,
            requested_delta=requested_delta,
            calibrated_requested_delta=calibrated_requested_delta,
            command_delta=command_delta,
            joint_limited_indices=limited_joints,
            target_error_max=float(np.max(np.abs(measured - command))),
            controlled_error_max=controlled_error_max,
            target_error_by_joint=error_by_joint,
            **holder_snapshot,
            arrival_enabled=arrival_enabled,
            arrival_ok=arrival_ok,
            arrival_elapsed_s=elapsed,
            arrival_samples=arrival_samples,
        )
        if arrival_enabled:
            status = "ARRIVED" if arrival_ok else "TIMEOUT"
            print(
                f"[{status}] step={step} action_idx={action_idx} elapsed={elapsed:.3f}s "
                f"controlled_error_max={controlled_error_max:.4f} "
                f"tolerance={args.action_arrival_tolerance:.4f} "
                f"samples={arrival_samples}/{args.action_arrival_settle_samples}",
                flush=True,
            )
        if arrival_enabled and not arrival_ok and args.action_arrival_timeout_policy == "abort":
            raise RuntimeError(
                f"action arrival timeout at step={step} action_idx={action_idx}: "
                f"controlled_error_max={controlled_error_max:.4f} > "
                f"tolerance={args.action_arrival_tolerance:.4f}"
            )
    holder.raise_if_failed()
    if last_command is None:
        raise RuntimeError("no action executed")
    return last_command


def release_kwargs_from_holder(holder: ArmSdkTargetHold | None) -> dict[str, np.ndarray]:
    if holder is None:
        return {}
    snapshot = holder.snapshot()
    kwargs: dict[str, np.ndarray] = {"tau14": np.asarray(snapshot["applied_tau"], dtype=np.float32)}
    if snapshot.get("tau_rate_limit_enabled", False):
        kwargs["tau_rate_limit"] = np.asarray(snapshot["tau_rate_limit"], dtype=np.float32)
    return kwargs


def release_arm_sdk(
    arm_client: OfficialH2ArmSdk,
    current: np.ndarray,
    release_s: float,
    *,
    tau14: np.ndarray | None = None,
    tau_rate_limit: np.ndarray | None = None,
) -> None:
    if release_s <= 0.0:
        return
    start_tau = np.zeros(14, dtype=np.float32) if tau14 is None else np.asarray(tau14, dtype=np.float32).copy()
    if start_tau.shape != (14,) or not np.all(np.isfinite(start_tau)):
        raise ValueError("release tau14 must be a finite q14 array")
    rate_limit = None if tau_rate_limit is None else np.asarray(tau_rate_limit, dtype=np.float32)
    if rate_limit is not None and (rate_limit.shape != (14,) or np.any(rate_limit <= 0.0)):
        raise ValueError("release tau_rate_limit must contain 14 positive values")
    if rate_limit is not None:
        min_tau_release_s = float(np.max(np.abs(start_tau) / rate_limit))
        release_s = max(float(release_s), min_tau_release_s)
    started = time.monotonic()
    last_tau = start_tau.copy()
    last_tau_at = started
    while True:
        now = time.monotonic()
        elapsed = now - started
        if elapsed >= release_s:
            break
        ratio = min(max(elapsed / release_s, 0.0), 1.0)
        desired_tau = (1.0 - ratio) * start_tau
        if rate_limit is None:
            send_tau = desired_tau.astype(np.float32)
        else:
            dt = max(0.0, min(now - last_tau_at, 0.04))
            send_tau = (last_tau + np.clip(desired_tau - last_tau, -rate_limit * dt, rate_limit * dt)).astype(np.float32)
        arm_client._write_arm_command(current, weight=1.0 - ratio, tau14=send_tau)
        last_tau = send_tau
        last_tau_at = now
        time.sleep(0.02)
    arm_client._write_arm_command(current, weight=0.0, tau14=np.zeros(14, dtype=np.float32))


def restore_start_pose(
    arm_client: OfficialH2ArmSdk,
    startup_q14: np.ndarray | None,
    args: argparse.Namespace,
    *,
    reason: str,
    holder: ArmSdkTargetHold | None = None,
) -> np.ndarray | None:
    if not args.restore_on_exit:
        return None
    current = arm_client.read_arm_q()
    if args.restore_pose_file:
        restore_target = load_init_pose(args.restore_pose_file)
        restore_source = str(resolve_project_path(args.restore_pose_file))
    else:
        if startup_q14 is None:
            return None
        restore_target = startup_q14
        restore_source = "startup_restore_pose"
    control_arm = args.restore_control_arm or "both"
    target = keep_selected_arm(restore_target, current, control_arm)
    delta = float(np.max(np.abs(target - current)))
    print(
        f"[RESTORE] reason={reason} source={restore_source} control_arm={control_arm} "
        f"delta_max={delta:.4f} duration={args.restore_duration:.2f}s",
        flush=True,
    )
    arrival_ok = False
    if holder is None:
        result = arm_client.move_absolute(
            target,
            duration_s=args.restore_duration,
            hold_s=args.restore_hold,
            release_s=0.0,
        )
        held = result.held
        moved_max = result.moved_max
        target_error_max = result.target_error_max
        arrival_ok = target_error_max <= 0.01
    else:
        before = current.copy()
        holder.set_target(target, weight=1.0)
        controlled = controlled_joint_slice(control_arm)
        max_velocity = max(float(args.max_command_velocity), 0.01)
        timeout_s = max(float(args.restore_duration), delta / max_velocity + 1.0)
        deadline = time.monotonic() + timeout_s
        stable = 0
        held = current
        while time.monotonic() < deadline:
            holder.raise_if_failed()
            held = arm_client.read_arm_q()
            controlled_error = float(np.max(np.abs(held[controlled] - target[controlled])))
            if controlled_error <= 0.01:
                stable += 1
                if stable >= 25:
                    arrival_ok = True
                    break
            else:
                stable = 0
            time.sleep(0.01)
        if args.restore_hold > 0.0:
            time.sleep(args.restore_hold)
        held = arm_client.read_arm_q()
        moved_max = float(np.max(np.abs(held - before)))
        target_error_max = float(np.max(np.abs(held - target)))
        arrival_ok = arrival_ok or target_error_max <= 0.01
    print(
        f"[RESTORE] mode={'closed_loop' if holder is not None else 'legacy'} "
        f"arrival_ok={arrival_ok} moved_max={moved_max:.4f} "
        f"target_error_max={target_error_max:.4f}",
        flush=True,
    )
    log_event(
        args,
        "restore_start_pose",
        reason=reason,
        source=restore_source,
        control_arm=control_arm,
        startup_q14=startup_q14,
        target=target,
        held=held,
        moved_max=moved_max,
        target_error_max=target_error_max,
        arrival_ok=arrival_ok,
        controller_mode="closed_loop" if holder is not None else "legacy",
    )
    return held


def print_action_summary(step: int, state: np.ndarray, actions: np.ndarray) -> None:
    arm_actions = actions[:, :14]
    first_delta = arm_actions[0] - state
    print(
        f"[STEP {step}] state_min={state.min():.4f} state_max={state.max():.4f} "
        f"action_min={actions.min():.4f} action_max={actions.max():.4f} "
        f"first_delta_max={np.max(np.abs(first_delta)):.4f} "
        f"right_first_delta_max={np.max(np.abs(first_delta[7:])):.4f}",
        flush=True,
    )
    print("[ACTION TABLE] right arm horizon summary", flush=True)
    for offset, name in enumerate(ARM_NAMES[7:], start=7):
        values = arm_actions[:, offset]
        print(
            f"  {name:<20s} state={state[offset]: .4f} "
            f"first={arm_actions[0, offset]: .4f} last={arm_actions[-1, offset]: .4f} "
            f"min={values.min(): .4f} max={values.max(): .4f} "
            f"first_delta={arm_actions[0, offset] - state[offset]: .4f} "
            f"last_delta={arm_actions[-1, offset] - state[offset]: .4f}",
            flush=True,
        )
    print("[ACTION TABLE] left arm horizon summary", flush=True)
    for offset, name in enumerate(ARM_NAMES[:7]):
        values = arm_actions[:, offset]
        print(
            f"  {name:<20s} state={state[offset]: .4f} "
            f"first={arm_actions[0, offset]: .4f} last={arm_actions[-1, offset]: .4f} "
            f"min={values.min(): .4f} max={values.max(): .4f} "
            f"first_delta={arm_actions[0, offset] - state[offset]: .4f} "
            f"last_delta={arm_actions[-1, offset] - state[offset]: .4f}",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://192.168.61.228:8080")
    parser.add_argument("--instruction", default="change the switch from remote to close")
    parser.add_argument("--network-interface", default="enp86s0")
    parser.add_argument("--dds-domain", type=int, default=0)
    parser.add_argument("--img-server-ip", default="127.0.0.1")
    parser.add_argument("--img-request-port", type=int, default=60000)
    parser.add_argument("--image-camera", default="head_camera")
    parser.add_argument("--left-wrist-camera", default="torso_camera")
    parser.add_argument("--right-wrist-camera", default="right_wrist_camera")
    parser.add_argument("--image-timeout", type=float, default=5.0)
    parser.add_argument("--missing-camera-policy", choices=["duplicate-main", "black", "error"], default="error")
    parser.add_argument(
        "--left-wrist-fallback-image",
        default="",
        help="Static image used only when --left-wrist-camera is unavailable.",
    )
    parser.add_argument(
        "--left-wrist-static-image",
        default="",
        help="Static image always used for the left_wrist_image model slot.",
    )
    parser.add_argument("--fallback-image-height", type=int, default=480)
    parser.add_argument("--fallback-image-width", type=int, default=640)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument(
        "--state-tail-zeros",
        type=int,
        default=0,
        help="Append this many zero values to the model state vector, e.g. rubber EE dims.",
    )
    parser.add_argument(
        "--state-tail-values",
        default="",
        help="Comma-separated state tail defaults. Use 0.5 for each motor direction dimension.",
    )
    parser.add_argument(
        "--motor-action-indices",
        default="",
        help="Comma-separated absolute policy action indices for the motor, from dataset metadata.",
    )
    parser.add_argument("--motor-control-url", default="http://127.0.0.1:18099/api/motor/control")
    parser.add_argument("--motor-left-max", type=float, default=0.25)
    parser.add_argument("--motor-right-min", type=float, default=0.75)
    parser.add_argument(
        "--extra-action-dims-policy",
        choices=["reject", "crop"],
        default="reject",
        help="How to handle model actions wider than the 14D H2 arm command.",
    )
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument(
        "--observation-horizon",
        type=int,
        default=1,
        help="Number of recent observations to include in the request. Current server uses the latest item.",
    )
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--action-horizon", type=int, default=16)
    parser.add_argument("--exe-steps", type=int, default=16)
    parser.add_argument("--action-start-index", type=int, default=0)
    parser.add_argument(
        "--auto-action-start",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Skip leading low-motion frames in the returned action horizon before executing.",
    )
    parser.add_argument(
        "--auto-action-min-delta",
        type=float,
        default=0.04,
        help="Controlled-arm delta threshold used by --auto-action-start.",
    )
    parser.add_argument(
        "--auto-action-max-start",
        type=int,
        default=12,
        help="Largest horizon index that --auto-action-start may select.",
    )
    parser.add_argument("--control-freq", type=float, default=15.0)
    parser.add_argument(
        "--action-arrival-tolerance",
        type=float,
        default=0.0,
        help="Max controlled-joint position error in radians before advancing. Set <=0 to disable.",
    )
    parser.add_argument(
        "--action-arrival-timeout",
        type=float,
        default=0.8,
        help="Maximum seconds to wait for each action target when arrival gating is enabled.",
    )
    parser.add_argument(
        "--action-arrival-poll-hz",
        type=float,
        default=100.0,
        help="Measured-q polling frequency while waiting for action arrival.",
    )
    parser.add_argument(
        "--action-arrival-settle-samples",
        type=int,
        default=3,
        help="Consecutive in-tolerance measurements required before advancing.",
    )
    parser.add_argument(
        "--action-arrival-timeout-policy",
        choices=["abort", "continue"],
        default="abort",
        help="Whether an action arrival timeout aborts safely or advances to the next action.",
    )
    parser.add_argument(
        "--arm-sdk-publish-hz",
        type=float,
        default=250.0,
        help="Independent rt/arm_sdk target hold publish frequency.",
    )
    parser.add_argument(
        "--arm-sdk-telemetry-period",
        type=float,
        default=1.0,
        help="Seconds between holder rate/error diagnostics. Set 0 to disable.",
    )
    parser.add_argument(
        "--arm-feedback-gain",
        type=float,
        default=0.6,
        help="Outer-loop position error gain applied to the held ArmSDK target. Set 0 to disable.",
    )
    parser.add_argument(
        "--arm-feedback-max-offset",
        type=float,
        default=0.12,
        help="Per-joint radian limit for outer-loop target correction. Set 0 to disable.",
    )
    parser.add_argument("--arm-feedback-ki", type=float, default=0.8)
    parser.add_argument("--arm-feedback-integral-zone", type=float, default=0.15)
    parser.add_argument("--arm-feedback-max-integral", type=float, default=0.08)
    parser.add_argument(
        "--gravity-model-cache",
        default=str(PROJECT_ROOT / "h2_model_cache.pkl"),
        help="XR Pinocchio reduced-model cache. Set '' to disable gravity feedforward.",
    )
    parser.add_argument(
        "--gravity-ramp-seconds",
        type=float,
        default=None,
        help="Deprecated alias for --tau-activation-blend-seconds.",
    )
    parser.add_argument(
        "--tau-activation-blend-seconds",
        type=float,
        default=None,
        help="Seconds used to blend tau continuously when the holder first accepts an active target.",
    )
    parser.add_argument(
        "--tau-rate-limit",
        default=DEFAULT_TAU_RATE_LIMIT_CSV,
        help="Comma-separated 14D per-joint tau slew-rate limits in Nm/s, H2 q14 joint order.",
    )
    parser.add_argument("--control-arm", choices=["right", "left", "both"], default="right")
    add_deployment_args(parser)
    parser.add_argument("--max-command-delta", type=float, default=0.18)
    parser.add_argument("--max-command-velocity", type=float, default=0.3)
    parser.add_argument(
        "--unsafe-disable-command-limits",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Diagnostics only. Refused with --execute; q continuity limits stay enabled by default.",
    )
    parser.add_argument(
        "--unsafe-disable-tau-rate-limit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Diagnostics only. Refused with --execute; tau slew-rate limiting stays enabled by default.",
    )
    parser.add_argument(
        "--reject-action-delta",
        type=float,
        default=0.45,
        help="Reject model actions whose max joint jump exceeds this value. Set <=0 to disable.",
    )
    parser.add_argument(
        "--max-abs-q",
        type=float,
        default=4.5,
        help="Reject model actions whose absolute joint value exceeds this value. Set <=0 to disable.",
    )
    parser.add_argument("--init-pose-file", default=str(PROJECT_ROOT / "config" / "h2_pose_init.json"))
    parser.add_argument("--init-pose-duration", type=float, default=2.0)
    parser.add_argument("--init-pose-hold", type=float, default=0.3)
    parser.add_argument("--max-init-delta", type=float, default=0.8)
    parser.add_argument(
        "--init-control-arm",
        choices=["", "right", "left", "both"],
        default="",
        help="Arm selection for --init-pose-file. Empty means reuse --control-arm.",
    )
    parser.add_argument(
        "--pre-init-pose-file",
        default="",
        help="Optional pose applied before --init-pose-file, useful for lifting the arm into the task workspace.",
    )
    parser.add_argument(
        "--pre-init-pose-sequence",
        nargs="*",
        default=[],
        help="Optional ordered pose files applied before VLA, e.g. h2_pose_07 ... h2_pose_01.",
    )
    parser.add_argument(
        "--skip-init-after-pre-sequence",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After --pre-init-pose-sequence, skip --init-pose-file by default so the final sequence pose is preserved.",
    )
    parser.add_argument("--pre-init-pose-duration", type=float, default=5.0)
    parser.add_argument("--pre-init-pose-hold", type=float, default=0.5)
    parser.add_argument("--max-pre-init-delta", type=float, default=2.5)
    parser.add_argument(
        "--pre-init-control-arm",
        choices=["", "right", "left", "both"],
        default="",
        help="Arm selection for --pre-init-pose-file. Empty means reuse --control-arm.",
    )
    parser.add_argument(
        "--pre-vla-trajectory-csv",
        default="",
        help="Optional recorded/player CSV segment to execute before the VLA loop.",
    )
    parser.add_argument("--pre-vla-trajectory-start", type=int, default=0)
    parser.add_argument(
        "--pre-vla-trajectory-end",
        type=int,
        default=0,
        help="Exclusive end frame for --pre-vla-trajectory-csv. 0 means use the end of the CSV.",
    )
    parser.add_argument("--pre-vla-trajectory-stride", type=int, default=1)
    parser.add_argument(
        "--pre-vla-trajectory-max-frames",
        type=int,
        default=0,
        help="Maximum pre-VLA trajectory frames after slicing. 0 means no cap.",
    )
    parser.add_argument("--pre-vla-trajectory-freq", type=float, default=50.0)
    parser.add_argument("--pre-vla-trajectory-publish-mode", choices=["hold", "direct"], default="hold")
    parser.add_argument("--pre-vla-trajectory-publish-hz", type=float, default=50.0)
    parser.add_argument("--pre-vla-trajectory-hold", type=float, default=0.5)
    parser.add_argument("--pre-vla-trajectory-max-start-delta", type=float, default=1.5)
    parser.add_argument("--pre-vla-trajectory-max-step-delta", type=float, default=0.35)
    parser.add_argument("--pre-vla-trajectory-max-command-delta", type=float, default=0.8)
    parser.add_argument(
        "--pre-vla-trajectory-control-arm",
        choices=["", "right", "left", "both"],
        default="",
        help="Arm selection for --pre-vla-trajectory-csv. Empty means reuse --control-arm.",
    )
    parser.add_argument(
        "--pre-vla-only",
        action="store_true",
        help="Execute init/pre-VLA trajectory, then restore and exit without requesting the policy.",
    )
    parser.add_argument("--skip-init-pose", action="store_true")
    parser.add_argument("--final-hold-seconds", type=float, default=0.5)
    parser.add_argument("--release-seconds", type=float, default=1.0)
    parser.add_argument(
        "--restore-on-exit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Return the arm to the q14 captured at program startup before releasing ArmSDK.",
    )
    parser.add_argument(
        "--restore-pose-file",
        default=str(PROJECT_ROOT / "config" / "h2_pose_init.json"),
        help="Pose JSON used for safety restore on exit. Set to '' to restore the startup q14 instead.",
    )
    parser.add_argument("--restore-duration", type=float, default=4.0)
    parser.add_argument("--restore-hold", type=float, default=0.5)
    parser.add_argument(
        "--restore-control-arm",
        choices=["", "right", "left", "both"],
        default="both",
        help="Arm selection for startup-pose restore. Empty/both restores both arms.",
    )
    parser.add_argument(
        "--log-jsonl",
        default=str(PROJECT_ROOT / "logs" / "h2_openpi_official_vla_last.jsonl"),
        help="Write per-step observations, model actions, commands, and measured qpos to this JSONL file. Use '' to disable.",
    )
    parser.add_argument(
        "--debug-image-dir",
        default="",
        help="Optional directory to save the exact RGB images sent to the policy at each step.",
    )
    parser.add_argument("--append-log", action="store_true", help="Append to --log-jsonl instead of replacing it at startup.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    args = parse_args()
    motor_controller = configure_policy_layout(args)
    if args.execute and not args.confirm_execute:
        raise SystemExit("Refusing to execute: pass both --execute and --confirm-execute")
    if args.steps <= 0:
        raise ValueError("--steps must be positive")
    if args.exe_steps <= 0 or args.action_horizon <= 0:
        raise ValueError("--exe-steps and --action-horizon must be positive")
    if args.observation_horizon <= 0:
        raise ValueError("--observation-horizon must be positive")
    if args.auto_action_max_start < 0:
        raise ValueError("--auto-action-max-start must be >= 0")
    if args.auto_action_min_delta < 0.0:
        raise ValueError("--auto-action-min-delta must be >= 0")
    if args.action_start_index < 0:
        raise ValueError("--action-start-index must be >= 0")
    if args.action_start_index >= args.action_horizon:
        raise ValueError("--action-start-index must be < --action-horizon")
    if args.control_freq <= 0.0:
        raise ValueError("--control-freq must be positive")
    if args.action_arrival_timeout <= 0.0:
        raise ValueError("--action-arrival-timeout must be positive")
    if args.action_arrival_poll_hz <= 0.0:
        raise ValueError("--action-arrival-poll-hz must be positive")
    if args.action_arrival_settle_samples <= 0:
        raise ValueError("--action-arrival-settle-samples must be positive")
    if args.arm_sdk_publish_hz <= 0.0:
        raise ValueError("--arm-sdk-publish-hz must be positive")
    if args.arm_sdk_telemetry_period < 0.0:
        raise ValueError("--arm-sdk-telemetry-period must be >= 0")
    finalize_arm_safety_args(args)
    if args.gravity_model_cache and not Path(args.gravity_model_cache).expanduser().exists():
        raise FileNotFoundError(f"gravity model cache not found: {args.gravity_model_cache}")
    if args.init_pose_duration <= 0.0:
        raise ValueError("--init-pose-duration must be positive")
    if args.init_pose_hold < 0.0:
        raise ValueError("--init-pose-hold must be >= 0")
    if args.pre_init_pose_duration <= 0.0:
        raise ValueError("--pre-init-pose-duration must be positive")
    if args.pre_init_pose_hold < 0.0:
        raise ValueError("--pre-init-pose-hold must be >= 0")
    if args.max_pre_init_delta <= 0.0:
        raise ValueError("--max-pre-init-delta must be positive")
    if args.pre_vla_trajectory_freq <= 0.0:
        raise ValueError("--pre-vla-trajectory-freq must be positive")
    if args.pre_vla_trajectory_publish_hz <= 0.0:
        raise ValueError("--pre-vla-trajectory-publish-hz must be positive")
    if args.pre_vla_trajectory_hold < 0.0:
        raise ValueError("--pre-vla-trajectory-hold must be >= 0")
    if args.pre_vla_trajectory_start < 0:
        raise ValueError("--pre-vla-trajectory-start must be >= 0")
    if args.pre_vla_trajectory_stride <= 0:
        raise ValueError("--pre-vla-trajectory-stride must be positive")
    if args.pre_vla_trajectory_max_frames < 0:
        raise ValueError("--pre-vla-trajectory-max-frames must be >= 0")
    if args.final_hold_seconds < 0.0:
        raise ValueError("--final-hold-seconds must be >= 0")
    if args.restore_duration <= 0.0:
        raise ValueError("--restore-duration must be positive")
    if args.restore_hold < 0.0:
        raise ValueError("--restore-hold must be >= 0")
    if not 1 <= args.jpeg_quality <= 100:
        raise ValueError("--jpeg-quality must be in [1,100]")

    calibrator = prepare_action_calibration(args)
    print("[INFO] H2 joint order:", ", ".join(ARM_NAMES), flush=True)
    print(
        f"[INFO] mode={'EXECUTE' if args.execute else 'DRY-RUN'} server={args.server} "
        f"control_arm={args.control_arm} max_command_delta={args.max_command_delta:.4f}",
        flush=True,
    )
    if args.log_jsonl:
        log_path = Path(args.log_jsonl).expanduser()
        if not log_path.is_absolute():
            log_path = PROJECT_ROOT / log_path
        if not args.append_log:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        print(f"[INFO] JSONL log: {log_path}", flush=True)
        log_event(
            args,
            "start",
            argv=sys.argv,
            instruction=args.instruction,
            control_arm=args.control_arm,
            max_command_delta=args.max_command_delta,
            max_command_velocity=args.max_command_velocity,
            unsafe_disable_command_limits=args.unsafe_disable_command_limits,
            tau_activation_blend_seconds=args.tau_activation_blend_seconds,
            tau_rate_limit=args.tau_rate_limit_array,
            unsafe_disable_tau_rate_limit=args.unsafe_disable_tau_rate_limit,
            reject_action_delta=args.reject_action_delta,
            skip_init_pose=args.skip_init_pose,
            pre_init_pose_file=args.pre_init_pose_file,
            pre_init_pose_sequence=args.pre_init_pose_sequence,
            pre_vla_trajectory_csv=args.pre_vla_trajectory_csv,
            restore_on_exit=args.restore_on_exit,
            restore_pose_file=args.restore_pose_file,
            restore_control_arm=args.restore_control_arm or "both",
            skip_init_after_pre_sequence=args.skip_init_after_pre_sequence,
            init_pose_file=args.init_pose_file,
            pre_init_control_arm=args.pre_init_control_arm or args.control_arm,
            init_control_arm=args.init_control_arm or args.control_arm,
            action_calibration_model=str(calibrator.path) if calibrator is not None else "",
            action_calibration_kind=calibrator.kind if calibrator is not None else "",
            action_calibration_blend=args.action_calibration_blend,
            action_calibration_max_offset=args.action_calibration_max_offset,
        )
    init_dds(args.network_interface, args.dds_domain)
    arm_client = OfficialH2ArmSdk()
    arm_client.init()
    arm_client.wait_low_state()
    startup_q14 = arm_client.read_arm_q().copy()
    format_q("startup_restore_pose", startup_q14)
    log_event(args, "startup_restore_pose", q14=startup_q14)
    restored_on_success = False
    if args.execute and not args.skip_init_pose:
        move_to_init_pose(arm_client, args)
    if args.execute:
        execute_pre_vla_trajectory(arm_client, args)
        if args.pre_vla_only:
            if args.debug_image_dir:
                image_client_cls = import_image_client()
                image_client = image_client_cls(host=args.img_server_ip, request_port=args.img_request_port, request_bgr=True)
                try:
                    obs = read_observation(image_client, arm_client, args)
                    save_debug_images(args, obs, 0)
                    log_event(
                        args,
                        "pre_vla_only_observation",
                        state=obs.state,
                        image=image_stats(obs.image),
                        left_wrist_image=image_stats(obs.left_wrist_image),
                        right_wrist_image=image_stats(obs.right_wrist_image),
                    )
                finally:
                    try:
                        image_client.close()
                    except Exception as close_exc:
                        print(f"[WARN] image client close failed: {close_exc!r}", flush=True)
            restored = restore_start_pose(arm_client, startup_q14, args, reason="pre_vla_only")
            release_arm_sdk(
                arm_client,
                restored if restored is not None else arm_client.read_arm_q(),
                args.release_seconds,
            )
            print("[DONE]", flush=True)
            return 0
    holder: ArmSdkTargetHold | None = None
    if args.execute:
        holder = ArmSdkTargetHold(
            arm_client,
            publish_hz=args.arm_sdk_publish_hz,
            max_velocity=args.max_command_velocity,
            telemetry_period_s=args.arm_sdk_telemetry_period,
            feedback_gain=args.arm_feedback_gain,
            feedback_max_offset=args.arm_feedback_max_offset,
            feedback_arm="both",
            feedback_ki=args.arm_feedback_ki,
            feedback_integral_zone=args.arm_feedback_integral_zone,
            feedback_max_integral=args.arm_feedback_max_integral,
            gravity_model_cache=args.gravity_model_cache,
            tau_activation_blend_s=args.tau_activation_blend_seconds,
            tau_rate_limit=args.tau_rate_limit_array,
            unsafe_disable_command_limits=args.unsafe_disable_command_limits,
            unsafe_disable_tau_rate_limit=args.unsafe_disable_tau_rate_limit,
        )
        holder.start()
        holder.set_target(arm_client.read_arm_q(), weight=1.0, activate=False)
    image_client_cls = import_image_client()
    image_client = image_client_cls(host=args.img_server_ip, request_port=args.img_request_port, request_bgr=True)

    first_state: np.ndarray | None = None
    last_target: np.ndarray | None = None
    obs_history: list[Observation] = []
    try:
        for step in range(args.steps):
            obs = read_observation(image_client, arm_client, args)
            attach_policy_state(obs, args, motor_controller)
            obs_history.append(obs)
            if len(obs_history) > args.observation_horizon:
                obs_history = obs_history[-args.observation_horizon :]
            save_debug_images(args, obs, step)
            if first_state is None:
                first_state = obs.state.copy()
                format_q("initial", first_state)
            actions = validate_actions(request_actions(args, obs_history), args)
            action_start_index = choose_action_start_index(actions, obs.state, args)
            horizon_deltas = controlled_delta_by_index(actions, obs.state, args.control_arm)
            print_action_summary(step, obs.state, actions)
            print(
                f"[HORIZON] step={step} start_index={action_start_index} "
                f"controlled_delta={np.round(horizon_deltas, 4).tolist()}",
                flush=True,
            )
            log_event(
                args,
                "policy_actions",
                step=step,
                observation_count=len(obs_history),
                action_start_index=action_start_index,
                horizon_controlled_delta=horizon_deltas,
                state=obs.state,
                actions=actions,
                image=image_stats(obs.image),
                left_wrist_image=image_stats(obs.left_wrist_image),
                right_wrist_image=image_stats(obs.right_wrist_image),
                first_delta=actions[0, :14] - obs.state,
                last_delta=actions[min(len(actions), args.exe_steps) - 1, :14] - obs.state,
            )
            if args.execute:
                if holder is None:
                    raise RuntimeError("internal error: holder not initialized")
                last_target = execute_chunk(
                    arm_client,
                    holder,
                    actions,
                    args,
                    step=step,
                    action_start_index=action_start_index,
                    motor_controller=motor_controller,
                )
        if args.execute and first_state is not None and last_target is not None:
            if holder is not None:
                holder.set_target(last_target, weight=1.0)
                time.sleep(args.final_hold_seconds)
            held = arm_client.read_arm_q()
            print(
                f"[VERIFY] moved_max={np.max(np.abs(held - first_state)):.4f} "
                f"target_error_max={np.max(np.abs(held - last_target)):.4f}",
                flush=True,
            )
            log_event(
                args,
                "verify",
                first_state=first_state,
                last_target=last_target,
                held=held,
                moved_max=float(np.max(np.abs(held - first_state))),
                target_error_max=float(np.max(np.abs(held - last_target))),
            )
            restored = restore_start_pose(
                arm_client,
                startup_q14,
                args,
                reason="success",
                holder=holder,
            )
            restored_on_success = restored is not None
            release_kwargs = release_kwargs_from_holder(holder)
            if holder is not None:
                holder.clear()
            release_arm_sdk(
                arm_client,
                restored if restored is not None else held,
                args.release_seconds,
                **release_kwargs,
            )
    except BaseException:
        if args.execute:
            restored = None
            try:
                restored = restore_start_pose(
                    arm_client,
                    startup_q14,
                    args,
                    reason="error",
                    holder=holder,
                )
            except Exception as restore_exc:
                print(f"[WARN] arm_sdk closed-loop restore failed after error: {restore_exc!r}", flush=True)
            finally:
                release_kwargs = release_kwargs_from_holder(holder)
                if holder is not None:
                    holder.clear()
            try:
                release_arm_sdk(
                    arm_client,
                    restored if restored is not None else arm_client.read_arm_q(),
                    args.release_seconds,
                    **release_kwargs,
                )
            except Exception as release_exc:
                print(f"[WARN] arm_sdk release failed after error: {release_exc!r}", flush=True)
        raise
    finally:
        try:
            motor_controller.stop()
        except Exception as motor_stop_exc:
            print(f"[WARN] motor stop failed: {motor_stop_exc!r}", flush=True)
        try:
            image_client.close()
        except Exception as close_exc:
            print(f"[WARN] image client close failed: {close_exc!r}", flush=True)
        if holder is not None:
            if args.execute and not restored_on_success:
                holder.clear()
            holder.stop()
    print("[DONE]", flush=True)

    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
