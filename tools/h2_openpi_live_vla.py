#!/usr/bin/env python3
"""Minimal live-camera launcher for the H2 OpenPI VLA client."""

from __future__ import annotations

import sys

from tools.h2_openpi_official_vla import DEFAULT_TAU_RATE_LIMIT_CSV
from tools.h2_openpi_dataset_image_vla import main


DEFAULT_ARGS = [
    "--image-source",
    "camera",
    "--server",
    "http://192.168.61.228:8080",
    "--instruction",
    "Change the switch from close to remote",
    "--network-interface",
    "enp86s0",
    "--img-server-ip",
    "127.0.0.1",
    "--image-camera",
    "head_camera",
    "--left-wrist-camera",
    "torso_camera",
    "--right-wrist-camera",
    "right_wrist_camera",
    "--steps",
    "40",
    "--action-horizon",
    "8",
    "--exe-steps",
    "1",
    "--control-freq",
    "15",
    "--action-arrival-tolerance",
    "0.015",
    "--action-arrival-timeout",
    "3.0",
    "--action-arrival-poll-hz",
    "100",
    "--action-arrival-settle-samples",
    "3",
    "--action-arrival-timeout-policy",
    "abort",
    "--arm-sdk-publish-hz",
    "250",
    "--arm-sdk-telemetry-period",
    "1",
    "--arm-feedback-gain",
    "0.6",
    "--arm-feedback-max-offset",
    "0.12",
    "--arm-feedback-ki",
    "0.8",
    "--arm-feedback-integral-zone",
    "0.15",
    "--arm-feedback-max-integral",
    "0.08",
    "--gravity-model-cache",
    "/home/robot/eai_teleoperate_studio/h2_model_cache.pkl",
    "--tau-activation-blend-seconds",
    "0.5",
    "--tau-rate-limit",
    DEFAULT_TAU_RATE_LIMIT_CSV,
    "--control-arm",
    "right",
    "--state-tail-zeros",
    "2",
    "--extra-action-dims-policy",
    "crop",
    "--max-command-delta",
    "0.18",
    "--max-command-velocity",
    "0.3",
    "--reject-action-delta",
    "0",
    "--pre-vla-trajectory-csv",
    "",
    "--restore-pose-file",
    "config/h2_pose_init.json",
    "--restore-duration",
    "4",
    "--debug-image-dir",
    "logs/vla_debug_images_live_vla",
    "--log-jsonl",
    "logs/h2_openpi_live_vla.jsonl",
]


if __name__ == "__main__":
    sys.argv = [sys.argv[0], *DEFAULT_ARGS, *sys.argv[1:]]
    raise SystemExit(main())
