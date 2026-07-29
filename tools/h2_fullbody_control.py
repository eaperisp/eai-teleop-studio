#!/usr/bin/env python3
"""
Unitree H2 全身状态与控制工具

功能：
  status          读取 31 个本体关节状态
  save            保存当前关节位姿为 JSON
  compare         比较当前位姿与目标 JSON
  pose            释放常规运控并以 rt/lowcmd 平滑设置全身位姿
  damping         释放常规运控并进入低层阻尼模式
  zero-torque     释放常规运控并进入零力矩模式
  motion-status   查询当前 MotionSwitcher 模式
  motion          停止本程序的低层发送后，选择常规运控模式

安全说明：
  1. pose、damping、zero-torque 都会释放 H2 原生运动控制。
  2. damping 和 zero-torque 不具备站立/平衡能力，机器人必须可靠悬吊。
  3. 全身 pose 首次测试也必须可靠悬吊。
  4. 不要同时运行多个 rt/lowcmd 发布程序。
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
    MotionSwitcherClient,
)
from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelPublisher,
    ChannelSubscriber,
)
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC


NUM_MOTORS = 31
LOW_STATE_TOPIC = "rt/lowstate"
LOW_CMD_TOPIC = "rt/lowcmd"

# 与官方 H2 low_level/h2_ankle_swing_example.py 的低层索引一致。
JOINT_NAMES = [
    "left_hip_pitch",        # 0
    "left_hip_roll",         # 1
    "left_hip_yaw",          # 2
    "left_knee",             # 3
    "left_ankle_roll",       # 4
    "left_ankle_pitch",      # 5
    "right_hip_pitch",       # 6
    "right_hip_roll",        # 7
    "right_hip_yaw",         # 8
    "right_knee",            # 9
    "right_ankle_roll",      # 10
    "right_ankle_pitch",     # 11
    "waist_yaw",             # 12
    "waist_roll",            # 13
    "waist_pitch",           # 14
    "left_shoulder_pitch",   # 15
    "left_shoulder_roll",    # 16
    "left_shoulder_yaw",     # 17
    "left_elbow",            # 18
    "left_wrist_roll",       # 19
    "left_wrist_pitch",      # 20
    "left_wrist_yaw",        # 21
    "right_shoulder_pitch",  # 22
    "right_shoulder_roll",   # 23
    "right_shoulder_yaw",    # 24
    "right_elbow",           # 25
    "right_wrist_roll",      # 26
    "right_wrist_pitch",     # 27
    "right_wrist_yaw",       # 28
    "head_pitch",            # 29
    "head_yaw",              # 30
]

GROUPS = {
    "left_leg": range(0, 6),
    "right_leg": range(6, 12),
    "waist": range(12, 15),
    "left_arm": range(15, 22),
    "right_arm": range(22, 29),
    "head": range(29, 31),
}

# 官方 H2 低层示例中的位置控制增益。
POSITION_KP = np.array(
    [
        150, 150, 150, 250, 60, 90,
        150, 150, 150, 250, 60, 90,
        200, 200, 200,
        90, 60, 20, 60, 4, 4, 4,
        90, 60, 20, 60, 4, 4, 4,
        30, 30,
    ],
    dtype=np.float64,
)

POSITION_KD = np.array(
    [
        2.0, 2.0, 2.0, 2.0, 0.3, 0.1,
        2.0, 2.0, 2.0, 2.0, 0.3, 0.1,
        2.5, 5.0, 5.0,
        2.0, 1.0, 0.4, 1.0, 0.2, 0.2, 0.2,
        2.0, 1.0, 0.4, 1.0, 0.2, 0.2, 0.2,
        1.0, 1.0,
    ],
    dtype=np.float64,
)

# 较温和的低层阻尼值；可通过 --damping-scale 整体缩放。
DAMPING_KD = np.array(
    [
        3.0, 3.0, 3.0, 4.0, 1.0, 1.0,
        3.0, 3.0, 3.0, 4.0, 1.0, 1.0,
        3.0, 3.0, 3.0,
        1.5, 1.5, 1.0, 1.5, 0.4, 0.4, 0.4,
        1.5, 1.5, 1.0, 1.5, 0.4, 0.4, 0.4,
        0.5, 0.5,
    ],
    dtype=np.float64,
)


def smoothstep5(alpha: float) -> float:
    """五次平滑插值，端点速度和加速度均为 0。"""
    alpha = float(np.clip(alpha, 0.0, 1.0))
    return 10.0 * alpha**3 - 15.0 * alpha**4 + 6.0 * alpha**5


class H2Controller:
    def __init__(self, interface: str, control_dt: float) -> None:
        ChannelFactoryInitialize(0, interface)

        self.control_dt = control_dt
        self.crc = CRC()
        self.stop_event = threading.Event()
        self.state_event = threading.Event()
        self.state_lock = threading.Lock()

        self.low_state: Optional[LowState_] = None
        self.mode_machine = 0
        self.mode_pr = 0  # 官方低层示例：PR=0，AB=1。

        self.publisher: Optional[ChannelPublisher] = None

        self.subscriber = ChannelSubscriber(LOW_STATE_TOPIC, LowState_)
        self.subscriber.Init(self._state_callback, 10)

        self.motion_switcher = MotionSwitcherClient()
        self.motion_switcher.SetTimeout(5.0)
        self.motion_switcher.Init()

    def _state_callback(self, msg: LowState_) -> None:
        with self.state_lock:
            self.low_state = msg
            self.mode_machine = int(msg.mode_machine)
        self.state_event.set()

    def wait_state(self, timeout: float) -> None:
        if not self.state_event.wait(timeout):
            raise TimeoutError(
                f"{timeout:.1f} 秒内没有收到 {LOW_STATE_TOPIC}；"
                "请检查网卡、DDS 域和机器人连接。"
            )

    def positions(self) -> np.ndarray:
        with self.state_lock:
            if self.low_state is None:
                raise RuntimeError("尚未收到 LowState")
            return np.array(
                [float(self.low_state.motor_state[i].q) for i in range(NUM_MOTORS)],
                dtype=np.float64,
            )

    def velocities(self) -> np.ndarray:
        with self.state_lock:
            if self.low_state is None:
                raise RuntimeError("尚未收到 LowState")
            return np.array(
                [float(self.low_state.motor_state[i].dq) for i in range(NUM_MOTORS)],
                dtype=np.float64,
            )

    def print_status(self) -> None:
        q = self.positions()
        dq = self.velocities()
        for group, indices in GROUPS.items():
            print(f"\n[{group}]")
            for i in indices:
                print(
                    f"{i:02d} {JOINT_NAMES[i]:24s} "
                    f"q={q[i]: .6f} rad ({math.degrees(q[i]): .3f} deg)  "
                    f"dq={dq[i]: .6f} rad/s"
                )

    def save_pose(self, path: Path) -> None:
        q = self.positions()
        payload = {
            "robot": "Unitree H2",
            "unit": "rad",
            "joint_index_source": (
                "unitree_sdk2_python/example/h2/low_level/"
                "h2_ankle_swing_example.py"
            ),
            "positions": {
                name: float(q[i]) for i, name in enumerate(JOINT_NAMES)
            },
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def load_pose(path: Path) -> np.ndarray:
        payload = json.loads(path.read_text(encoding="utf-8"))
        positions = payload.get("positions")
        if not isinstance(positions, dict):
            raise ValueError("目标 JSON 缺少 positions 对象")

        missing = [name for name in JOINT_NAMES if name not in positions]
        if missing:
            raise ValueError("目标 JSON 缺少关节：" + ", ".join(missing))

        q = np.array([float(positions[name]) for name in JOINT_NAMES])
        unit = str(payload.get("unit", "rad")).lower()

        if unit in {"deg", "degree", "degrees"}:
            q = np.deg2rad(q)
        elif unit not in {"rad", "radian", "radians"}:
            raise ValueError(f"不支持的位姿单位：{unit}")

        if not np.all(np.isfinite(q)):
            raise ValueError("目标位姿包含 NaN 或无穷值")
        return q.astype(np.float64)

    def compare_pose(
        self,
        target: np.ndarray,
        tolerance: float,
        print_result: bool = True,
    ) -> bool:
        current = self.positions()
        errors = target - current
        mask = np.abs(errors) > tolerance

        if print_result:
            print(
                f"比较阈值：{tolerance:.6f} rad "
                f"({math.degrees(tolerance):.3f} deg)"
            )
            if not np.any(mask):
                print("当前全身位姿已在目标阈值内。")
            else:
                print("以下关节超出阈值：")
                for i in np.flatnonzero(mask):
                    print(
                        f"{i:02d} {JOINT_NAMES[i]:24s} "
                        f"current={current[i]: .6f}  "
                        f"target={target[i]: .6f}  "
                        f"error={errors[i]: .6f} rad "
                        f"({math.degrees(errors[i]): .3f} deg)"
                    )
        return bool(np.any(mask))

    def motion_status(self) -> tuple[int, Optional[dict]]:
        return self.motion_switcher.CheckMode()

    def release_motion(self) -> None:
        status, result = self.motion_status()
        if status != 0:
            raise RuntimeError(f"CheckMode 失败，状态码={status}")

        while result and result.get("name"):
            print(f"[INFO] 正在释放运动服务：{result}")
            code, _ = self.motion_switcher.ReleaseMode()
            if code != 0:
                raise RuntimeError(f"ReleaseMode 失败，状态码={code}")
            time.sleep(1.0)
            status, result = self.motion_status()
            if status != 0:
                raise RuntimeError(f"CheckMode 失败，状态码={status}")

        print("[INFO] 原生运动服务已释放")

    def select_motion(self, name: str) -> None:
        print(f"[INFO] 请求选择常规运动模式：{name}")
        code, _ = self.motion_switcher.SelectMode(name)
        if code != 0:
            raise RuntimeError(
                f"SelectMode({name!r}) 失败，状态码={code}；"
                "请先执行 motion-status 并确认本机固件支持的名称/别名。"
            )
        time.sleep(1.0)
        status, result = self.motion_status()
        print(f"[INFO] SelectMode 返回成功；当前模式查询：status={status}, result={result}")

    def init_publisher(self) -> None:
        if self.publisher is None:
            self.publisher = ChannelPublisher(LOW_CMD_TOPIC, LowCmd_)
            self.publisher.Init()

    def build_command(
        self,
        q: np.ndarray,
        dq: np.ndarray,
        kp: np.ndarray,
        kd: np.ndarray,
        tau: np.ndarray,
    ):
        arrays = (q, dq, kp, kd, tau)
        if any(len(x) != NUM_MOTORS for x in arrays):
            raise ValueError("所有低层命令数组长度必须为 31")

        cmd = unitree_hg_msg_dds__LowCmd_()
        cmd.mode_pr = self.mode_pr
        cmd.mode_machine = self.mode_machine

        for i in range(NUM_MOTORS):
            motor = cmd.motor_cmd[i]
            motor.mode = 1
            motor.q = float(q[i])
            motor.dq = float(dq[i])
            motor.kp = float(kp[i])
            motor.kd = float(kd[i])
            motor.tau = float(tau[i])

        cmd.crc = self.crc.Crc(cmd)
        return cmd

    def publish_command(
        self,
        q: np.ndarray,
        dq: np.ndarray,
        kp: np.ndarray,
        kd: np.ndarray,
        tau: np.ndarray,
    ) -> None:
        if self.publisher is None:
            raise RuntimeError("LowCmd publisher 尚未初始化")
        self.publisher.Write(self.build_command(q, dq, kp, kd, tau))

    def run_damping(self, damping_scale: float) -> None:
        zeros = np.zeros(NUM_MOTORS)
        kd = DAMPING_KD * damping_scale
        print("[WARN] 已进入低层阻尼模式；该模式不能保持站立和平衡。")
        print("[INFO] 按 Ctrl+C 后仍会短暂发送阻尼，再退出。")

        while not self.stop_event.is_set():
            self.publish_command(zeros, zeros, zeros, kd, zeros)
            time.sleep(self.control_dt)

    def run_zero_torque(self) -> None:
        zeros = np.zeros(NUM_MOTORS)
        print("[WARN] 已进入零力矩模式：Kp=0、Kd=0、tau=0。")
        print("[INFO] 按 Ctrl+C 后会短暂切换为阻尼，再退出。")

        while not self.stop_event.is_set():
            self.publish_command(zeros, zeros, zeros, zeros, zeros)
            time.sleep(self.control_dt)

    def move_to_pose(
        self,
        target: np.ndarray,
        duration: float,
        kp_scale: float,
        max_delta: float,
        tolerance: float,
    ) -> None:
        start = self.positions()
        delta = target - start
        max_error = float(np.max(np.abs(delta)))
        max_index = int(np.argmax(np.abs(delta)))

        if max_error > max_delta:
            raise RuntimeError(
                f"最大关节变化 {max_error:.6f} rad "
                f"({math.degrees(max_error):.3f} deg)，位于 "
                f"{max_index}:{JOINT_NAMES[max_index]}，超过 --max-delta。"
            )

        zeros = np.zeros(NUM_MOTORS)
        kp = POSITION_KP * kp_scale
        kd = POSITION_KD
        steps = max(1, int(duration / self.control_dt))
        start_time = time.monotonic()

        print(f"[INFO] 用 {duration:.2f} 秒平滑移动到目标位姿。")
        for step in range(steps + 1):
            if self.stop_event.is_set():
                return
            blend = smoothstep5(step / steps)
            q_cmd = start + blend * delta
            self.publish_command(q_cmd, zeros, kp, kd, zeros)

            deadline = start_time + (step + 1) * self.control_dt
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)

        print("[INFO] 已到达目标，持续保持。按 Ctrl+C 进入阻尼并退出。")
        while not self.stop_event.is_set():
            self.publish_command(target, zeros, kp, kd, zeros)
            time.sleep(self.control_dt)

        # 这里只是最终检查；中断退出时仍会执行 finally 中的阻尼。
        self.compare_pose(target, tolerance, print_result=True)

    def publish_damping_for(self, seconds: float, damping_scale: float) -> None:
        if self.publisher is None or seconds <= 0:
            return

        zeros = np.zeros(NUM_MOTORS)
        kd = DAMPING_KD * damping_scale
        deadline = time.monotonic() + seconds
        print(f"[INFO] 退出前发送 {seconds:.1f} 秒阻尼命令。")

        while time.monotonic() < deadline:
            self.publisher.Write(self.build_command(zeros, zeros, zeros, kd, zeros))
            time.sleep(self.control_dt)


def require_confirmation(expected: str, message: str, assume_yes: bool) -> None:
    print(message)
    if assume_yes:
        return
    actual = input(f'请输入大写 "{expected}" 继续：').strip()
    if actual != expected:
        raise RuntimeError("用户取消操作")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unitree H2 全身位姿、低层模式与常规运控切换工具"
    )
    parser.add_argument(
        "interface",
        help="连接 H2 DDS 网络的网卡名，例如 enp3s0",
    )
    parser.add_argument(
        "command",
        choices=[
            "status",
            "save",
            "compare",
            "pose",
            "damping",
            "zero-torque",
            "motion-status",
            "motion",
        ],
    )
    parser.add_argument("--file", type=Path, help="save/compare/pose 使用的 JSON 文件")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="等待 LowState 的秒数"
    )
    parser.add_argument(
        "--control-dt", type=float, default=0.002, help="低层控制周期，默认 2 ms"
    )
    parser.add_argument(
        "--tolerance-deg", type=float, default=1.0, help="比较容差，单位度"
    )
    parser.add_argument(
        "--duration", type=float, default=8.0, help="目标位姿插值时间，单位秒"
    )
    parser.add_argument(
        "--kp-scale",
        type=float,
        default=0.5,
        help="位置控制 Kp 缩放，范围 (0,1]，默认 0.5",
    )
    parser.add_argument(
        "--damping-scale",
        type=float,
        default=1.0,
        help="阻尼增益缩放，范围 (0,2]，默认 1.0",
    )
    parser.add_argument(
        "--max-delta-deg",
        type=float,
        default=15.0,
        help="pose 单关节最大允许变化，单位度",
    )
    parser.add_argument(
        "--exit-damping-seconds",
        type=float,
        default=1.0,
        help="退出低层控制前继续发送阻尼的时间",
    )
    parser.add_argument(
        "--motion-name",
        default="normal",
        help="MotionSwitcher 常规模式名称/别名，默认 normal",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅建议自动化且已有外部安全联锁时使用",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.control_dt <= 0:
        raise ValueError("--control-dt 必须大于 0")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0")
    if args.duration <= 0:
        raise ValueError("--duration 必须大于 0")
    if not 0.0 < args.kp_scale <= 1.0:
        raise ValueError("--kp-scale 必须位于 (0,1]")
    if not 0.0 < args.damping_scale <= 2.0:
        raise ValueError("--damping-scale 必须位于 (0,2]")
    if args.max_delta_deg <= 0:
        raise ValueError("--max-delta-deg 必须大于 0")
    if args.tolerance_deg < 0:
        raise ValueError("--tolerance-deg 不能小于 0")

    if args.command in {"save", "compare", "pose"} and args.file is None:
        raise ValueError(f"{args.command} 命令必须指定 --file")


def main() -> int:
    args = parse_args()
    validate_args(args)

    controller = H2Controller(args.interface, args.control_dt)

    def handle_signal(_signum, _frame) -> None:
        controller.stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # motion-status 和 motion 不强制依赖 LowState。
    if args.command == "motion-status":
        status, result = controller.motion_status()
        print(f"status={status}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "motion":
        require_confirmation(
            "MOTION",
            (
                "[WARN] 请确认当前没有其他程序持续发布 rt/lowcmd。\n"
                "[WARN] 机器人应处于官方允许恢复常规运控的安全状态。\n"
                f"[INFO] 即将 SelectMode({args.motion_name!r})。"
            ),
            args.yes,
        )
        controller.select_motion(args.motion_name)
        return 0

    print(f"[INFO] 等待 {LOW_STATE_TOPIC}……")
    controller.wait_state(args.timeout)
    print("[INFO] 已收到 H2 低层状态。")

    if args.command == "status":
        controller.print_status()
        return 0

    if args.command == "save":
        controller.save_pose(args.file)
        print(f"[INFO] 当前位姿已保存到：{args.file}")
        return 0

    tolerance = math.radians(args.tolerance_deg)

    # 只有 compare 和 pose 命令需要读取目标位姿文件。
    target: Optional[np.ndarray] = None
    if args.command in {"compare", "pose"}:
        if args.file is None:
            raise ValueError(f"{args.command} 命令必须指定 --file")
        target = controller.load_pose(args.file)

    if args.command == "compare":
        assert target is not None
        changed = controller.compare_pose(target, tolerance)
        return 2 if changed else 0

    if args.command == "pose":
        changed = controller.compare_pose(target, tolerance)
        if not changed:
            return 0
        require_confirmation(
            "H2-POSE",
            (
                "[DANGER] 将释放 H2 原生运动控制并接管全部 31 个电机。\n"
                "[DANGER] 首次测试必须可靠悬吊，周围无人，急停可用。"
            ),
            args.yes,
        )
    elif args.command == "damping":
        require_confirmation(
            "DAMPING",
            (
                "[DANGER] 阻尼模式没有站立和平衡能力。\n"
                "[DANGER] 机器人必须可靠悬吊。"
            ),
            args.yes,
        )
    elif args.command == "zero-torque":
        require_confirmation(
            "ZERO-TORQUE",
            (
                "[DANGER] 所有关节将失去主动支撑和软件阻尼。\n"
                "[DANGER] 机器人必须可靠悬吊。"
            ),
            args.yes,
        )

    # 接管低层控制之前，先检查机器人是否仍明显运动。
    max_velocity = float(np.max(np.abs(controller.velocities())))
    if max_velocity > 0.2:
        raise RuntimeError(
            f"机器人仍在运动，最大关节速度={max_velocity:.3f} rad/s；拒绝接管。"
        )

    controller.release_motion()
    controller.init_publisher()

    try:
        if args.command == "pose":
            assert target is not None
            controller.move_to_pose(
                target=target,
                duration=args.duration,
                kp_scale=args.kp_scale,
                max_delta=math.radians(args.max_delta_deg),
                tolerance=tolerance,
            )
        elif args.command == "damping":
            controller.run_damping(args.damping_scale)
        elif args.command == "zero-torque":
            controller.run_zero_torque()
    finally:
        # 注意：程序退出后阻尼也会消失。机器人仍需保持悬吊，
        # 随后单独执行 motion 命令或按官方遥控器流程恢复常规运控。
        controller.publish_damping_for(
            args.exit_damping_seconds,
            args.damping_scale,
        )

    print(
        "[WARN] 本程序已停止发布 rt/lowcmd。\n"
        "[WARN] 机器人必须继续保持悬吊，直到常规运动服务确认恢复。"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)

