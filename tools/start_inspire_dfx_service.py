"""Start the Inspire DFX serial-to-DDS service with stable serial discovery."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


DEFAULT_SERVICE = Path("/home/robot/DFX_inspire_service/build/inspire_h1")
SERIAL_DIRS = (Path("/dev/serial/by-id"), Path("/dev/serial/by-path"))
SERIAL_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")
DEFAULT_DFX_SERIAL_HINTS = (
    "USB_Serial",
    "1a86",
    "CH340",
    "CH341",
)


def existing_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if path.exists():
        return path
    raise FileNotFoundError(f"serial path does not exist: {path}")


def list_serial_candidates(pattern: str | None) -> list[Path]:
    candidates: list[Path] = []

    for directory in SERIAL_DIRS:
        if not directory.exists():
            continue
        for item in sorted(directory.iterdir()):
            text = str(item)
            if pattern and pattern not in text:
                continue
            candidates.append(item)

    if candidates:
        return candidates

    for serial_glob in SERIAL_GLOBS:
        for item in sorted(Path("/").glob(serial_glob.removeprefix("/"))):
            text = str(item)
            if pattern and pattern not in text:
                continue
            candidates.append(item)

    return candidates


def resolve_serial(args: argparse.Namespace) -> Path:
    explicit = existing_path(args.serial) or existing_path(os.getenv("HAND_SERIAL"))
    if explicit is not None:
        return explicit

    candidates = list_serial_candidates(args.serial_match)
    if len(candidates) == 1:
        return candidates[0]
    if not args.serial_match:
        hinted = [
            candidate
            for candidate in candidates
            if any(hint.lower() in str(candidate).lower() for hint in DEFAULT_DFX_SERIAL_HINTS)
        ]
        if len(hinted) == 1:
            return hinted[0]
    if not candidates:
        raise RuntimeError(
            "No serial device found. Check /dev/serial/by-id, /dev/serial/by-path, "
            "/dev/ttyUSB*, and /dev/ttyACM*."
        )

    lines = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise RuntimeError(
        "Multiple serial devices found; refusing to guess.\n"
        f"{lines}\n"
        "Pass --serial /dev/serial/by-id/<device> or --serial-match <substring>."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start DFX Inspire hand serial-to-DDS service.")
    parser.add_argument(
        "--service",
        default=os.getenv("INSPIRE_DFX_SERVICE", str(DEFAULT_SERVICE)),
        help="Path to inspire_h1 service binary.",
    )
    parser.add_argument("--serial", default=None, help="Stable serial path, preferably /dev/serial/by-id/...")
    parser.add_argument("--serial-match", default=None, help="Substring used to filter serial candidates.")
    parser.add_argument("--network", default=os.getenv("DDS_IFACE"), required=os.getenv("DDS_IFACE") is None)
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved command without starting it.")
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="Run service directly instead of prepending sudo.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = Path(args.service).expanduser()
    if not service.exists():
        raise FileNotFoundError(f"service binary does not exist: {service}")

    serial = resolve_serial(args)
    command = [str(service), "-s", str(serial), "--network", args.network]
    if not args.no_sudo and os.geteuid() != 0:
        command = ["sudo", *command]

    print(f"Resolved serial: {serial}")
    print(f"DDS network: {args.network}")
    print("Command:", " ".join(command))

    if args.dry_run:
        return 0

    return subprocess.call(command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI should print concise operator errors.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
