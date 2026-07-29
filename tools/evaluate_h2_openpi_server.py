#!/usr/bin/env python3
"""Evaluate an H2 OpenPI HTTP server on frames from the training dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from urllib import request

import numpy as np
from PIL import Image


def qpos(frame: dict, root: str) -> np.ndarray:
    return np.asarray(
        frame[root]["left_arm"]["qpos"] + frame[root]["right_arm"]["qpos"],
        dtype=np.float32,
    )


def load_rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def predict(server: str, episode: Path, frame: dict, prompt: str, timeout: float) -> np.ndarray:
    colors = frame["colors"]
    payload = {
        "observations": [
            {
                "full_image": load_rgb(episode / colors["color_0"]).tolist(),
                # This dataset records head + torso + right wrist. The torso image
                # intentionally occupies OpenPI's otherwise-unused left image slot.
                "left_wrist_image": load_rgb(episode / colors["color_1"]).tolist(),
                "right_wrist_image": load_rgb(episode / colors["color_2"]).tolist(),
                "state": qpos(frame, "states").tolist(),
                "instruction": prompt,
                "task_name": prompt,
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        server.rstrip("/") + "/predict_action",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("result") != "ok":
        raise RuntimeError(result)
    return np.asarray(result["action"], dtype=np.float32)


def select_samples(raw_root: Path, count: int) -> list[tuple[Path, dict, list[dict]]]:
    candidates: list[tuple[Path, list[dict]]] = []
    for episode in sorted(raw_root.glob("episode_*")):
        data_path = episode / "data.json"
        if not data_path.is_file():
            continue
        frames = json.loads(data_path.read_text(encoding="utf-8")).get("data") or []
        if len(frames) < 100:
            continue
        right = np.asarray([qpos(frame, "states")[7:] for frame in frames])
        if float(np.max(np.ptp(right, axis=0))) >= 1.0:
            candidates.append((episode, frames))
    if not candidates:
        raise RuntimeError("No sufficiently long right-arm motion episodes found")

    fractions = np.linspace(0.15, 0.8, count)
    selected: list[tuple[Path, dict, list[dict]]] = []
    for index, fraction in enumerate(fractions):
        episode, frames = candidates[round(index * (len(candidates) - 1) / max(count - 1, 1))]
        frame_index = min(int(round(fraction * (len(frames) - 17))), len(frames) - 17)
        selected.append((episode, frames[frame_index], frames[frame_index : frame_index + 16]))
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--prompts",
        nargs="+",
        default=["change the switch from remote to close", "remote to close"],
    )
    args = parser.parse_args()

    samples = select_samples(args.raw, args.samples)
    metrics: dict[str, list[tuple[float, float, float, float]]] = {prompt: [] for prompt in args.prompts}
    for sample_index, (episode, frame, future_frames) in enumerate(samples):
        state = qpos(frame, "states")
        ground_truth = np.asarray([qpos(item, "actions") for item in future_frames], dtype=np.float32)
        baseline = np.repeat(state[None, :], len(ground_truth), axis=0)
        baseline_all = float(np.mean(np.abs(baseline - ground_truth)))
        baseline_right = float(np.mean(np.abs(baseline[:, 7:] - ground_truth[:, 7:])))
        print(
            f"sample={sample_index} episode={episode.name} frame={frame['idx']} "
            f"baseline_mae={baseline_all:.6f} baseline_right_mae={baseline_right:.6f}",
            flush=True,
        )
        for prompt in args.prompts:
            started = time.time()
            actions = predict(args.server, episode, frame, prompt, args.timeout)
            horizon = min(len(actions), len(ground_truth))
            predicted = actions[:horizon]
            target = ground_truth[:horizon]
            mae_all = float(np.mean(np.abs(predicted - target)))
            mae_right = float(np.mean(np.abs(predicted[:, 7:] - target[:, 7:])))
            first_delta = float(np.max(np.abs(predicted[0] - state)))
            metrics[prompt].append((mae_all, mae_right, baseline_all, baseline_right))
            print(
                f"  prompt={prompt!r} shape={actions.shape} mae={mae_all:.6f} "
                f"right_mae={mae_right:.6f} first_delta_max={first_delta:.6f} "
                f"latency={time.time() - started:.2f}s",
                flush=True,
            )

    print("SUMMARY")
    for prompt, values in metrics.items():
        array = np.asarray(values, dtype=np.float64)
        print(
            f"prompt={prompt!r} model_mae={array[:, 0].mean():.6f} "
            f"model_right_mae={array[:, 1].mean():.6f} "
            f"hold_baseline_mae={array[:, 2].mean():.6f} "
            f"hold_baseline_right_mae={array[:, 3].mean():.6f}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
