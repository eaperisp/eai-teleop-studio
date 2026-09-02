#!/usr/bin/env python3
"""Run H2 OpenPI VLA with images loaded from a recorded xr_teleoperate episode."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.h2_action_calibration import add_deployment_args  # noqa: E402
from tools.h2_openpi_official_vla import (  # noqa: E402
    ArmSdkTargetHold,
    DEFAULT_TAU_RATE_LIMIT_CSV,
    MotorPolicyController,
    Observation,
    OfficialH2ArmSdk,
    attach_policy_state,
    configure_policy_layout,
    execute_chunk,
    execute_pre_vla_trajectory,
    finalize_arm_safety_args,
    format_q,
    image_stats,
    import_image_client,
    init_dds,
    log_event,
    prepare_action_calibration,
    read_observation,
    release_arm_sdk,
    release_kwargs_from_holder,
    request_actions,
    restore_start_pose,
    save_debug_images,
    validate_actions,
)


ARM_VECTOR_KEYS = ("left_arm", "right_arm")


def _request_shutdown(signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def resolve_path(path: str | Path, base: Path = PROJECT_ROOT) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = base / resolved
    return resolved


def read_rgb(path: Path) -> np.ndarray:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError(f"failed to read image: {path}")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def q14_from_frame(frame: dict[str, Any], root_key: str) -> np.ndarray:
    root = frame.get(root_key) or {}
    values: list[float] = []
    for key in ARM_VECTOR_KEYS:
        item = root.get(key) or {}
        qpos = item.get("qpos") or []
        if len(qpos) != 7:
            raise ValueError(f"{root_key}.{key}.qpos must be 7D, got {len(qpos)}")
        values.extend(float(v) for v in qpos)
    q14 = np.asarray(values, dtype=np.float32)
    if q14.shape != (14,) or not np.all(np.isfinite(q14)):
        raise ValueError(f"{root_key} q14 must be finite 14D, got {q14.shape}")
    return q14


class EpisodeImageSource:
    def __init__(self, episode_dir: Path, camera_map: dict[str, str]) -> None:
        self.episode_dir = episode_dir
        self.camera_map = camera_map
        payload = json.loads((episode_dir / "data.json").read_text(encoding="utf-8"))
        self.frames = payload.get("data") or []
        if not self.frames:
            raise ValueError(f"episode has no frames: {episode_dir}")
        self.instruction = ((payload.get("text") or {}).get("goal") or "").strip()

    def frame_at(self, frame_index: int) -> dict[str, Any]:
        idx = min(max(int(frame_index), 0), len(self.frames) - 1)
        return self.frames[idx]

    def observation(self, frame: dict[str, Any], arm_client: OfficialH2ArmSdk, state_source: str) -> Observation:
        colors = frame.get("colors") or {}

        def load_slot(slot: str) -> np.ndarray:
            color_key = self.camera_map[slot]
            rel = colors.get(color_key)
            if not rel:
                raise KeyError(f"frame {frame.get('idx')} missing colors.{color_key} for {slot}")
            return read_rgb(self.episode_dir / rel)

        if state_source == "dataset":
            state = q14_from_frame(frame, "states")
        else:
            state = arm_client.read_arm_q()

        return Observation(
            state=state,
            image=load_slot("image"),
            left_wrist_image=load_slot("left_wrist_image"),
            right_wrist_image=load_slot("right_wrist_image"),
        )


def parse_camera_map(value: str) -> dict[str, str]:
    presets = {
        "right_hand_3cam": {
            "image": "color_0",
            "left_wrist_image": "color_1",
            "right_wrist_image": "color_2",
        },
        "dual_4cam": {
            "image": "color_0",
            "left_wrist_image": "color_2",
            "right_wrist_image": "color_3",
        },
    }
    text = value.strip()
    if text in presets:
        return presets[text]
    result: dict[str, str] = {}
    for item in text.split(","):
        if not item.strip():
            continue
        source, target = item.split(":", 1)
        result[target.strip()] = source.strip()
    required = {"image", "left_wrist_image", "right_wrist_image"}
    missing = sorted(required - set(result))
    if missing:
        raise ValueError(f"--camera-map missing target slots: {missing}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://192.168.61.228:8080")
    parser.add_argument("--image-source", choices=["dataset", "camera"], default="dataset")
    parser.add_argument("--task-dir", default="/home/robot/data/datasets/robot/h2_switch_close_to_remote")
    parser.add_argument("--episode", default="episode_0001")
    parser.add_argument("--camera-map", default="right_hand_3cam")
    parser.add_argument("--offline-frame-start", type=int, default=220)
    parser.add_argument("--offline-frame-stride", type=int, default=16)
    parser.add_argument("--offline-state-source", choices=["current", "dataset"], default="dataset")
    parser.add_argument("--instruction", default="")
    parser.add_argument("--network-interface", default="enp86s0")
    parser.add_argument("--dds-domain", type=int, default=0)
    parser.add_argument("--img-server-ip", default="127.0.0.1")
    parser.add_argument("--img-request-port", type=int, default=60000)
    parser.add_argument("--image-camera", default="head_camera")
    parser.add_argument("--left-wrist-camera", default="torso_camera")
    parser.add_argument("--right-wrist-camera", default="right_wrist_camera")
    parser.add_argument("--image-timeout", type=float, default=5.0)
    parser.add_argument("--missing-camera-policy", choices=["duplicate-main", "black", "error"], default="error")
    parser.add_argument("--left-wrist-fallback-image", default="")
    parser.add_argument("--left-wrist-static-image", default="")
    parser.add_argument("--fallback-image-height", type=int, default=480)
    parser.add_argument("--fallback-image-width", type=int, default=640)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument("--state-tail-zeros", type=int, default=2)
    parser.add_argument("--state-tail-values", default="")
    parser.add_argument("--motor-action-indices", default="")
    parser.add_argument("--motor-control-url", default="http://127.0.0.1:18099/api/motor/control")
    parser.add_argument("--motor-left-max", type=float, default=0.25)
    parser.add_argument("--motor-right-min", type=float, default=0.75)
    parser.add_argument("--extra-action-dims-policy", choices=["reject", "crop"], default="crop")
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--observation-horizon", type=int, default=1)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--action-horizon", type=int, default=8)
    parser.add_argument("--exe-steps", type=int, default=16)
    parser.add_argument("--action-start-index", type=int, default=0)
    parser.add_argument("--auto-action-start", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--auto-action-min-delta", type=float, default=0.04)
    parser.add_argument("--auto-action-max-start", type=int, default=12)
    parser.add_argument("--control-freq", type=float, default=15.0)
    parser.add_argument("--action-arrival-tolerance", type=float, default=0.0)
    parser.add_argument("--action-arrival-timeout", type=float, default=0.8)
    parser.add_argument("--action-arrival-poll-hz", type=float, default=100.0)
    parser.add_argument("--action-arrival-settle-samples", type=int, default=3)
    parser.add_argument(
        "--action-arrival-timeout-policy",
        choices=["abort", "continue"],
        default="abort",
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
    parser.add_argument("--arm-feedback-gain", type=float, default=0.6)
    parser.add_argument("--arm-feedback-max-offset", type=float, default=0.12)
    parser.add_argument("--arm-feedback-ki", type=float, default=0.8)
    parser.add_argument("--arm-feedback-integral-zone", type=float, default=0.15)
    parser.add_argument("--arm-feedback-max-integral", type=float, default=0.08)
    parser.add_argument("--gravity-model-cache", default=str(PROJECT_ROOT / "h2_model_cache.pkl"))
    parser.add_argument("--gravity-ramp-seconds", type=float, default=None, help="Deprecated alias for --tau-activation-blend-seconds.")
    parser.add_argument("--tau-activation-blend-seconds", type=float, default=None)
    parser.add_argument("--tau-rate-limit", default=DEFAULT_TAU_RATE_LIMIT_CSV)
    parser.add_argument("--control-arm", choices=["right", "left", "both"], default="right")
    add_deployment_args(parser)
    parser.add_argument("--max-command-delta", type=float, default=0.18)
    parser.add_argument("--max-command-velocity", type=float, default=0.3)
    parser.add_argument("--unsafe-disable-command-limits", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--unsafe-disable-tau-rate-limit", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--reject-action-delta", type=float, default=0.0)
    parser.add_argument("--max-abs-q", type=float, default=4.5)
    parser.add_argument("--pre-vla-trajectory-csv", default="/home/robot/eai_teleoperate_studio/data/action/h2_traces/episode_0001.csv")
    parser.add_argument("--pre-vla-trajectory-start", type=int, default=40)
    parser.add_argument("--pre-vla-trajectory-end", type=int, default=220)
    parser.add_argument("--pre-vla-trajectory-stride", type=int, default=1)
    parser.add_argument("--pre-vla-trajectory-max-frames", type=int, default=0)
    parser.add_argument("--pre-vla-trajectory-freq", type=float, default=50.0)
    parser.add_argument("--pre-vla-trajectory-publish-mode", choices=["hold", "direct"], default="hold")
    parser.add_argument("--pre-vla-trajectory-publish-hz", type=float, default=50.0)
    parser.add_argument("--pre-vla-trajectory-hold", type=float, default=0.5)
    parser.add_argument("--pre-vla-trajectory-max-start-delta", type=float, default=1.5)
    parser.add_argument("--pre-vla-trajectory-max-step-delta", type=float, default=0.0)
    parser.add_argument("--pre-vla-trajectory-max-command-delta", type=float, default=0.0)
    parser.add_argument("--pre-vla-trajectory-control-arm", choices=["", "right", "left", "both"], default="")
    parser.add_argument("--restore-on-exit", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--restore-pose-file", default="config/h2_pose_init.json")
    parser.add_argument("--restore-control-arm", choices=["", "right", "left", "both"], default="")
    parser.add_argument("--restore-duration", type=float, default=4.0)
    parser.add_argument("--restore-hold", type=float, default=0.2)
    parser.add_argument("--release-seconds", type=float, default=0.5)
    parser.add_argument("--final-hold-seconds", type=float, default=0.5)
    parser.add_argument("--debug-image-dir", default="logs/vla_debug_images_dataset_image_exec")
    parser.add_argument("--log-jsonl", default="logs/h2_openpi_dataset_image_vla.jsonl")
    parser.add_argument("--append-log", action="store_true")
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
    if args.steps <= 0 or args.offline_frame_stride < 0:
        raise ValueError("--steps must be positive and --offline-frame-stride must be >= 0")
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

    calibrator = prepare_action_calibration(args)
    task_dir = resolve_path(args.task_dir)
    source: EpisodeImageSource | None = None
    if args.image_source == "dataset":
        source = EpisodeImageSource(task_dir / args.episode, parse_camera_map(args.camera_map))
    if not args.instruction and source is not None:
        args.instruction = source.instruction or "Change the switch from close to remote"
    elif not args.instruction:
        args.instruction = "Change the switch from close to remote"

    print(
        f"[INFO] mode={'EXECUTE' if args.execute else 'DRY-RUN'} image_source={args.image_source} "
        f"task={task_dir} episode={args.episode} "
        f"frames={len(source.frames) if source is not None else 'live'} "
        f"start={args.offline_frame_start} stride={args.offline_frame_stride} "
        f"state_source={args.offline_state_source}",
        flush=True,
    )
    if args.log_jsonl and not args.append_log:
        log_path = resolve_path(args.log_jsonl)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
    log_event(
        args,
        "start_dataset_image_vla",
        argv=sys.argv,
        instruction=args.instruction,
        control_arm=args.control_arm,
        max_command_delta=args.max_command_delta,
        max_command_velocity=args.max_command_velocity,
        unsafe_disable_command_limits=args.unsafe_disable_command_limits,
        tau_activation_blend_seconds=args.tau_activation_blend_seconds,
        tau_rate_limit=args.tau_rate_limit_array,
        unsafe_disable_tau_rate_limit=args.unsafe_disable_tau_rate_limit,
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
    holder: ArmSdkTargetHold | None = None
    first_state: np.ndarray | None = None
    last_target: np.ndarray | None = None
    obs_history: list[Observation] = []
    image_client: Any | None = None
    try:
        if args.execute:
            execute_pre_vla_trajectory(arm_client, args)
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
        if args.image_source == "camera":
            image_client_cls = import_image_client()
            image_client = image_client_cls(host=args.img_server_ip, request_port=args.img_request_port, request_bgr=True)

        for step in range(args.steps):
            frame_index = args.offline_frame_start + step * args.offline_frame_stride
            frame_idx: Any = "live"
            if args.image_source == "dataset":
                if source is None:
                    raise RuntimeError("internal error: dataset source not initialized")
                frame = source.frame_at(frame_index)
                frame_idx = frame.get("idx")
                obs = source.observation(frame, arm_client, args.offline_state_source)
            else:
                if image_client is None:
                    raise RuntimeError("internal error: image client not initialized")
                obs = read_observation(image_client, arm_client, args)
            attach_policy_state(obs, args, motor_controller)
            obs_history.append(obs)
            if len(obs_history) > args.observation_horizon:
                obs_history = obs_history[-args.observation_horizon :]
            save_debug_images(args, obs, step)
            if first_state is None:
                first_state = arm_client.read_arm_q().copy()
                format_q("initial_robot", first_state)
            print(f"[OBS] step={step} source={args.image_source} requested_frame={frame_index} frame_idx={frame_idx}", flush=True)
            actions = validate_actions(request_actions(args, obs_history), args)
            log_event(
                args,
                "policy_actions",
                step=step,
                requested_frame=frame_index,
                frame_idx=frame_idx,
                state=obs.state,
                actions=actions,
                image=image_stats(obs.image),
                left_wrist_image=image_stats(obs.left_wrist_image),
                right_wrist_image=image_stats(obs.right_wrist_image),
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
                    motor_controller=motor_controller,
                )

        if args.execute and first_state is not None and last_target is not None:
            if holder is not None:
                holder.set_target(last_target, weight=1.0)
                time.sleep(args.final_hold_seconds)
            held = arm_client.read_arm_q()
            print(
                f"[VERIFY] moved_max={float(np.max(np.abs(held - first_state))):.4f} "
                f"target_error_max={float(np.max(np.abs(held - last_target))):.4f}",
                flush=True,
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
        if image_client is not None:
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
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1)
