#!/usr/bin/env python3
"""Convert xr_teleoperate H2 episodes to LeRobot format for OpenPI training.

The xr_teleoperate recorder writes one directory per episode:

    episode_0001/
      data.json
      colors/000000_color_0.jpg

This script converts those episodes into a LeRobot dataset with the field names
expected by the OpenPI examples: image, wrist_image, state, actions, and task.
Raw states/actions stay unnormalized; OpenPI computes normalization statistics
from the converted LeRobot dataset before training.
"""

from __future__ import annotations

import argparse
import gc
import inspect
import json
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("XR_TELEOP_DATA_DIR", Path.home() / "data")).expanduser()
DEFAULT_LEROBOT_HOME = DEFAULT_DATA_DIR / "datasets" / "lerobot"
DEFAULT_CACHE_ROOT_NAME = "cache"
DEFAULT_CAMERA_MAP = {
    "color_0": "image",
    "color_2": "left_wrist_image",
    "color_3": "right_wrist_image",
}
DEFAULT_TARGET_IMAGE_KEYS = ("image", "left_wrist_image", "right_wrist_image")
CAMERA_PRESET_SOURCES = {
    # color_0=head, color_1=torso, color_2=left wrist, color_3=right wrist.
    # The OpenPI data transform normally consumes three image slots.  Single-hand
    # presets keep the task-relevant wrist view and use torso as the spare slot.
    "dual": {
        "image": "color_0",
        "left_wrist_image": "color_2",
        "right_wrist_image": "color_3",
    },
    "right_hand": {
        "image": "color_0",
        "left_wrist_image": "color_1",
        "right_wrist_image": "color_3",
    },
    "right_hand_3cam": {
        "image": "color_0",
        "left_wrist_image": "color_1",
        "right_wrist_image": "color_2",
    },
    "left_hand": {
        "image": "color_0",
        "left_wrist_image": "color_2",
        "right_wrist_image": "color_1",
    },
    "head_torso": {
        "image": "color_0",
        "left_wrist_image": "color_1",
        "right_wrist_image": None,
    },
    "head_only": {
        "image": "color_0",
        "left_wrist_image": None,
        "right_wrist_image": None,
    },
}

DEFAULT_VECTOR_KEYS = ("left_arm", "right_arm", "left_ee", "right_ee")
NO_CONTROL_END_EFFECTOR_TYPES = {"none", ""}
FIXED_END_EFFECTOR_DIMS = {"rubber": 1}

H2_ARM_JOINT_NAMES = {
    "left_arm": [
        "left_shoulder_pitch_joint",
        "left_shoulder_roll_joint",
        "left_shoulder_yaw_joint",
        "left_elbow_joint",
        "left_wrist_roll_joint",
        "left_wrist_pitch_joint",
        "left_wrist_yaw_joint",
    ],
    "right_arm": [
        "right_shoulder_pitch_joint",
        "right_shoulder_roll_joint",
        "right_shoulder_yaw_joint",
        "right_elbow_joint",
        "right_wrist_roll_joint",
        "right_wrist_pitch_joint",
        "right_wrist_yaw_joint",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_DATA_DIR / "datasets" / "robot" / "h2_pick_red_cup",
        help="xr_teleoperate task directory containing episode_XXXX folders.",
    )
    parser.add_argument(
        "--repo-id",
        default="local/h2_pick_red_cup",
        help="LeRobot repo id. For local-only use, any owner/name string is OK.",
    )
    parser.add_argument(
        "--robot-type",
        default="robot",
        help="LeRobot robot_type metadata.",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Override language instruction. Defaults to data.json text.goal.",
    )
    parser.add_argument(
        "--camera-map",
        default="auto",
        help=(
            "Comma mapping from recorder color key to LeRobot image key, e.g. "
            "color_0:image,color_2:left_wrist_image,color_3:right_wrist_image. "
            "Use 'auto' to infer a 3-camera OpenPI plan."
        ),
    )
    parser.add_argument(
        "--camera-preset",
        choices=("auto", *CAMERA_PRESET_SOURCES),
        default="auto",
        help=(
            "3-camera OpenPI preset used when --camera-map=auto. "
            "right_hand drops the left wrist camera; right_hand_3cam is for head+torso+right wrist recordings."
        ),
    )
    parser.add_argument(
        "--target-image-keys",
        default=",".join(DEFAULT_TARGET_IMAGE_KEYS),
        help="Comma ordered LeRobot image keys to create. Missing sources are filled with black images.",
    )
    parser.add_argument(
        "--image-size",
        default="original",
        help="Output image size as HxW, for example 240x320. Use 'original' to keep source size.",
    )
    parser.add_argument(
        "--image-writer-processes",
        type=int,
        default=0,
        help="LeRobot image writer worker processes. Keep 0 to avoid orphan writer processes.",
    )
    parser.add_argument(
        "--image-writer-threads",
        type=int,
        default=2,
        help="LeRobot image writer threads. Keep low to avoid OOM on long image datasets.",
    )
    parser.add_argument(
        "--keep-external-images",
        action="store_true",
        help="Keep LeRobot's external images/ cache. By default it is removed after each episode because parquet stores image bytes.",
    )
    parser.add_argument(
        "--image-encoding",
        choices=("auto", "jpg", "jpeg", "png"),
        default="auto",
        help="Encoding used by LeRobot's image writer. 'auto' follows source metadata extension: jpg/jpeg stays jpg, png stays png.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=95,
        help="JPEG quality used when --image-encoding resolves to jpg/jpeg. Valid range: 1-100.",
    )
    parser.add_argument(
        "--vector-keys",
        default=",".join(DEFAULT_VECTOR_KEYS),
        help=(
            "Comma ordered qpos groups to concatenate from states/actions. "
            "left_ee/right_ee are dynamic: none contributes 0D, rubber "
            "contributes 1D per side, and active end effectors contribute "
            "their observed qpos dimension."
        ),
    )
    parser.add_argument(
        "--urdf",
        type=Path,
        default=Path("assets/h2/H2.urdf"),
        help="H2 URDF, used only for metadata/validation.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete an existing LeRobot dataset with the same repo id.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an existing LeRobot dataset. Rolls back the last episodes first to avoid half-written data.",
    )
    parser.add_argument(
        "--resume-overlap",
        type=int,
        default=2,
        help="When --resume is used, delete and reconvert this many completed LeRobot episodes.",
    )
    parser.add_argument(
        "--start-episode",
        type=int,
        default=0,
        help="Zero-based completed raw episode index to start converting from.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Batch size. The converter keeps launching the next batch until all selected raw episodes are written.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Conversion cache directory. Defaults to a per-repo directory under "
            "<HF_LEROBOT_HOME>/../cache/convert so ~/.cache is not used."
        ),
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="Keep conversion cache after each batch. By default cache files are removed between batches.",
    )
    parser.add_argument(
        "--trim-leading-static",
        action="store_true",
        help=(
            "Drop static/waiting frames at the start of each episode before writing LeRobot. "
            "The source data is not modified."
        ),
    )
    parser.add_argument(
        "--trim-root-key",
        choices=("states", "actions"),
        default="actions",
        help="Frame root used to detect the first non-static segment.",
    )
    parser.add_argument(
        "--trim-vector-key",
        default="right_arm",
        help="qpos group used to detect motion, for example right_arm or left_arm.",
    )
    parser.add_argument(
        "--trim-threshold",
        type=float,
        default=0.05,
        help="Start threshold in rad, measured as max abs delta from the episode's first valid qpos.",
    )
    parser.add_argument(
        "--trim-window",
        type=int,
        default=1,
        help="Require this many consecutive frames above threshold before cutting.",
    )
    parser.add_argument(
        "--trim-pre-roll",
        type=int,
        default=0,
        help="Keep this many frames before the detected start frame.",
    )
    parser.add_argument(
        "--min-frames-after-trim",
        type=int,
        default=8,
        help="Skip episodes that would have fewer than this many frames after trimming.",
    )
    parser.add_argument(
        "--push-to-hub",
        action="store_true",
        help="Push the converted LeRobot dataset to Hugging Face Hub.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect source episodes and print inferred features without writing a dataset.",
    )
    return parser.parse_args()


def parse_camera_map(value: str) -> dict[str, str]:
    if value.strip().lower() == "auto":
        return {}
    result: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            source, target = item.split(":", 1)
        except ValueError as exc:
            raise ValueError(f"Bad --camera-map item: {item!r}") from exc
        result[source.strip()] = target.strip()
    return result


def parse_image_size(value: str | None) -> tuple[int, int] | None:
    text = str(value or "original").strip().lower()
    if text in {"", "original", "keep", "source"}:
        return None
    match = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", text)
    if not match:
        raise ValueError("--image-size must be 'original' or HxW, for example 240x320.")
    height, width = int(match.group(1)), int(match.group(2))
    if height <= 0 or width <= 0:
        raise ValueError("--image-size dimensions must be > 0.")
    return height, width


def normalize_image_encoding(value: str) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized == "jpeg":
        return "jpg"
    if normalized not in {"auto", "jpg", "png"}:
        raise ValueError("--image-encoding must be auto, jpg, jpeg, or png.")
    return normalized


def infer_source_image_encoding(
    episodes: list[tuple[Path, dict[str, Any]]],
    camera_plan: dict[str, str | None],
    *,
    limit: int = 200,
) -> tuple[str, dict[str, int]]:
    counts = {"jpg": 0, "png": 0}
    inspected = 0
    source_keys = {source for source in camera_plan.values() if source}
    for _episode_dir, payload in episodes:
        for frame in payload.get("data", []):
            colors = frame.get("colors") or {}
            for source_key in source_keys:
                rel_path = colors.get(source_key)
                suffix = Path(str(rel_path or "")).suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    counts["jpg"] += 1
                elif suffix == ".png":
                    counts["png"] += 1
                inspected += 1
                if inspected >= limit:
                    break
            if inspected >= limit:
                break
        if inspected >= limit:
            break
    if counts["jpg"] == 0 and counts["png"] == 0:
        return "png", counts
    return ("jpg" if counts["jpg"] >= counts["png"] else "png"), counts


def configure_lerobot_image_encoding(extension: str, jpeg_quality: int) -> None:
    extension = normalize_image_encoding(extension)
    if extension == "auto":
        raise ValueError("Resolved image encoding cannot be auto.")
    if not 1 <= int(jpeg_quality) <= 100:
        raise ValueError("--jpeg-quality must be between 1 and 100.")
    image_path = f"images/{{image_key}}/episode_{{episode_index:06d}}/frame_{{frame_index:06d}}.{extension}"
    try:
        import lerobot.datasets.image_writer as image_writer_module
        import lerobot.datasets.lerobot_dataset as lerobot_dataset_module
        import lerobot.datasets.utils as lerobot_utils_module
    except Exception as exc:  # pragma: no cover - depends on LeRobot env.
        raise RuntimeError("Could not patch LeRobot image encoding; run inside the LeRobot environment.") from exc

    lerobot_dataset_module.DEFAULT_IMAGE_PATH = image_path
    lerobot_utils_module.DEFAULT_IMAGE_PATH = image_path
    original_write_image = image_writer_module.write_image

    def write_image_with_encoding(image: np.ndarray | Image.Image, fpath: Path) -> None:
        if extension != "jpg":
            original_write_image(image, fpath)
            return
        try:
            if isinstance(image, np.ndarray):
                img = image_writer_module.image_array_to_pil_image(image)
            elif isinstance(image, Image.Image):
                img = image
            else:
                raise TypeError(f"Unsupported image type: {type(image)}")
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(fpath, format="JPEG", quality=int(jpeg_quality), optimize=True)
        except Exception as exc:
            print(f"Error writing image {fpath}: {exc}", flush=True)

    image_writer_module.write_image = write_image_with_encoding
    lerobot_dataset_module.write_image = write_image_with_encoding


def parse_target_image_keys(value: str) -> list[str]:
    keys = [item.strip() for item in value.split(",") if item.strip()]
    if not keys:
        raise ValueError("--target-image-keys cannot be empty")
    if len(keys) != len(set(keys)):
        raise ValueError("--target-image-keys contains duplicate keys")
    return keys


def import_lerobot_dataset():
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        return LeRobotDataset
    except ImportError:
        from lerobot.common.datasets.lerobot_dataset import LeRobotDataset

        return LeRobotDataset


def lerobot_home() -> Path:
    return Path(os.environ.get("HF_LEROBOT_HOME", DEFAULT_LEROBOT_HOME)).expanduser()


def safe_repo_dir_name(repo_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_id).strip("._") or "dataset"


def default_conversion_cache_dir(repo_id: str) -> Path:
    datasets_root = lerobot_home().parent
    return datasets_root / DEFAULT_CACHE_ROOT_NAME / "convert" / safe_repo_dir_name(repo_id)


def configure_conversion_cache(cache_dir: Path) -> None:
    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    env_defaults = {
        "XDG_CACHE_HOME": cache_dir / "xdg",
        "HF_HOME": cache_dir / "huggingface",
        "HF_DATASETS_CACHE": cache_dir / "huggingface" / "datasets",
        "HUGGINGFACE_HUB_CACHE": cache_dir / "huggingface" / "hub",
        "TORCH_HOME": cache_dir / "torch",
    }
    for key, path in env_defaults.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)


def cleanup_conversion_cache(cache_dir: Path) -> int:
    cache_dir = cache_dir.expanduser()
    if not cache_dir.exists():
        return 0
    removed = 0
    for child in cache_dir.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"Warning: failed to remove cache path {child}: {exc}", file=sys.stderr, flush=True)
    return removed


def lerobot_total_episodes(dataset_dir: Path) -> int:
    info_path = dataset_dir / "meta" / "info.json"
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
        return max(0, int(payload.get("total_episodes") or 0))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def trim_jsonl_by_episode(path: Path, keep_episodes: int) -> None:
    if not path.exists():
        return
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if int(payload.get("episode_index", -1)) < keep_episodes:
            kept.append(json.dumps(payload, ensure_ascii=False))
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")


def rollback_lerobot_episodes(dataset_dir: Path, keep_episodes: int) -> None:
    if keep_episodes < 0:
        keep_episodes = 0
    data_dir = dataset_dir / "data"
    if data_dir.exists():
        for parquet in data_dir.glob("chunk-*/episode_*.parquet"):
            match = re.fullmatch(r"episode_(\d+)\.parquet", parquet.name)
            if match and int(match.group(1)) >= keep_episodes:
                parquet.unlink(missing_ok=True)
    videos_dir = dataset_dir / "videos"
    if videos_dir.exists():
        for video in videos_dir.glob("chunk-*/**/episode_*.*"):
            match = re.fullmatch(r"episode_(\d+)\..+", video.name)
            if match and int(match.group(1)) >= keep_episodes:
                video.unlink(missing_ok=True)
    cleanup_external_images(dataset_dir, min_episode_index=keep_episodes)
    trim_jsonl_by_episode(dataset_dir / "meta" / "episodes.jsonl", keep_episodes)
    trim_jsonl_by_episode(dataset_dir / "meta" / "episodes_stats.jsonl", keep_episodes)
    info_path = dataset_dir / "meta" / "info.json"
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
        payload["total_episodes"] = keep_episodes
        payload["total_frames"] = 0
        episodes_file = dataset_dir / "meta" / "episodes.jsonl"
        if episodes_file.exists():
            total_frames = 0
            for line in episodes_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    total_frames += int(json.loads(line).get("length") or 0)
            payload["total_frames"] = total_frames
        splits = payload.get("splits")
        if isinstance(splits, dict) and "train" in splits:
            splits["train"] = f"0:{payload['total_frames']}"
        info_path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass


def cleanup_external_images(
    dataset_dir: Path,
    *,
    episode_index: int | None = None,
    min_episode_index: int | None = None,
) -> int:
    """Remove LeRobot image-writer PNG cache after parquet image bytes are saved."""
    images_dir = dataset_dir / "images"
    if not images_dir.exists():
        return 0
    removed = 0
    patterns: list[str] = []
    if episode_index is not None:
        patterns.append(f"episode_{episode_index:06d}")
    if min_episode_index is not None:
        for child in images_dir.glob("*/episode_*"):
            match = re.fullmatch(r"episode_(\d+)", child.name)
            if match and int(match.group(1)) >= min_episode_index:
                try:
                    shutil.rmtree(child)
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed
    for pattern in patterns:
        for child in images_dir.glob(f"*/{pattern}"):
            try:
                shutil.rmtree(child)
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def release_lerobot_dataset(dataset: Any) -> None:
    """Best-effort cleanup between batches to keep long conversions flatter."""
    for method_name in ("clear_episode_buffer",):
        method = getattr(dataset, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
    writer = getattr(dataset, "image_writer", None)
    if writer is not None:
        for method_name in ("wait_until_done", "close", "stop", "terminate"):
            method = getattr(writer, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass


def open_lerobot_dataset_for_append(LeRobotDataset: Any, repo_id: str, output_path: Path) -> Any:
    """Open an existing dataset for appending without scanning all historical parquet files."""
    total = lerobot_total_episodes(output_path)
    if total > 0:
        try:
            dataset = LeRobotDataset(repo_id, root=output_path, episodes=[total - 1])
            # `episodes` limits historical loading, but save_episode should validate
            # against the full dataset metadata when appending a new episode.
            dataset.episodes = None
            return dataset
        except Exception as exc:
            print(
                f"Fast resume open failed, falling back to full dataset load: {type(exc).__name__}: {exc}",
                flush=True,
            )
    return LeRobotDataset(repo_id, root=output_path)


def load_h2_joint_limits(urdf: Path) -> dict[str, dict[str, float]]:
    if not urdf.exists():
        return {}
    root = ET.parse(urdf).getroot()
    limits: dict[str, dict[str, float]] = {}
    for joint in root.findall("joint"):
        if joint.get("type") == "fixed":
            continue
        limit = joint.find("limit")
        if limit is None:
            continue
        limits[joint.get("name", "")] = {
            "lower": float(limit.get("lower", "nan")),
            "upper": float(limit.get("upper", "nan")),
            "effort": float(limit.get("effort", "nan")),
            "velocity": float(limit.get("velocity", "nan")),
        }
    return limits


def load_completed_episode(path: Path) -> dict[str, Any] | None:
    data_file = path / "data.json"
    if not data_file.exists():
        return None
    with data_file.open("rb") as handle:
        handle.seek(max(0, data_file.stat().st_size - 16))
        if not handle.read().replace(b"\r\n", b"\n").rstrip().endswith(b"]\n}"):
            return None
    return json.loads(data_file.read_text(encoding="utf-8"))


def iter_episode_dirs(src: Path) -> list[Path]:
    return sorted(path for path in src.glob("episode_*") if path.is_dir())


def _qpos_values(item: Any) -> list[float]:
    if not isinstance(item, dict):
        return []
    qpos = item.get("qpos", [])
    if qpos is None:
        qpos = []
    return [float(v) for v in qpos]


def _end_effector_type(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("type") or "").lower()


def _is_no_control_end_effector(item: Any) -> bool:
    return _end_effector_type(item) in NO_CONTROL_END_EFFECTOR_TYPES


def infer_vector_dims(
    episodes: list[tuple[Path, dict[str, Any]]],
    vector_keys: list[str],
) -> list[tuple[str, int]]:
    dims = {key: 0 for key in vector_keys}
    for _, payload in episodes:
        for frame in payload.get("data", []):
            if not isinstance(frame, dict):
                continue
            for root_key in ("states", "actions"):
                root = frame.get(root_key) or {}
                if not isinstance(root, dict):
                    continue
                for key in vector_keys:
                    item = root.get(key) or {}
                    if key in ("left_ee", "right_ee"):
                        ee_type = _end_effector_type(item)
                        if ee_type in NO_CONTROL_END_EFFECTOR_TYPES:
                            continue
                        fixed_dim = FIXED_END_EFFECTOR_DIMS.get(ee_type)
                        if fixed_dim is not None:
                            dims[key] = max(dims[key], fixed_dim)
                            continue
                    dims[key] = max(dims[key], len(_qpos_values(item)))
    return [(key, dims[key]) for key in vector_keys if dims[key] > 0]


def qpos_vector(frame: dict[str, Any], root_key: str, vector_dims: list[tuple[str, int]]) -> np.ndarray:
    root = frame.get(root_key) or {}
    values: list[float] = []
    for key, dim in vector_dims:
        item = root.get(key) or {}
        qpos = [] if key in ("left_ee", "right_ee") and _is_no_control_end_effector(item) else _qpos_values(item)
        if len(qpos) > dim:
            raise ValueError(f"{root_key}.{key} qpos has {len(qpos)} values, expected at most {dim}")
        values.extend(qpos)
        if len(qpos) < dim:
            values.extend([0.0] * (dim - len(qpos)))
    return np.asarray(values, dtype=np.float32)


def frame_group_qpos(frame: dict[str, Any], root_key: str, vector_key: str) -> np.ndarray | None:
    root = frame.get(root_key) or {}
    if not isinstance(root, dict):
        return None
    item = root.get(vector_key) or {}
    qpos = _qpos_values(item)
    if not qpos:
        return None
    return np.asarray(qpos, dtype=np.float32)


def detect_motion_start(
    frames: list[Any],
    *,
    root_key: str,
    vector_key: str,
    threshold: float,
    window: int,
    pre_roll: int,
) -> tuple[int, dict[str, Any]]:
    valid_frames = [frame for frame in frames if isinstance(frame, dict)]
    if not valid_frames:
        return 0, {"reason": "no_valid_frames", "detected_index": 0, "max_delta": 0.0}

    baseline = None
    baseline_index = 0
    for index, frame in enumerate(valid_frames):
        qpos = frame_group_qpos(frame, root_key, vector_key)
        if qpos is not None:
            baseline = qpos
            baseline_index = index
            break
    if baseline is None:
        return 0, {"reason": "missing_qpos", "detected_index": 0, "max_delta": 0.0}

    deltas: list[float] = []
    for frame in valid_frames:
        qpos = frame_group_qpos(frame, root_key, vector_key)
        if qpos is None or qpos.shape != baseline.shape:
            deltas.append(0.0)
        else:
            deltas.append(float(np.max(np.abs(qpos - baseline))))

    window = max(1, int(window))
    detected_index = None
    for index in range(baseline_index, len(deltas)):
        if all(delta >= threshold for delta in deltas[index : index + window]):
            detected_index = index
            break

    if detected_index is None:
        return 0, {
            "reason": "threshold_not_reached",
            "detected_index": 0,
            "max_delta": max(deltas) if deltas else 0.0,
        }

    start_index = max(0, detected_index - max(0, int(pre_roll)))
    return start_index, {
        "reason": "motion_detected",
        "detected_index": detected_index,
        "max_delta": max(deltas) if deltas else 0.0,
    }


def trim_leading_static_episodes(
    episodes: list[tuple[Path, dict[str, Any]]],
    args: argparse.Namespace,
) -> tuple[list[tuple[Path, dict[str, Any]]], dict[str, Any]]:
    if not args.trim_leading_static:
        return episodes, {"enabled": False}

    trimmed: list[tuple[Path, dict[str, Any]]] = []
    details: list[dict[str, Any]] = []
    total_input_frames = 0
    total_output_frames = 0
    total_trimmed_frames = 0
    skipped_short = 0

    for episode_dir, payload in episodes:
        frames = payload.get("data", [])
        if not isinstance(frames, list):
            frames = []
        total_input_frames += len(frames)
        start_index, info = detect_motion_start(
            frames,
            root_key=args.trim_root_key,
            vector_key=args.trim_vector_key,
            threshold=float(args.trim_threshold),
            window=int(args.trim_window),
            pre_roll=int(args.trim_pre_roll),
        )
        kept_frames = frames[start_index:]
        if len(kept_frames) < args.min_frames_after_trim:
            skipped_short += 1
            details.append(
                {
                    "episode": episode_dir.name,
                    "input_frames": len(frames),
                    "trim_start": start_index,
                    "output_frames": 0,
                    "skipped": True,
                    **info,
                }
            )
            continue

        new_payload = dict(payload)
        new_payload["data"] = kept_frames
        trimmed.append((episode_dir, new_payload))
        total_output_frames += len(kept_frames)
        total_trimmed_frames += start_index
        details.append(
            {
                "episode": episode_dir.name,
                "input_frames": len(frames),
                "trim_start": start_index,
                "output_frames": len(kept_frames),
                "skipped": False,
                **info,
            }
        )

    return trimmed, {
        "enabled": True,
        "root_key": args.trim_root_key,
        "vector_key": args.trim_vector_key,
        "threshold": float(args.trim_threshold),
        "window": int(args.trim_window),
        "pre_roll": int(args.trim_pre_roll),
        "min_frames_after_trim": int(args.min_frames_after_trim),
        "input_episodes": len(episodes),
        "output_episodes": len(trimmed),
        "skipped_short_episodes": skipped_short,
        "input_frames": total_input_frames,
        "output_frames": total_output_frames,
        "trimmed_frames": total_trimmed_frames,
        "details": details,
    }


def read_rgb(path: Path, image_size: tuple[int, int] | None = None) -> np.ndarray:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        if image_size is not None:
            height, width = image_size
            rgb = rgb.resize((width, height), resample=Image.BICUBIC)
        return np.asarray(rgb)


def first_valid_frame(episodes: list[tuple[Path, dict[str, Any]]]) -> tuple[Path, dict[str, Any]]:
    for episode_dir, payload in episodes:
        for frame in payload.get("data", []):
            if isinstance(frame, dict):
                return episode_dir, frame
    raise ValueError("No valid frames found in completed episodes.")


def all_color_keys(episodes: list[tuple[Path, dict[str, Any]]]) -> set[str]:
    keys: set[str] = set()
    for _, payload in episodes:
        for frame in payload.get("data", []):
            colors = frame.get("colors") if isinstance(frame, dict) else None
            if isinstance(colors, dict):
                keys.update(key for key, value in colors.items() if isinstance(value, str))
    return keys


def episode_ee_types(episodes: list[tuple[Path, dict[str, Any]]]) -> tuple[set[str], set[str]]:
    left_types: set[str] = set()
    right_types: set[str] = set()
    for _, payload in episodes:
        for frame in payload.get("data", []):
            if not isinstance(frame, dict):
                continue
            states = frame.get("states") or {}
            for source, target in (("left_ee", left_types), ("right_ee", right_types)):
                ee = states.get(source) if isinstance(states, dict) else None
                if isinstance(ee, dict):
                    ee_type = str(ee.get("type") or "none")
                    if ee_type and ee_type != "none":
                        target.add(ee_type)
    return left_types, right_types


def infer_camera_preset(episodes: list[tuple[Path, dict[str, Any]]], available_keys: set[str]) -> str:
    left_types, right_types = episode_ee_types(episodes)
    if right_types and not left_types:
        return "right_hand" if "color_3" in available_keys else "right_hand_3cam"
    if left_types and not right_types:
        return "left_hand"
    if {"color_0", "color_2", "color_3"}.issubset(available_keys):
        return "dual"
    if {"color_0", "color_1", "color_2"}.issubset(available_keys):
        return "right_hand_3cam"
    if {"color_0", "color_1"}.issubset(available_keys):
        return "head_torso"
    return "head_only"


def build_camera_plan(
    *,
    explicit_map: dict[str, str],
    preset: str,
    target_keys: list[str],
    episodes: list[tuple[Path, dict[str, Any]]],
) -> tuple[dict[str, str | None], str]:
    available_keys = all_color_keys(episodes)
    if explicit_map:
        plan = {target: None for target in target_keys}
        for source, target in explicit_map.items():
            if target not in plan:
                raise ValueError(f"Camera target {target!r} is not in --target-image-keys")
            plan[target] = source if source in available_keys else None
        return plan, "explicit"

    resolved_preset = infer_camera_preset(episodes, available_keys) if preset == "auto" else preset
    preset_sources = CAMERA_PRESET_SOURCES[resolved_preset]
    plan = {
        target: (preset_sources.get(target) if preset_sources.get(target) in available_keys else None)
        for target in target_keys
    }
    return plan, resolved_preset


def infer_image_shape(
    episodes: list[tuple[Path, dict[str, Any]]],
    fallback_frame: dict[str, Any],
    fallback_episode_dir: Path,
) -> tuple[int, int, int]:
    for episode_dir, payload in episodes:
        for frame in payload.get("data", []):
            colors = frame.get("colors") if isinstance(frame, dict) else None
            if not isinstance(colors, dict):
                continue
            for rel_path in colors.values():
                if not isinstance(rel_path, str):
                    continue
                image_path = episode_dir / rel_path
                if image_path.is_file():
                    return tuple(read_rgb(image_path).shape)
    info = fallback_frame.get("info") or {}
    image_info = info.get("image") if isinstance(info, dict) else None
    if not isinstance(image_info, dict):
        # data.json stores info at payload level, not per-frame; try the sample episode payload.
        for _, payload in episodes:
            image_info = ((payload.get("info") or {}).get("image") or {})
            if image_info:
                break
    height = int(float((image_info or {}).get("height", 480)))
    width = int(float((image_info or {}).get("width", 640)))
    return (height, width, 3)


def image_features(
    episode_dir: Path,
    frame: dict[str, Any],
    camera_plan: dict[str, str | None],
    default_shape: tuple[int, int, int],
    image_size: tuple[int, int] | None,
) -> dict[str, dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {}
    colors = frame.get("colors") or {}
    for target_key, source_key in camera_plan.items():
        rel_path = colors.get(source_key) if source_key else None
        image = read_rgb(episode_dir / rel_path, image_size) if rel_path else np.zeros(default_shape, dtype=np.uint8)
        features[target_key] = {
            "dtype": "image",
            "shape": tuple(image.shape),
            "names": ["height", "width", "channel"],
        }
    return features


def infer_fps(payloads: list[dict[str, Any]]) -> int:
    for payload in payloads:
        fps = (((payload.get("info") or {}).get("image") or {}).get("fps"))
        if fps:
            return int(round(float(fps)))
    return 30


def write_metadata(
    dataset_dir: Path,
    *,
    args: argparse.Namespace,
    state_dim: int,
    action_dim: int,
    image_keys: list[str],
    source_image_shape: tuple[int, int, int],
    output_image_shape: tuple[int, int, int],
    image_size: tuple[int, int] | None,
    camera_plan: dict[str, str | None],
    camera_preset: str,
    vector_keys: list[str],
    vector_dims: list[tuple[str, int]],
    joint_limits: dict[str, dict[str, float]],
    image_encoding: str,
    image_encoding_counts: dict[str, int],
    trim_report: dict[str, Any] | None = None,
) -> None:
    joint_names: list[str] = []
    for key, _dim in vector_dims:
        joint_names.extend(H2_ARM_JOINT_NAMES.get(key, []))
    metadata = {
        "source_dataset": str(args.src),
        "robot": "Unitree H2",
        "urdf": str(args.urdf),
        "state_dim": state_dim,
        "action_dim": action_dim,
        "image_keys": image_keys,
        "source_image_shape": list(source_image_shape),
        "output_image_shape": list(output_image_shape),
        "image_size": "original" if image_size is None else f"{image_size[0]}x{image_size[1]}",
        "image_encoding": image_encoding,
        "requested_image_encoding": normalize_image_encoding(args.image_encoding),
        "source_image_encoding_counts": image_encoding_counts,
        "jpeg_quality": int(args.jpeg_quality) if image_encoding == "jpg" else None,
        "external_images": "kept" if args.keep_external_images else "removed_after_parquet_save",
        "camera_plan": camera_plan,
        "camera_preset": camera_preset,
        "resume": bool(args.resume),
        "resume_overlap": int(args.resume_overlap),
        "start_episode": int(args.start_episode),
        "batch_size": args.batch_size,
        "batch_policy": "continue_until_all_selected_episodes_are_written",
        "cache_dir": str((args.cache_dir or default_conversion_cache_dir(args.repo_id)).expanduser()),
        "cache_policy": "kept" if args.keep_cache else "cleared_between_batches",
        "leading_static_trim": trim_report or {"enabled": False},
        "missing_image_policy": "Black image placeholders are written for target image keys whose source camera is absent.",
        "vector_keys": vector_keys,
        "vector_dims": [{"key": key, "dim": dim} for key, dim in vector_dims],
        "joint_names_for_known_dims": joint_names,
        "h2_joint_limits": joint_limits,
        "note": "Raw qpos values are stored. OpenPI normalization stats should be computed after conversion.",
    }
    (dataset_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    if args.resume and args.overwrite:
        print("--resume and --overwrite cannot be used together.", file=sys.stderr)
        return 2
    if args.start_episode < 0:
        print("--start-episode must be >= 0.", file=sys.stderr)
        return 2
    if args.resume_overlap < 0:
        print("--resume-overlap must be >= 0.", file=sys.stderr)
        return 2
    if args.batch_size is not None and args.batch_size <= 0:
        print("--batch-size must be > 0.", file=sys.stderr)
        return 2
    cache_dir = (args.cache_dir or default_conversion_cache_dir(args.repo_id)).expanduser()
    configure_conversion_cache(cache_dir)
    print(f"Conversion cache dir: {cache_dir}", flush=True)
    if not args.keep_cache:
        removed_cache_items = cleanup_conversion_cache(cache_dir)
        if removed_cache_items:
            print(f"Cleared conversion cache before start: {removed_cache_items} paths", flush=True)
        configure_conversion_cache(cache_dir)
    camera_map = parse_camera_map(args.camera_map)
    try:
        image_size = parse_image_size(args.image_size)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    target_image_keys = parse_target_image_keys(args.target_image_keys)
    vector_keys = [key.strip() for key in args.vector_keys.split(",") if key.strip()]
    joint_limits = load_h2_joint_limits(args.urdf)

    if not args.src.exists():
        print(f"Source dataset does not exist: {args.src}", file=sys.stderr)
        print("Put the H2 task directory there or pass --src /path/to/task.", file=sys.stderr)
        return 2

    episodes: list[tuple[Path, dict[str, Any]]] = []
    skipped = 0
    for episode_dir in iter_episode_dirs(args.src):
        payload = load_completed_episode(episode_dir)
        if payload is None:
            skipped += 1
            continue
        if payload.get("data"):
            episodes.append((episode_dir, payload))

    if not episodes:
        print(f"No completed episodes found under {args.src}", file=sys.stderr)
        return 2

    episodes, trim_report = trim_leading_static_episodes(episodes, args)
    if not episodes:
        print(f"No episodes left after leading-static trim under {args.src}", file=sys.stderr)
        return 2

    initial_start_index = min(max(0, args.start_episode), len(episodes))
    final_end_index = len(episodes)
    if initial_start_index >= final_end_index:
        print(
            f"No episodes selected. completed={len(episodes)}, start={initial_start_index}, batch_size={args.batch_size}",
            file=sys.stderr,
        )
        return 2
    schema_episodes = episodes[initial_start_index:final_end_index]

    sample_episode_dir, sample_frame = first_valid_frame(schema_episodes)
    vector_dims = infer_vector_dims(schema_episodes, vector_keys)
    state_dim = sum(dim for _, dim in vector_dims)
    action_dim = state_dim
    camera_plan, camera_preset = build_camera_plan(
        explicit_map=camera_map,
        preset=args.camera_preset,
        target_keys=target_image_keys,
        episodes=schema_episodes,
    )
    requested_image_encoding = normalize_image_encoding(args.image_encoding)
    inferred_image_encoding, image_encoding_counts = infer_source_image_encoding(schema_episodes, camera_plan)
    image_encoding = inferred_image_encoding if requested_image_encoding == "auto" else requested_image_encoding
    try:
        configure_lerobot_image_encoding(image_encoding, args.jpeg_quality)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    source_image_shape = infer_image_shape(schema_episodes, sample_frame, sample_episode_dir)
    if image_size is not None and (image_size[0] > source_image_shape[0] or image_size[1] > source_image_shape[1]):
        print(
            f"--image-size {image_size[0]}x{image_size[1]} is larger than source image "
            f"{source_image_shape[0]}x{source_image_shape[1]}. Upscaling is not allowed.",
            file=sys.stderr,
        )
        return 2
    output_image_shape = (
        (image_size[0], image_size[1], 3)
        if image_size is not None
        else source_image_shape
    )
    img_features = image_features(sample_episode_dir, sample_frame, camera_plan, output_image_shape, image_size)
    image_shapes = {key: tuple(value["shape"]) for key, value in img_features.items()}
    fps = infer_fps([payload for _, payload in schema_episodes])

    print(f"Completed episodes: {len(episodes)} (skipped incomplete: {skipped})")
    print(f"FPS: {fps}")
    print(f"State dim: {state_dim}; action dim: {action_dim}")
    print(f"Vector dims: {', '.join(f'{key}:{dim}' for key, dim in vector_dims) if vector_dims else '(none)'}")
    print(f"Camera preset: {camera_preset}")
    print(f"Camera plan: {camera_plan}")
    print(
        "Image encoding: "
        f"{image_encoding} (requested={requested_image_encoding}, "
        f"source jpg={image_encoding_counts['jpg']}, png={image_encoding_counts['png']}, "
        f"jpeg_quality={args.jpeg_quality if image_encoding == 'jpg' else '-'})"
    )
    print(f"Source image shape: {source_image_shape[0]}x{source_image_shape[1]}")
    print(f"Output image shape: {output_image_shape[0]}x{output_image_shape[1]}")
    print(f"Image keys: {', '.join(img_features) if img_features else '(none)'}")
    print(f"H2 URDF non-fixed joints with limits: {len(joint_limits)}")
    if trim_report.get("enabled"):
        print(
            "Leading static trim: "
            f"{trim_report['trimmed_frames']} frames removed, "
            f"{trim_report['output_frames']}/{trim_report['input_frames']} frames kept, "
            f"{trim_report['output_episodes']}/{trim_report['input_episodes']} episodes kept",
            flush=True,
        )
        top_trimmed = sorted(
            trim_report.get("details", []),
            key=lambda item: int(item.get("trim_start") or 0),
            reverse=True,
        )[:10]
        for item in top_trimmed:
            print(
                f"  {item['episode']}: trim_start={item['trim_start']} "
                f"input={item['input_frames']} output={item['output_frames']} "
                f"reason={item['reason']} max_delta={float(item['max_delta']):.4f}",
                flush=True,
            )

    if args.dry_run:
        return 0

    try:
        LeRobotDataset = import_lerobot_dataset()
    except ImportError as exc:
        print("Could not import LeRobot. Run inside the OpenPI/LeRobot environment.", file=sys.stderr)
        raise exc

    output_root = lerobot_home()
    output_path = output_root / args.repo_id
    existing_episodes = lerobot_total_episodes(output_path)
    resume_start = args.start_episode
    if args.resume:
        if not output_path.exists():
            print(f"--resume requested but output does not exist; creating a new dataset: {output_path}")
        elif existing_episodes <= 0:
            print(f"--resume requested but no valid existing episodes were found; recreating: {output_path}")
            shutil.rmtree(output_path)
        elif existing_episodes > 0:
            keep_episodes = max(0, min(existing_episodes, len(episodes)) - args.resume_overlap)
            print(
                f"Resume requested: existing LeRobot episodes={existing_episodes}, "
                f"rollback={existing_episodes - keep_episodes}, restart raw index={keep_episodes}",
                flush=True,
            )
            rollback_lerobot_episodes(output_path, keep_episodes)
            resume_start = max(resume_start, keep_episodes)
    elif output_path.exists():
        if not args.overwrite:
            print(f"Output already exists: {output_path}", file=sys.stderr)
            print("Re-run with --overwrite to replace it, or --resume to continue it.", file=sys.stderr)
            return 2
        shutil.rmtree(output_path)

    start_index = min(resume_start, len(episodes))
    final_end_index = len(episodes)
    if start_index >= final_end_index:
        print(
            f"No episodes selected. completed={len(episodes)}, start={start_index}, batch_size={args.batch_size}",
            file=sys.stderr,
        )
        return 2
    batch_size = args.batch_size or (final_end_index - start_index)
    print(
        f"Selected raw episode range: {start_index + 1}-{final_end_index} / {len(episodes)} "
        f"(count={final_end_index - start_index}, batch_size={batch_size})",
        flush=True,
    )

    features: dict[str, dict[str, Any]] = {
        **img_features,
        "state": {
            "dtype": "float32",
            "shape": (state_dim,),
            "names": ["state"],
        },
        "actions": {
            "dtype": "float32",
            "shape": (action_dim,),
            "names": ["actions"],
        },
    }

    create_kwargs: dict[str, Any] = {
        "repo_id": args.repo_id,
        "robot_type": args.robot_type,
        "fps": fps,
        "features": features,
        "image_writer_threads": max(1, args.image_writer_threads),
        "image_writer_processes": max(0, args.image_writer_processes),
    }
    if "root" in inspect.signature(LeRobotDataset.create).parameters:
        create_kwargs["root"] = output_path

    total_episodes = len(episodes)
    current_index = start_index
    batch_index = 0
    while current_index < final_end_index:
        batch_index += 1
        batch_end_index = min(final_end_index, current_index + batch_size)
        batch_episodes = episodes[current_index:batch_end_index]
        if output_path.exists() and lerobot_total_episodes(output_path) > 0:
            dataset = open_lerobot_dataset_for_append(LeRobotDataset, args.repo_id, output_path)
        else:
            dataset = LeRobotDataset.create(**create_kwargs)
        add_frame_parameters = inspect.signature(dataset.add_frame).parameters
        add_frame_takes_task = "task" in add_frame_parameters
        print(
            f"Starting batch {batch_index}: raw episode range "
            f"{current_index + 1}-{batch_end_index} / {total_episodes}",
            flush=True,
        )
        try:
            for episode_index, (episode_dir, payload) in enumerate(batch_episodes, start=current_index + 1):
                prompt = args.task or (payload.get("text") or {}).get("goal") or (payload.get("text") or {}).get("desc") or ""
                for frame in payload.get("data", []):
                    state = qpos_vector(frame, "states", vector_dims)
                    action = qpos_vector(frame, "actions", vector_dims)
                    if state.shape[0] != state_dim or action.shape[0] != action_dim:
                        raise ValueError(
                            f"Dimension changed in {episode_dir.name} frame {frame.get('idx')}: "
                            f"state {state.shape[0]} vs {state_dim}, action {action.shape[0]} vs {action_dim}"
                        )

                    item: dict[str, Any] = {
                        "state": state,
                        "actions": action,
                    }
                    colors = frame.get("colors") or {}
                    for target_key, source_key in camera_plan.items():
                        rel_path = colors.get(source_key) if source_key else None
                        item[target_key] = (
                            read_rgb(episode_dir / rel_path, image_size)
                            if rel_path
                            else np.zeros(image_shapes[target_key], dtype=np.uint8)
                        )
                    if add_frame_takes_task:
                        dataset.add_frame(item, task=prompt)
                    else:
                        item["task"] = prompt
                        dataset.add_frame(item)
                    del item
                dataset.save_episode()
                saved_output_index = lerobot_total_episodes(output_path) - 1
                if not args.keep_external_images and saved_output_index >= 0:
                    removed_image_dirs = cleanup_external_images(output_path, episode_index=saved_output_index)
                    if removed_image_dirs:
                        print(
                            f"Removed external images for episode_{saved_output_index:06d}: "
                            f"{removed_image_dirs} image-key dirs",
                            flush=True,
                        )
                progress = {
                    "source_total_episodes": total_episodes,
                    "selected_start_episode": start_index,
                    "selected_end_episode": final_end_index,
                    "batch_size": batch_size,
                    "current_batch": batch_index,
                    "current_batch_start_episode": current_index,
                    "current_batch_end_episode": batch_end_index,
                    "last_saved_source_episode": episode_dir.name,
                    "last_saved_source_index": episode_index - 1,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                (output_path / "conversion_progress.json").write_text(
                    json.dumps(progress, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"Saved episode {episode_index}/{total_episodes}: {episode_dir.name}", flush=True)
                gc.collect()
        finally:
            release_lerobot_dataset(dataset)
            del dataset
        write_metadata(
            output_path,
            args=args,
            state_dim=state_dim,
            action_dim=action_dim,
            image_keys=list(img_features),
            source_image_shape=source_image_shape,
            output_image_shape=output_image_shape,
            image_size=image_size,
            camera_plan=camera_plan,
            camera_preset=camera_preset,
            vector_keys=vector_keys,
            vector_dims=vector_dims,
            joint_limits=joint_limits,
            image_encoding=image_encoding,
            image_encoding_counts=image_encoding_counts,
            trim_report=trim_report,
        )
        if trim_report.get("enabled"):
            (output_path / "trim_report.json").write_text(
                json.dumps(trim_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        current_index = batch_end_index
        print(f"Finished batch {batch_index}: next raw index {current_index}", flush=True)
        if not args.keep_cache:
            removed_cache_items = cleanup_conversion_cache(cache_dir)
            if removed_cache_items:
                print(f"Cleared conversion cache after batch {batch_index}: {removed_cache_items} paths", flush=True)
            configure_conversion_cache(cache_dir)
        gc.collect()

    write_metadata(
        output_path,
        args=args,
        state_dim=state_dim,
        action_dim=action_dim,
        image_keys=list(img_features),
        source_image_shape=source_image_shape,
        output_image_shape=output_image_shape,
        image_size=image_size,
        camera_plan=camera_plan,
        camera_preset=camera_preset,
        vector_keys=vector_keys,
        vector_dims=vector_dims,
        joint_limits=joint_limits,
        image_encoding=image_encoding,
        image_encoding_counts=image_encoding_counts,
        trim_report=trim_report,
    )
    if trim_report.get("enabled"):
        (output_path / "trim_report.json").write_text(
            json.dumps(trim_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.push_to_hub:
        dataset = LeRobotDataset(args.repo_id, root=output_path)
        dataset.push_to_hub(
            tags=["unitree", "h2", "xr_teleoperate", "openpi"],
            private=False,
            push_videos=True,
            license="apache-2.0",
        )
        release_lerobot_dataset(dataset)
        del dataset

    print(f"LeRobot dataset written to: {output_path}")
    if not args.keep_cache:
        removed_cache_items = cleanup_conversion_cache(cache_dir)
        if removed_cache_items:
            print(f"Cleared conversion cache after finish: {removed_cache_items} paths", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
