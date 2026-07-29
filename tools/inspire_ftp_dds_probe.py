"""Standalone Inspire FTP dexterous hand test tool."""

import argparse
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from teleop.robot_control.devices.inspire_ftp.sdk import (
    CLOSE_ANGLES,
    HAND_SIDES,
    OPEN_ANGLES,
    PINCH_ANGLES,
    InspireHandSDK,
)


COMMANDS = ("state", "angle", "open", "close", "pinch", "sweep")


def parse_angles(raw_values):
    if raw_values is None:
        raise argparse.ArgumentTypeError("--values is required for command=angle")
    if len(raw_values) == 1 and "," in raw_values[0]:
        raw_values = raw_values[0].split(",")
    if len(raw_values) == 1:
        return [int(raw_values[0])] * 6
    if len(raw_values) != 6:
        raise argparse.ArgumentTypeError("expected one angle or six angles")
    return [int(value) for value in raw_values]


def print_state(sdk, sides, timeout):
    for side in sides:
        state = sdk.read_state(side, timeout=timeout)
        if state is None:
            print(f"{side}: state timeout")
        else:
            print(f"{side}: angle_act={list(state.angles)} normalized={list(state.normalized)}")


def run_command(sdk, side, command, angles, duration, rate):
    if command == "state":
        print_state(sdk, [side], timeout=0.05)
        return None
    if command == "angle":
        return sdk.move_to(side, angles, duration=duration, rate=rate)
    if command == "open":
        return sdk.move_to(side, OPEN_ANGLES, duration=duration, rate=rate)
    if command == "close":
        return sdk.move_to(side, CLOSE_ANGLES, duration=duration, rate=rate)
    if command == "pinch":
        return sdk.move_to(side, PINCH_ANGLES, duration=duration, rate=rate)
    if command == "sweep":
        sdk.move_to(side, OPEN_ANGLES, duration=duration, rate=rate)
        time.sleep(0.3)
        sdk.move_to(side, CLOSE_ANGLES, duration=duration, rate=rate)
        time.sleep(0.3)
        return sdk.move_to(side, OPEN_ANGLES, duration=duration, rate=rate)
    raise ValueError(f"unsupported command: {command}")


def main():
    parser = argparse.ArgumentParser(description="Test Inspire FTP hand DDS topics.")
    parser.add_argument("--network-interface", default=None)
    parser.add_argument("--domain", type=int, default=0)
    parser.add_argument("--side", choices=HAND_SIDES + ("both",), default="right")
    parser.add_argument("--command", choices=COMMANDS, default="state")
    parser.add_argument("--values", nargs="*", help="One angle or six angles for command=angle, range 0-1000.")
    parser.add_argument("--duration", type=float, default=1.0, help="Ramp duration in seconds.")
    parser.add_argument("--rate", type=float, default=50.0, help="Command publish rate in Hz during ramps.")
    parser.add_argument("--loop", action="store_true", help="Repeat the command until Ctrl+C.")
    parser.add_argument("--interval", type=float, default=1.0, help="Delay between loop iterations.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned command without DDS writes.")
    args = parser.parse_args()

    sides = list(HAND_SIDES) if args.side == "both" else [args.side]
    try:
        angles = parse_angles(args.values) if args.command == "angle" else None
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    if args.dry_run:
        print(
            f"dry-run: side={args.side} command={args.command} "
            f"angles={angles} duration={args.duration} rate={args.rate}"
        )
        return

    sdk = InspireHandSDK(
        network_interface=args.network_interface,
        domain=args.domain,
        enable_left="left" in sides,
        enable_right="right" in sides,
    )
    sdk.initialize()
    print(
        f"Inspire FTP hand test started: side={args.side}, command={args.command}, "
        f"network_interface={args.network_interface}, domain={args.domain}"
    )

    try:
        while True:
            for side in sides:
                sent = run_command(sdk, side, args.command, angles, args.duration, args.rate)
                if sent is not None:
                    print(f"{side}: angle_set={sent}")
            if not args.loop:
                break
            time.sleep(max(args.interval, 0.0))
    except KeyboardInterrupt:
        print("Interrupted.")


if __name__ == "__main__":
    main()
