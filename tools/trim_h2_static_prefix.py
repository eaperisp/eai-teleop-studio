#!/usr/bin/env python3
"""Trim static/waiting prefixes from H2 xr_teleoperate episodes.

The script creates a new raw dataset directory without modifying the source.
Episode image folders are symlinked by default, while data.json is rewritten
with the leading static frames removed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np


def qpos14(frame: dict[str, Any], key: str = "states") -> np.ndarray:
    groups = frame[key]
    left = groups["left_arm"]["qpos"]
    right = groups["right_arm"]["qpos"]
    return np.asarray(left + right, dtype=np.float32)


def choose_trim_index(
    data: list[dict[str, Any]],
    *,
    arm: str,
    move_threshold: float,
    pitch_delta_threshold: float,
    absolute_pitch_threshold: float,
    preroll: int,
) -> tuple[int, int | None, dict[str, float]]:
    if not data:
        return 0, None, {}

    states = np.stack([qpos14(frame, "states") for frame in data])
    sl = slice(7, 14) if arm == "right" else slice(0, 7)
    arm_states = states[:, sl]
    start = arm_states[0]
    move_norm = np.linalg.norm(arm_states - start, axis=1)

    # For H2 right arm in this dataset, shoulder pitch becoming more negative
    # corresponds to lifting/approaching the switch. Keep this as a secondary
    # cue, but use vector motion as the primary, robot-agnostic signal.
    pitch = arm_states[:, 0]
    pitch_delta = start[0] - pitch

    candidates: list[int] = []
    move_hits = np.flatnonzero(move_norm >= move_threshold)
    if len(move_hits):
        candidates.append(int(move_hits[0]))

    pitch_hits = np.flatnonzero(pitch_delta >= pitch_delta_threshold)
    if len(pitch_hits):
        candidates.append(int(pitch_hits[0]))

    absolute_hits = np.flatnonzero(pitch <= absolute_pitch_threshold)
    if len(absolute_hits):
        candidates.append(int(absolute_hits[0]))

    if not candidates:
        stats = {
            "max_move_norm": float(np.max(move_norm)),
            "max_pitch_delta": float(np.max(pitch_delta)),
            "min_pitch": float(np.min(pitch)),
        }
        return 0, None, stats

    trigger = min(candidates)
    trim = max(0, trigger - preroll)
    stats = {
        "trigger_index": float(trigger),
        "trim_index": float(trim),
        "max_move_norm": float(np.max(move_norm)),
        "max_pitch_delta": float(np.max(pitch_delta)),
        "min_pitch": float(np.min(pitch)),
    }
    return trim, trigger, stats


def rewrite_episode(
    src_ep: Path,
    dst_ep: Path,
    *,
    trim_index: int,
    link_images: str,
) -> None:
    with (src_ep / "data.json").open("r", encoding="utf-8") as f:
        doc = json.load(f)

    new_doc = copy.deepcopy(doc)
    frames = copy.deepcopy(doc["data"][trim_index:])
    for new_idx, frame in enumerate(frames):
        frame["idx"] = new_idx
    new_doc["data"] = frames
    new_doc.setdefault("info", {})["trim_static_prefix"] = {
        "source_episode": src_ep.name,
        "trimmed_frames": trim_index,
        "remaining_frames": len(frames),
    }

    dst_ep.mkdir(parents=True, exist_ok=True)
    with (dst_ep / "data.json").open("w", encoding="utf-8") as f:
        json.dump(new_doc, f, ensure_ascii=False, indent=2)

    for child in src_ep.iterdir():
        if child.name == "data.json":
            continue
        target = dst_ep / child.name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if link_images == "symlink":
            os.symlink(child, target, target_is_directory=child.is_dir())
        elif link_images == "hardlink" and child.is_dir():
            shutil.copytree(child, target, copy_function=os.link)
        elif link_images == "copy":
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        else:
            raise ValueError(f"Unsupported --link-images={link_images}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dst", required=True, type=Path)
    parser.add_argument("--arm", choices=["right", "left"], default="right")
    parser.add_argument("--move-threshold", type=float, default=0.15)
    parser.add_argument("--pitch-delta-threshold", type=float, default=0.20)
    parser.add_argument("--absolute-pitch-threshold", type=float, default=-0.60)
    parser.add_argument("--preroll", type=int, default=8)
    parser.add_argument("--min-frames", type=int, default=32)
    parser.add_argument("--link-images", choices=["symlink", "hardlink", "copy"], default="symlink")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    src = args.src.expanduser().resolve()
    dst = args.dst.expanduser()
    episodes = sorted(p for p in src.glob("episode_*") if (p / "data.json").exists())
    if not episodes:
        raise FileNotFoundError(f"No episode_*/data.json found under {src}")

    if dst.exists() and args.overwrite and not args.dry_run:
        shutil.rmtree(dst)
    if dst.exists() and not args.dry_run:
        raise FileExistsError(f"Destination exists: {dst}. Use --overwrite.")

    report: list[dict[str, Any]] = []
    kept = 0
    skipped = 0
    total_trimmed = 0

    for src_ep in episodes:
        with (src_ep / "data.json").open("r", encoding="utf-8") as f:
            doc = json.load(f)
        data = doc.get("data", [])
        trim, trigger, stats = choose_trim_index(
            data,
            arm=args.arm,
            move_threshold=args.move_threshold,
            pitch_delta_threshold=args.pitch_delta_threshold,
            absolute_pitch_threshold=args.absolute_pitch_threshold,
            preroll=args.preroll,
        )
        remaining = len(data) - trim
        entry = {
            "episode": src_ep.name,
            "source_frames": len(data),
            "trim_index": trim,
            "trigger_index": trigger,
            "remaining_frames": remaining,
            "skipped": remaining < args.min_frames,
            **stats,
        }
        report.append(entry)
        if remaining < args.min_frames:
            skipped += 1
            continue
        kept += 1
        total_trimmed += trim
        if not args.dry_run:
            rewrite_episode(src_ep, dst / src_ep.name, trim_index=trim, link_images=args.link_images)

    summary = {
        "source": str(src),
        "destination": str(dst),
        "episodes_total": len(episodes),
        "episodes_kept": kept,
        "episodes_skipped": skipped,
        "total_trimmed_frames": total_trimmed,
        "mean_trimmed_frames_per_kept_episode": total_trimmed / kept if kept else 0.0,
        "parameters": vars(args) | {"src": str(args.src), "dst": str(args.dst)},
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("first_20_report")
    print(json.dumps(report[:20], ensure_ascii=False, indent=2))

    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        with (dst / "trim_static_prefix_report.json").open("w", encoding="utf-8") as f:
            json.dump({"summary": summary, "episodes": report}, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
