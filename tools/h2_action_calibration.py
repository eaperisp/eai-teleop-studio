#!/usr/bin/env python3
"""Fit and apply an H2 right-arm command-to-realized-pose calibration model.

The source XR episodes contain both the commanded joint target (``actions``)
and the measured joint position (``states``).  This module learns the old
controller's realized pose at a configurable feedback lag.  At deployment the
predicted realized pose can be sent to the newer accurate closed-loop
controller, with an explicit per-joint correction limit.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


JOINT_NAMES = (
    "right_shoulder_pitch",
    "right_shoulder_roll",
    "right_shoulder_yaw",
    "right_elbow",
    "right_wrist_roll",
    "right_wrist_pitch",
    "right_wrist_yaw",
)


def add_deployment_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--action-calibration-model",
        default="",
        help="Optional .npz old-controller calibration model. Empty disables calibration.",
    )
    parser.add_argument(
        "--action-calibration-max-offset",
        type=float,
        default=0.08,
        help="Per-joint radian limit applied to the learned correction.",
    )
    parser.add_argument(
        "--action-calibration-blend",
        type=float,
        default=1.0,
        help="Blend from raw action (0) to calibrated action (1).",
    )


def _q7(frame: dict[str, Any], root: str) -> np.ndarray:
    values = (((frame.get(root) or {}).get("right_arm") or {}).get("qpos") or [])
    if len(values) != 7:
        raise ValueError(f"{root}.right_arm.qpos must be 7D, got {len(values)}")
    result = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{root}.right_arm.qpos contains non-finite values")
    return result


def feature_matrix(current: np.ndarray, action: np.ndarray, kind: str) -> np.ndarray:
    current = np.asarray(current, dtype=np.float64)
    action = np.asarray(action, dtype=np.float64)
    if current.shape[-1] != 7 or action.shape != current.shape:
        raise ValueError(f"current/action must end in 7D and match, got {current.shape}/{action.shape}")
    delta = action - current
    linear = np.concatenate((current, action, delta), axis=-1)
    if kind == "linear":
        return linear
    if kind == "nonlinear":
        return np.concatenate(
            (
                linear,
                np.sin(current),
                np.cos(current),
                np.sin(action),
                np.cos(action),
                delta * np.abs(delta),
                current * action,
            ),
            axis=-1,
        )
    raise ValueError(f"unsupported feature kind: {kind}")


def _ridge_fit(
    features: np.ndarray,
    target: np.ndarray,
    ridge: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    mean = np.mean(features, axis=0)
    scale = np.std(features, axis=0)
    scale = np.where(scale < 1e-6, 1.0, scale)
    normalized = (features - mean) / scale
    design = np.concatenate((normalized, np.ones((len(normalized), 1))), axis=1)
    gram = design.T @ design
    penalty = np.eye(gram.shape[0], dtype=np.float64) * float(ridge)
    penalty[-1, -1] = 0.0
    weights = np.linalg.solve(gram + penalty, design.T @ target)
    return mean, scale, weights[:-1], weights[-1]


def _predict(
    current: np.ndarray,
    action: np.ndarray,
    kind: str,
    feature_mean: np.ndarray,
    feature_scale: np.ndarray,
    weights: np.ndarray,
    intercept: np.ndarray,
    feature_lower: np.ndarray | None = None,
    feature_upper: np.ndarray | None = None,
) -> np.ndarray:
    features = feature_matrix(current, action, kind)
    if feature_lower is not None and feature_upper is not None:
        features = np.clip(features, feature_lower, feature_upper)
    residual = ((features - feature_mean) / feature_scale) @ weights + intercept
    return action + residual


def _metrics(prediction: np.ndarray, target: np.ndarray) -> dict[str, Any]:
    error = np.abs(np.asarray(prediction) - np.asarray(target))
    sample_max = np.max(error, axis=1)
    return {
        "count": int(len(error)),
        "mae_rad": float(np.mean(error)),
        "p95_joint_error_rad": float(np.quantile(error, 0.95)),
        "mean_sample_max_rad": float(np.mean(sample_max)),
        "p95_sample_max_rad": float(np.quantile(sample_max, 0.95)),
        "per_joint_mae_rad": np.mean(error, axis=0).tolist(),
        "per_joint_p95_rad": np.quantile(error, 0.95, axis=0).tolist(),
    }


def _episode_is_validation(path: Path, validation_modulus: int) -> bool:
    suffix = path.parent.name.rsplit("_", 1)[-1]
    try:
        episode_number = int(suffix)
    except ValueError:
        episode_number = sum(path.parent.name.encode("utf-8"))
    return episode_number % validation_modulus == 0


def _load_samples(
    paths: Iterable[Path],
    lag_frames: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    current_parts: list[np.ndarray] = []
    action_parts: list[np.ndarray] = []
    future_parts: list[np.ndarray] = []
    episodes: list[str] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        frames = payload.get("data") or []
        if len(frames) <= lag_frames:
            continue
        current = np.asarray([_q7(frame, "states") for frame in frames[:-lag_frames]])
        action = np.asarray([_q7(frame, "actions") for frame in frames[:-lag_frames]])
        future = np.asarray([_q7(frame, "states") for frame in frames[lag_frames:]])
        valid = (
            np.all(np.isfinite(current), axis=1)
            & np.all(np.isfinite(action), axis=1)
            & np.all(np.isfinite(future), axis=1)
        )
        if np.any(valid):
            current_parts.append(current[valid])
            action_parts.append(action[valid])
            future_parts.append(future[valid])
            episodes.append(path.parent.name)
    if not current_parts:
        raise ValueError("no valid calibration samples found")
    return (
        np.concatenate(current_parts),
        np.concatenate(action_parts),
        np.concatenate(future_parts),
        episodes,
    )


def train(args: argparse.Namespace) -> dict[str, Any]:
    dataset_dir = Path(args.dataset_dir).expanduser().resolve()
    paths = [Path(path) for path in sorted(glob.glob(str(dataset_dir / "episode_*" / "data.json")))]
    if not paths:
        raise FileNotFoundError(f"no episode_*/data.json under {dataset_dir}")
    train_paths = [path for path in paths if not _episode_is_validation(path, args.validation_modulus)]
    val_paths = [path for path in paths if _episode_is_validation(path, args.validation_modulus)]
    if not train_paths or not val_paths:
        raise ValueError("episode split produced an empty training or validation set")

    train_current, train_action, train_future, train_episodes = _load_samples(train_paths, args.lag_frames)
    val_current, val_action, val_future, val_episodes = _load_samples(val_paths, args.lag_frames)
    train_residual = train_future - train_action

    candidates: dict[str, dict[str, Any]] = {}
    fixed_residual = np.median(train_residual, axis=0)
    fixed_prediction = val_action + fixed_residual
    candidates["fixed"] = {
        "metrics": _metrics(fixed_prediction, val_future),
        "residual": fixed_residual.tolist(),
    }

    fitted: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for kind in ("linear", "nonlinear"):
        train_features = feature_matrix(train_current, train_action, kind)
        feature_lower = np.quantile(train_features, 0.001, axis=0)
        feature_upper = np.quantile(train_features, 0.999, axis=0)
        model = (*_ridge_fit(train_features, train_residual, args.ridge), feature_lower, feature_upper)
        fitted[kind] = model
        prediction = _predict(val_current, val_action, kind, *model)
        candidates[kind] = {"metrics": _metrics(prediction, val_future)}

    candidates["raw_action"] = {"metrics": _metrics(val_action, val_future)}
    selected_kind = min(
        ("fixed", "linear", "nonlinear"),
        key=lambda name: candidates[name]["metrics"]["p95_sample_max_rad"],
    )
    if selected_kind == "fixed":
        feature_mean = np.zeros(0, dtype=np.float64)
        feature_scale = np.ones(0, dtype=np.float64)
        weights = np.zeros((0, 7), dtype=np.float64)
        intercept = fixed_residual
        feature_lower = np.zeros(0, dtype=np.float64)
        feature_upper = np.zeros(0, dtype=np.float64)
    else:
        feature_mean, feature_scale, weights, intercept, feature_lower, feature_upper = fitted[selected_kind]

    correction_q01 = np.quantile(train_residual, 0.01, axis=0)
    correction_q99 = np.quantile(train_residual, 0.99, axis=0)
    report = {
        "version": 1,
        "created_unix": time.time(),
        "dataset_dir": str(dataset_dir),
        "lag_frames": int(args.lag_frames),
        "data_frequency_hz": float(args.data_frequency_hz),
        "lag_seconds": float(args.lag_frames / args.data_frequency_hz),
        "episode_split": {
            "total": len(paths),
            "training": len(train_paths),
            "validation": len(val_paths),
            "validation_modulus": int(args.validation_modulus),
        },
        "sample_split": {
            "training": int(len(train_current)),
            "validation": int(len(val_current)),
        },
        "selected_kind": selected_kind,
        "ridge": float(args.ridge),
        "joint_names": list(JOINT_NAMES),
        "correction_q01_rad": correction_q01.tolist(),
        "correction_q99_rad": correction_q99.tolist(),
        "candidates": candidates,
        "training_episodes_first_last": [train_episodes[0], train_episodes[-1]],
        "validation_episodes_first_last": [val_episodes[0], val_episodes[-1]],
    }

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output,
        version=np.asarray(1, dtype=np.int64),
        kind=np.asarray(selected_kind),
        lag_frames=np.asarray(args.lag_frames, dtype=np.int64),
        feature_mean=feature_mean,
        feature_scale=feature_scale,
        weights=weights,
        intercept=intercept,
        feature_lower=feature_lower,
        feature_upper=feature_upper,
        correction_q01=correction_q01,
        correction_q99=correction_q99,
        report_json=np.asarray(json.dumps(report, separators=(",", ":"))),
    )
    report_path = Path(args.report).expanduser().resolve() if args.report else output.with_suffix(".report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["output"] = str(output)
    report["report"] = str(report_path)
    return report


@dataclass(frozen=True)
class ActionCalibrator:
    kind: str
    lag_frames: int
    feature_mean: np.ndarray
    feature_scale: np.ndarray
    weights: np.ndarray
    intercept: np.ndarray
    feature_lower: np.ndarray
    feature_upper: np.ndarray
    correction_q01: np.ndarray
    correction_q99: np.ndarray
    report: dict[str, Any]
    path: Path

    @classmethod
    def load(cls, path: str | Path) -> "ActionCalibrator":
        resolved = Path(path).expanduser().resolve()
        with np.load(resolved, allow_pickle=False) as payload:
            return cls(
                kind=str(payload["kind"].item()),
                lag_frames=int(payload["lag_frames"].item()),
                feature_mean=np.asarray(payload["feature_mean"], dtype=np.float64),
                feature_scale=np.asarray(payload["feature_scale"], dtype=np.float64),
                weights=np.asarray(payload["weights"], dtype=np.float64),
                intercept=np.asarray(payload["intercept"], dtype=np.float64),
                feature_lower=np.asarray(payload["feature_lower"], dtype=np.float64),
                feature_upper=np.asarray(payload["feature_upper"], dtype=np.float64),
                correction_q01=np.asarray(payload["correction_q01"], dtype=np.float64),
                correction_q99=np.asarray(payload["correction_q99"], dtype=np.float64),
                report=json.loads(str(payload["report_json"].item())),
                path=resolved,
            )

    def predict_realized(self, current_q7: np.ndarray, action_q7: np.ndarray) -> np.ndarray:
        current = np.asarray(current_q7, dtype=np.float64).reshape(1, 7)
        action = np.asarray(action_q7, dtype=np.float64).reshape(1, 7)
        if self.kind == "fixed":
            return (action[0] + self.intercept).astype(np.float32)
        return _predict(
            current,
            action,
            self.kind,
            self.feature_mean,
            self.feature_scale,
            self.weights,
            self.intercept,
            self.feature_lower,
            self.feature_upper,
        )[0].astype(np.float32)

    def summary(self) -> str:
        metrics = self.report.get("candidates", {}).get(self.kind, {}).get("metrics", {})
        return (
            f"path={self.path} kind={self.kind} lag_frames={self.lag_frames} "
            f"validation_p95_sample_max={float(metrics.get('p95_sample_max_rad', math.nan)):.5f}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-dir",
        default="/home/robot/data/datasets/robot/h2_switch_close_to_remote",
    )
    parser.add_argument("--lag-frames", type=int, default=2)
    parser.add_argument("--data-frequency-hz", type=float, default=30.0)
    parser.add_argument("--validation-modulus", type=int, default=5)
    parser.add_argument("--ridge", type=float, default=1e-3)
    parser.add_argument(
        "--output",
        default="/home/robot/eai_teleoperate_studio/config/h2_action_calibration_v1.npz",
    )
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.lag_frames <= 0:
        raise ValueError("--lag-frames must be positive")
    if args.data_frequency_hz <= 0.0:
        raise ValueError("--data-frequency-hz must be positive")
    if args.validation_modulus < 2:
        raise ValueError("--validation-modulus must be >= 2")
    if args.ridge < 0.0:
        raise ValueError("--ridge must be >= 0")
    report = train(args)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
