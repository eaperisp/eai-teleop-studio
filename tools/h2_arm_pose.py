#!/usr/bin/env python3
"""Capture and apply Unitree H2 dual-arm joint poses.

Joint order:
  left_shoulder_pitch, left_shoulder_roll, left_shoulder_yaw, left_elbow,
  left_wrist_roll, left_wrist_pitch, left_wrist_yaw,
  right_shoulder_pitch, right_shoulder_roll, right_shoulder_yaw, right_elbow,
  right_wrist_roll, right_wrist_pitch, right_wrist_yaw
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


JOINT_NAMES = [
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
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "apply", "print"])
    parser.add_argument("--pose-file", type=Path, default=PROJECT_ROOT / "config" / "h2_arm_pose.json")
    parser.add_argument("--network-interface", default="enp86s0")
    parser.add_argument("--motion-mode", action="store_true")
    parser.add_argument("--duration", type=float, default=5.0, help="Seconds used to move to the target pose.")
    parser.add_argument("--hold-seconds", type=float, default=2.0, help="Seconds to keep publishing after reaching target.")
    parser.add_argument("--tolerance", type=float, default=0.03)
    return parser.parse_args()


def save_pose(path: Path, q: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "robot": "unitree_h2",
        "type": "dual_arm_qpos",
        "joint_names": JOINT_NAMES,
        "qpos": [float(value) for value in q],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pose(path: Path) -> np.ndarray:
    payload = json.loads(path.read_text(encoding="utf-8"))
    q = np.asarray(payload.get("qpos"), dtype=float)
    if q.shape != (14,):
        raise ValueError(f"Pose file must contain 14 qpos values, got shape {q.shape}")
    return q


def print_pose(q: np.ndarray) -> None:
    for name, value in zip(JOINT_NAMES, q):
        print(f"{name:32s} {value: .6f}")
    print("qpos:", json.dumps([round(float(v), 6) for v in q]))


def apply_pose(arm: H2_ArmController, target: np.ndarray, duration: float, hold_seconds: float, tolerance: float) -> None:
    arm.speed_gradual_max(duration)
    tau = np.zeros(14)
    deadline = time.time() + max(duration, 0.1)
    while time.time() < deadline:
        arm.ctrl_dual_arm(target, tau)
        time.sleep(0.004)
    hold_deadline = time.time() + max(hold_seconds, 0.0)
    while time.time() < hold_deadline:
        arm.ctrl_dual_arm(target, tau)
        time.sleep(0.004)
    current = arm.get_current_dual_arm_q()
    error = np.max(np.abs(current - target))
    print("target:")
    print_pose(target)
    print("current:")
    print_pose(current)
    print(f"max_abs_error: {error:.6f}")
    if error > tolerance:
        print(f"WARNING: pose error {error:.6f} is above tolerance {tolerance:.6f}")


def main() -> int:
    args = parse_args()
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from teleop.robot_control.robot_arm import H2_ArmController

    ChannelFactoryInitialize(0, networkInterface=args.network_interface)
    arm = H2_ArmController(motion_mode=args.motion_mode, simulation_mode=False)

    if args.command == "capture":
        q = arm.get_current_dual_arm_q()
        save_pose(args.pose_file, q)
        print(f"Saved H2 dual-arm pose to: {args.pose_file}")
        print_pose(q)
        return 0

    if args.command == "print":
        q = arm.get_current_dual_arm_q()
        print_pose(q)
        return 0

    target = load_pose(args.pose_file)
    apply_pose(arm, target, args.duration, args.hold_seconds, args.tolerance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
