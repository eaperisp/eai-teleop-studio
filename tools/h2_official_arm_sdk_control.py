#!/usr/bin/env python3
"""Clean Unitree H2 arm control tool based on the official arm_sdk DDS example.

This file intentionally does not use the older teleop control wrappers.  It
follows the official H2 example shape: subscribe rt/lowstate, publish rt/arm_sdk,
write arm joints 15-28 plus the weight field at index 31, and compute CRC.
"""

from __future__ import annotations

import argparse
import math
import pickle
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


ARM_SDK_TOPIC = "rt/arm_sdk"
LOWSTATE_TOPIC = "rt/lowstate"
CONTROL_DT = 0.02
DEFAULT_KP = 80.0
DEFAULT_KD = 1.5


class H2JointIndex:
    LeftShoulderPitch = 15
    LeftShoulderRoll = 16
    LeftShoulderYaw = 17
    LeftElbow = 18
    LeftWristRoll = 19
    LeftWristPitch = 20
    LeftWristYaw = 21

    RightShoulderPitch = 22
    RightShoulderRoll = 23
    RightShoulderYaw = 24
    RightElbow = 25
    RightWristRoll = 26
    RightWristPitch = 27
    RightWristYaw = 28

    Weight = 31


LEFT_ARM_JOINTS = [
    H2JointIndex.LeftShoulderPitch,
    H2JointIndex.LeftShoulderRoll,
    H2JointIndex.LeftShoulderYaw,
    H2JointIndex.LeftElbow,
    H2JointIndex.LeftWristRoll,
    H2JointIndex.LeftWristPitch,
    H2JointIndex.LeftWristYaw,
]
RIGHT_ARM_JOINTS = [
    H2JointIndex.RightShoulderPitch,
    H2JointIndex.RightShoulderRoll,
    H2JointIndex.RightShoulderYaw,
    H2JointIndex.RightElbow,
    H2JointIndex.RightWristRoll,
    H2JointIndex.RightWristPitch,
    H2JointIndex.RightWristYaw,
]
ARM_JOINTS = LEFT_ARM_JOINTS + RIGHT_ARM_JOINTS

ARM_NAMES = [
    "left_shoulder_pitch",
    "left_shoulder_roll",
    "left_shoulder_yaw",
    "left_elbow",
    "left_wrist_roll",
    "left_wrist_pitch",
    "left_wrist_yaw",
    "right_shoulder_pitch",
    "right_shoulder_roll",
    "right_shoulder_yaw",
    "right_elbow",
    "right_wrist_roll",
    "right_wrist_pitch",
    "right_wrist_yaw",
]

# Mechanical limits from the H2 IK implementation, with a 0.01 rad safety
# margin already included.  Order is left 7 joints followed by right 7.
ARM_Q_MIN = np.asarray(
    [
        -2.6079939, -0.507, -2.6079939, -0.976111, -2.6079939, -0.566, -1.2117305,
        -2.6079939, -2.484, -2.6079939, -0.976111, -2.6079939, -0.566, -1.2117305,
    ],
    dtype=np.float32,
)
ARM_Q_MAX = np.asarray(
    [
        1.8225957, 2.484, 2.6079939, 3.0617795, 2.6079939, 0.566, 1.2117305,
        1.8225957, 0.507, 2.6079939, 3.0617795, 2.6079939, 0.566, 1.2117305,
    ],
    dtype=np.float32,
)


def import_unitree_sdk() -> tuple[Any, Any, Any, Any, Any, Any]:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
    from unitree_sdk2py.utils.crc import CRC

    return ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber, unitree_hg_msg_dds__LowCmd_, LowCmd_, LowState_, CRC


@dataclass
class MotionResult:
    before: np.ndarray
    target: np.ndarray
    held: np.ndarray
    released: np.ndarray

    @property
    def moved_max(self) -> float:
        return float(np.max(np.abs(self.held - self.before)))

    @property
    def target_error_max(self) -> float:
        return float(np.max(np.abs(self.held - self.target)))

    @property
    def release_drift_max(self) -> float:
        return float(np.max(np.abs(self.released - self.held)))


@dataclass(frozen=True)
class ArmStateSnapshot:
    q: np.ndarray
    dq: np.ndarray
    tick: int | None
    seq: int | None
    received_at_monotonic: float


class OfficialH2ArmSdk:
    def __init__(self, kp: float = DEFAULT_KP, kd: float = DEFAULT_KD) -> None:
        (
            _channel_factory_initialize,
            channel_publisher,
            channel_subscriber,
            low_cmd_factory,
            low_cmd_type,
            low_state_type,
            crc_type,
        ) = import_unitree_sdk()
        self.kp = kp
        self.kd = kd
        self.low_cmd = low_cmd_factory()
        self.low_state: Any | None = None
        self.first_update_low_state = False
        self._state_lock = threading.Lock()
        self._state_snapshot: ArmStateSnapshot | None = None
        self.crc = crc_type()
        self.publisher = channel_publisher(ARM_SDK_TOPIC, low_cmd_type)
        self.subscriber = channel_subscriber(LOWSTATE_TOPIC, low_state_type)

    def init(self) -> None:
        self.publisher.Init()
        self.subscriber.Init(self._low_state_handler, 10)

    @staticmethod
    def _extract_lowstate_seq(msg: Any) -> int | None:
        sample_info = getattr(msg, "sample_info", None)
        for name in ("sequence_number", "seq", "sample_sequence_number"):
            value = getattr(sample_info, name, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _low_state_handler(self, msg: Any) -> None:
        q = np.asarray([msg.motor_state[j].q for j in ARM_JOINTS], dtype=np.float32)
        dq = np.asarray([msg.motor_state[j].dq for j in ARM_JOINTS], dtype=np.float32)
        tick_value = getattr(msg, "tick", None)
        tick = int(tick_value) if tick_value is not None else None
        snapshot = ArmStateSnapshot(
            q=q,
            dq=dq,
            tick=tick,
            seq=self._extract_lowstate_seq(msg),
            received_at_monotonic=time.monotonic(),
        )
        with self._state_lock:
            self.low_state = msg
            self._state_snapshot = snapshot
            self.first_update_low_state = True

    def wait_low_state(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            with self._state_lock:
                ready = self.first_update_low_state
            if ready:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no {LOWSTATE_TOPIC} message received within {timeout_s:.1f}s")
            time.sleep(0.05)

    def read_arm_state_snapshot(self) -> ArmStateSnapshot:
        with self._state_lock:
            snapshot = self._state_snapshot
        if snapshot is None:
            raise RuntimeError("low_state is not ready")
        return ArmStateSnapshot(
            q=snapshot.q.copy(),
            dq=snapshot.dq.copy(),
            tick=snapshot.tick,
            seq=snapshot.seq,
            received_at_monotonic=snapshot.received_at_monotonic,
        )

    def read_arm_q(self) -> np.ndarray:
        return self.read_arm_state_snapshot().q

    def read_arm_dq(self) -> np.ndarray:
        return self.read_arm_state_snapshot().dq

    def _write_arm_command(
        self,
        q14: np.ndarray,
        weight: float,
        tau14: np.ndarray | None = None,
    ) -> None:
        q14 = np.asarray(q14, dtype=np.float32)
        if q14.shape != (14,) or not np.all(np.isfinite(q14)):
            raise ValueError("q14 must be a finite array with shape (14,)")
        if tau14 is None:
            tau14 = np.zeros(14, dtype=np.float32)
        else:
            tau14 = np.asarray(tau14, dtype=np.float32)
            if tau14.shape != (14,) or not np.all(np.isfinite(tau14)):
                raise ValueError("tau14 must be a finite array with shape (14,)")
            if float(np.max(np.abs(tau14))) > 20.0:
                raise ValueError("refusing arm torque feedforward above 20 Nm")
        self.low_cmd.motor_cmd[H2JointIndex.Weight].q = float(weight)
        for value, tau, joint in zip(q14, tau14, ARM_JOINTS):
            cmd = self.low_cmd.motor_cmd[joint]
            cmd.tau = float(tau)
            cmd.q = float(value)
            cmd.dq = 0.0
            cmd.kp = float(self.kp)
            cmd.kd = float(self.kd)
        self.low_cmd.crc = self.crc.Crc(self.low_cmd)
        self.publisher.Write(self.low_cmd)


    def move_absolute(
        self,
        target_q14: np.ndarray,
        *,
        duration_s: float,
        hold_s: float,
        release_s: float,
    ) -> MotionResult:
        from unitree_sdk2py.utils.thread import RecurrentThread

        before = self.read_arm_q()
        target_q14 = np.asarray(target_q14, dtype=np.float32)
        if target_q14.shape != (14,):
            raise ValueError(f"target_q14 must have shape (14,), got {target_q14.shape}")

        start_time = time.monotonic()
        move_end = start_time + max(duration_s, 0.0)
        hold_end = move_end + max(hold_s, 0.0)
        release_end = hold_end + max(release_s, 0.0)
        done = False

        def write_step() -> None:
            nonlocal done
            if done:
                return
            now = time.monotonic()
            current = self.read_arm_q()
            if now < move_end:
                ratio = min(max((now - start_time) / max(duration_s, CONTROL_DT), 0.0), 1.0)
                q = ratio * target_q14 + (1.0 - ratio) * current
                self._write_arm_command(q, weight=1.0)
                return
            if now < hold_end:
                self._write_arm_command(target_q14, weight=1.0)
                return
            if now < release_end:
                ratio = min(max((now - hold_end) / max(release_s, CONTROL_DT), 0.0), 1.0)
                self._write_arm_command(target_q14, weight=1.0 - ratio)
                return
            if release_s > 0.0:
                self._write_arm_command(target_q14, weight=0.0)
            done = True

        thread = RecurrentThread(interval=CONTROL_DT, target=write_step, name="h2_arm_sdk_control")
        thread.Start()
        try:
            while not done:
                time.sleep(0.05)
        finally:
            thread.Wait(1.0)
        held = self.read_arm_q()

        return MotionResult(before=before, target=target_q14.copy(), held=held, released=self.read_arm_q())

    def move_absolute_sleep_loop(
        self,
        target_q14: np.ndarray,
        *,
        duration_s: float,
        hold_s: float,
        release_s: float,
    ) -> MotionResult:
        before = self.read_arm_q()
        target_q14 = np.asarray(target_q14, dtype=np.float32)
        if target_q14.shape != (14,):
            raise ValueError(f"target_q14 must have shape (14,), got {target_q14.shape}")

        start = before.copy()
        start_time = time.monotonic()
        next_tick = start_time
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration_s:
                break
            ratio = min(max(elapsed / max(duration_s, CONTROL_DT), 0.0), 1.0)
            q = (1.0 - ratio) * start + ratio * target_q14
            self._write_arm_command(q, weight=1.0)
            next_tick += CONTROL_DT
            time.sleep(max(0.0, next_tick - time.monotonic()))

        hold_end = time.monotonic() + max(0.0, hold_s)
        while time.monotonic() < hold_end:
            self._write_arm_command(target_q14, weight=1.0)
            time.sleep(CONTROL_DT)
        held = self.read_arm_q()

        if release_s > 0.0:
            release_start = time.monotonic()
            while True:
                elapsed = time.monotonic() - release_start
                if elapsed >= release_s:
                    break
                ratio = min(max(elapsed / max(release_s, CONTROL_DT), 0.0), 1.0)
                self._write_arm_command(target_q14, weight=1.0 - ratio)
                time.sleep(CONTROL_DT)
            self._write_arm_command(target_q14, weight=0.0)
            time.sleep(0.2)

        return MotionResult(before=before, target=target_q14.copy(), held=held, released=self.read_arm_q())

    def official_demo(self, *, duration_s: float, release_s: float) -> MotionResult:
        before = self.read_arm_q()
        zero = np.zeros(14, dtype=np.float32)
        target = np.asarray(
            [
                0.0,
                math.radians(20.0),
                0.0,
                math.radians(30.0),
                0.0,
                0.0,
                0.0,
                0.0,
                -math.radians(20.0),
                0.0,
                math.radians(30.0),
                0.0,
                0.0,
                0.0,
            ],
            dtype=np.float32,
        )
        self.move_absolute(zero, duration_s=duration_s, hold_s=0.0, release_s=0.0)
        self.move_absolute(target, duration_s=duration_s * 2.0, hold_s=0.5, release_s=0.0)
        self.move_absolute(zero, duration_s=duration_s * 3.0, hold_s=0.0, release_s=release_s)
        after = self.read_arm_q()
        return MotionResult(before=before, target=zero, held=after, released=after)


class H2GravityCompensator:
    """Pinocchio RNEA gravity feedforward using XR's trusted reduced model."""

    def __init__(self, model_cache: str | Path) -> None:
        import pinocchio as pin

        self.pin = pin
        self.model_cache = Path(model_cache).expanduser().resolve()
        with self.model_cache.open("rb") as cache_file:
            cache = pickle.load(cache_file)
        self.model = cache["reduced_model"]
        if self.model.nq != 14 or self.model.nv != 14:
            raise ValueError(
                f"unexpected XR reduced model dimensions: "
                f"nq={self.model.nq}, nv={self.model.nv}"
            )
        self.data = self.model.createData()
        self.zero = np.zeros(14, dtype=np.float64)

    def compute(self, q14: np.ndarray) -> np.ndarray:
        q = np.asarray(q14, dtype=np.float64)
        if q.shape != (14,) or not np.all(np.isfinite(q)):
            raise ValueError("gravity q14 must be a finite array with shape (14,)")
        tau = np.asarray(
            self.pin.rnea(self.model, self.data, q, self.zero, self.zero),
            dtype=np.float32,
        )
        if tau.shape != (14,) or not np.all(np.isfinite(tau)):
            raise RuntimeError("Pinocchio returned invalid gravity torque")
        if float(np.max(np.abs(tau))) > 20.0:
            raise RuntimeError("refusing gravity torque above 20 Nm")
        return tau


def format_q(title: str, q14: np.ndarray) -> None:
    print(f"{title}:")
    for idx, (name, value) in enumerate(zip(ARM_NAMES, q14)):
        print(f"  {idx:2d}  {name:<22s} {float(value): .6f}")
    print("  q14:", [round(float(v), 6) for v in q14])
    print("  left_q7:", [round(float(v), 6) for v in q14[:7]])
    print("  right_q7:", [round(float(v), 6) for v in q14[7:]])


def parse_float_list(values: Iterable[str], *, name: str) -> np.ndarray:
    try:
        out = np.asarray([float(v) for v in values], dtype=np.float32)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must contain only numbers") from exc
    return out


def build_target(current: np.ndarray, arm: str, joints: list[str]) -> np.ndarray:
    q = current.copy()
    values = parse_float_list(joints, name="--joints")
    if arm == "both":
        if values.shape != (14,):
            raise ValueError("--arm both requires exactly 14 joint values")
        q[:] = values
    elif arm == "left":
        if values.shape != (7,):
            raise ValueError("--arm left requires exactly 7 joint values")
        q[:7] = values
    elif arm == "right":
        if values.shape != (7,):
            raise ValueError("--arm right requires exactly 7 joint values")
        q[7:] = values
    else:
        raise ValueError(f"unsupported arm: {arm}")
    return q


def require_execute(args: argparse.Namespace) -> None:
    if not args.execute or not args.confirm_execute:
        raise SystemExit("refusing to move: add both --execute and --confirm-execute")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--network-interface", default="enp86s0")
    parser.add_argument("--lowstate-timeout", type=float, default=5.0)
    parser.add_argument("--kp", type=float, default=DEFAULT_KP)
    parser.add_argument("--kd", type=float, default=DEFAULT_KD)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("read", help="print current H2 14 arm joint positions")

    move_parser = subparsers.add_parser("move", help="move left, right, or both arms to absolute joint targets")
    move_parser.add_argument("--arm", choices=["left", "right", "both"], default="right")
    move_parser.add_argument("--joints", nargs="+", required=True)
    move_parser.add_argument("--duration", type=float, default=3.0)
    move_parser.add_argument("--hold-seconds", type=float, default=0.5)
    move_parser.add_argument("--release-seconds", type=float, default=1.0)
    move_parser.add_argument("--max-target-delta", type=float, default=0.05)
    move_parser.add_argument("--tolerance", type=float, default=0.08)
    move_parser.add_argument("--execute", action="store_true")
    move_parser.add_argument("--confirm-execute", action="store_true")

    demo_parser = subparsers.add_parser("demo", help="run the official small both-arm demo posture")
    demo_parser.add_argument("--duration", type=float, default=3.0)
    demo_parser.add_argument("--release-seconds", type=float, default=1.0)
    demo_parser.add_argument("--execute", action="store_true")
    demo_parser.add_argument("--confirm-execute", action="store_true")

    args = parser.parse_args()
    print(f"[INFO] ChannelFactoryInitialize interface={args.network_interface}")
    ChannelFactoryInitialize = import_unitree_sdk()[0]
    ChannelFactoryInitialize(0, args.network_interface)

    client = OfficialH2ArmSdk(kp=args.kp, kd=args.kd)
    client.init()
    client.wait_low_state(args.lowstate_timeout)

    current = client.read_arm_q()
    format_q("current", current)
    if args.command == "read":
        return 0

    require_execute(args)
    if args.command == "move":
        target = build_target(current, args.arm, args.joints)
        format_q("target", target)
        delta = float(np.max(np.abs(target - current)))
        print(f"[CHECK] max_target_delta={delta:.6f} rad")
        if delta > args.max_target_delta:
            raise SystemExit(
                f"refusing target: delta {delta:.6f} > --max-target-delta {args.max_target_delta:.6f}"
            )
        result = client.move_absolute(
            target,
            duration_s=args.duration,
            hold_s=args.hold_seconds,
            release_s=args.release_seconds,
        )
    elif args.command == "demo":
        result = client.official_demo(duration_s=args.duration, release_s=args.release_seconds)
    else:
        raise SystemExit(f"unknown command: {args.command}")

    format_q("held", result.held)
    if np.max(np.abs(result.released - result.held)) > 1e-6:
        format_q("released", result.released)
    print(
        "[VERIFY] "
        f"moved_max={result.moved_max:.6f} "
        f"target_error_max={result.target_error_max:.6f} "
        f"release_drift_max={result.release_drift_max:.6f}"
    )
    if args.command == "move" and result.target_error_max > args.tolerance:
        raise RuntimeError(
            f"H2 arm_sdk target was not tracked: error {result.target_error_max:.6f} > tolerance {args.tolerance:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
