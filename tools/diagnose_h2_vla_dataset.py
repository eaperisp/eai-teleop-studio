#!/usr/bin/env python3
"""Audit an H2 raw dataset and its LeRobot conversion without modifying either."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import numpy as np


GROUPS = ("left_arm", "right_arm")
CAMERAS = ("color_0", "color_1", "color_2")


def qpos(frame: dict, root: str) -> np.ndarray:
    values: list[float] = []
    for group in GROUPS:
        values.extend(frame[root][group]["qpos"])
    return np.asarray(values, dtype=np.float32)


def percentile(values: list[float], points=(0, 50, 90, 99, 100)) -> dict[int, float]:
    if not values:
        return {point: float("nan") for point in points}
    result = np.percentile(np.asarray(values, dtype=np.float64), points)
    return {point: float(value) for point, value in zip(points, result)}


def audit_raw(raw_root: Path) -> tuple[list[Path], list[int]]:
    episodes = sorted(path for path in raw_root.glob("episode_*") if (path / "data.json").is_file())
    frame_counts: list[int] = []
    goals: set[str] = set()
    state_ranges = {group: [] for group in GROUPS}
    action_ranges = {group: [] for group in GROUPS}
    endpoint_motion = {group: [] for group in GROUPS}
    action_state_mae = {lag: [] for lag in range(13)}
    camera_missing = {camera: 0 for camera in CAMERAS}
    camera_total = {camera: 0 for camera in CAMERAS}
    state_sum = np.zeros(14, dtype=np.float64)
    state_square_sum = np.zeros(14, dtype=np.float64)
    action_sum = np.zeros(14, dtype=np.float64)
    action_square_sum = np.zeros(14, dtype=np.float64)
    vector_count = 0
    bad_dims = 0
    nonfinite = 0

    for episode in episodes:
        payload = json.loads((episode / "data.json").read_text(encoding="utf-8"))
        frames = payload.get("data") or []
        frame_counts.append(len(frames))
        goals.add(str((payload.get("text") or {}).get("goal") or ""))
        if not frames:
            continue

        states = np.asarray([qpos(frame, "states") for frame in frames], dtype=np.float32)
        actions = np.asarray([qpos(frame, "actions") for frame in frames], dtype=np.float32)
        if states.shape[1:] != (14,) or actions.shape[1:] != (14,):
            bad_dims += 1
            continue
        if not np.all(np.isfinite(states)) or not np.all(np.isfinite(actions)):
            nonfinite += 1

        state_sum += states.sum(axis=0, dtype=np.float64)
        state_square_sum += np.square(states, dtype=np.float64).sum(axis=0)
        action_sum += actions.sum(axis=0, dtype=np.float64)
        action_square_sum += np.square(actions, dtype=np.float64).sum(axis=0)
        vector_count += len(states)

        for group, section in (("left_arm", slice(0, 7)), ("right_arm", slice(7, 14))):
            state_ranges[group].append(float(np.max(np.ptp(states[:, section], axis=0))))
            action_ranges[group].append(float(np.max(np.ptp(actions[:, section], axis=0))))
            endpoint_motion[group].append(float(np.max(np.abs(states[-1, section] - states[0, section]))))

        for lag in action_state_mae:
            if len(frames) > lag:
                action_state_mae[lag].append(float(np.mean(np.abs(actions[: len(frames) - lag] - states[lag:]))))

        for frame in frames:
            colors = frame.get("colors") or {}
            for camera in CAMERAS:
                camera_total[camera] += 1
                relpath = colors.get(camera)
                if not relpath or not (episode / relpath).is_file():
                    camera_missing[camera] += 1

    print("RAW")
    print(f"episodes={len(episodes)} frames={sum(frame_counts)} goals={sorted(goals)!r}")
    print(f"frame_count_percentiles={percentile(frame_counts)} bad_dims={bad_dims} nonfinite={nonfinite}")
    for group in GROUPS:
        print(
            f"{group}: state_range={percentile(state_ranges[group])} "
            f"action_range={percentile(action_ranges[group])} "
            f"endpoint_motion={percentile(endpoint_motion[group])}"
        )
    mean_mae = {lag: float(np.mean(values)) for lag, values in action_state_mae.items() if values}
    best_lag = min(mean_mae, key=mean_mae.get)
    print(f"action_to_future_state_mae={mean_mae}")
    print(f"best_action_state_lag={best_lag} frames ({best_lag / 30.0:.3f}s), mae={mean_mae[best_lag]:.6f}")
    print(f"camera_missing={camera_missing} camera_total={camera_total}")
    if vector_count:
        state_mean = state_sum / vector_count
        action_mean = action_sum / vector_count
        state_std = np.sqrt(np.maximum(state_square_sum / vector_count - np.square(state_mean), 0.0))
        action_std = np.sqrt(np.maximum(action_square_sum / vector_count - np.square(action_mean), 0.0))
        print(f"global_state_mean={np.round(state_mean, 8).tolist()}")
        print(f"global_state_std={np.round(state_std, 8).tolist()}")
        print(f"global_action_mean={np.round(action_mean, 8).tolist()}")
        print(f"global_action_std={np.round(action_std, 8).tolist()}")
    return episodes, frame_counts


def decode_image_stats(value: dict | None) -> tuple[float, float, float]:
    from PIL import Image

    if not value or not value.get("bytes"):
        return float("nan"), float("nan"), float("nan")
    image = np.asarray(Image.open(io.BytesIO(value["bytes"])).convert("RGB"), dtype=np.uint8)
    black_fraction = float(np.mean(np.all(image <= 2, axis=-1)))
    return float(image.mean()), float(image.std()), black_fraction


def audit_lerobot(lerobot_root: Path, raw_episodes: list[Path], raw_frame_counts: list[int]) -> None:
    import pyarrow.parquet as pq

    info = json.loads((lerobot_root / "meta" / "info.json").read_text(encoding="utf-8"))
    metadata = json.loads((lerobot_root / "metadata.json").read_text(encoding="utf-8"))
    tasks = [json.loads(line) for line in (lerobot_root / "meta" / "tasks.jsonl").read_text().splitlines() if line]
    parquet_files = sorted((lerobot_root / "data").rglob("episode_*.parquet"))
    row_counts: list[int] = []
    compare_errors: list[float] = []
    image_stats: dict[str, list[tuple[float, float, float]]] = {
        "image": [],
        "left_wrist_image": [],
        "right_wrist_image": [],
    }

    sample_indices = sorted({0, len(parquet_files) // 2, len(parquet_files) - 1}) if parquet_files else []
    for episode_index, parquet_file in enumerate(parquet_files):
        table = pq.read_table(parquet_file, columns=["state", "actions"])
        row_counts.append(table.num_rows)
        if episode_index in sample_indices and episode_index < len(raw_episodes):
            raw_payload = json.loads((raw_episodes[episode_index] / "data.json").read_text(encoding="utf-8"))
            raw_frames = raw_payload["data"]
            raw_state = np.asarray([qpos(frame, "states") for frame in raw_frames], dtype=np.float32)
            raw_action = np.asarray([qpos(frame, "actions") for frame in raw_frames], dtype=np.float32)
            converted_state = np.asarray(table["state"].to_pylist(), dtype=np.float32)
            converted_action = np.asarray(table["actions"].to_pylist(), dtype=np.float32)
            if raw_state.shape == converted_state.shape and raw_action.shape == converted_action.shape:
                compare_errors.append(
                    max(
                        float(np.max(np.abs(raw_state - converted_state))),
                        float(np.max(np.abs(raw_action - converted_action))),
                    )
                )
            else:
                compare_errors.append(float("inf"))

            image_table = pq.read_table(parquet_file, columns=list(image_stats))
            for row in sorted({0, image_table.num_rows // 2, image_table.num_rows - 1}):
                for key in image_stats:
                    image_stats[key].append(decode_image_stats(image_table[key][row].as_py()))

    print("LEROBOT")
    print(
        f"episodes={len(parquet_files)} rows={sum(row_counts)} info_frames={info.get('total_frames')} "
        f"fps={info.get('fps')} tasks={tasks!r}"
    )
    print(f"camera_plan={metadata.get('camera_plan')} vector_dims={metadata.get('vector_dims')}")
    print(f"row_counts_match_raw={row_counts == raw_frame_counts}")
    print(f"sample_raw_conversion_max_errors={compare_errors}")
    for key, values in image_stats.items():
        means = [item[0] for item in values]
        stds = [item[1] for item in values]
        black = [item[2] for item in values]
        print(
            f"{key}: sample_mean={percentile(means)} sample_std={percentile(stds)} "
            f"black_fraction={percentile(black)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--lerobot", type=Path, required=True)
    args = parser.parse_args()
    raw_episodes, raw_frame_counts = audit_raw(args.raw)
    audit_lerobot(args.lerobot, raw_episodes, raw_frame_counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
