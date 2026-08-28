"""Probe Inspire DFX hand IDs using read-only serial position requests."""

from __future__ import annotations

import argparse
import os
import select
import time


JOINT_NAMES = ("pinky", "ring", "middle", "index", "thumb_bend", "thumb_rotation")
STATUS_NAMES = {
    0: "opening",
    1: "closing",
    2: "target_reached",
    3: "force_limit",
    5: "current_protection",
    6: "locked_rotor",
    7: "actuator_fault",
}
ERROR_BITS = {
    0: "locked_rotor",
    1: "over_temperature",
    2: "over_current",
    3: "motor_abnormal",
    4: "communication",
}
RESPONSE_HEADERS = (b"\x90\xEB", b"\xEB\x90")


def checksum(payload: bytes) -> int:
    return sum(payload[2:-1]) & 0xFF


def position_request(device_id: int) -> bytes:
    return register_request(device_id, 1546, 12)


def register_request(device_id: int, address: int, count: int) -> bytes:
    payload = bytearray((
        0xEB,
        0x90,
        device_id,
        0x04,
        0x11,
        address & 0xFF,
        (address >> 8) & 0xFF,
        count,
        0x00,
    ))
    payload[-1] = checksum(payload)
    return bytes(payload)


def read_response(fd: int, expected_length: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    received = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(fd, 256)
        if chunk:
            received.extend(chunk)
        starts = [received.find(header) for header in RESPONSE_HEADERS]
        valid_starts = [start for start in starts if start >= 0]
        frame_start = min(valid_starts) if valid_starts else -1
        if frame_start >= 0 and len(received) - frame_start >= expected_length:
            return bytes(received[frame_start:frame_start + expected_length])
    return bytes(received)


def decode_payload(frame: bytes, expected_id: int, count: int) -> bytes | None:
    if len(frame) != count + 8 or frame[:2] not in RESPONSE_HEADERS:
        return None
    if frame[2] != expected_id or frame[-1] != checksum(frame):
        return None
    return frame[7:7 + count]


def decode_positions(frame: bytes, expected_id: int) -> list[float] | None:
    payload = decode_payload(frame, expected_id, 12)
    if payload is None:
        return None
    return [
        (payload[offset] | (payload[offset + 1] << 8)) / 1000.0
        for offset in range(0, 12, 2)
    ]


def read_register(fd: int, device_id: int, address: int, count: int, timeout: float) -> bytes | None:
    import termios

    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, register_request(device_id, address, count))
    frame = read_response(fd, count + 8, timeout)
    return decode_payload(frame, device_id, count)


def decode_words(payload: bytes | None) -> list[int] | None:
    if payload is None or len(payload) % 2:
        return None
    return [payload[index] | (payload[index + 1] << 8) for index in range(0, len(payload), 2)]


def decode_errors(values: bytes | None) -> list[list[str]] | None:
    if values is None:
        return None
    return [
        [name for bit, name in ERROR_BITS.items() if value & (1 << bit)]
        for value in values
    ]


def print_diagnostics(fd: int, device_id: int, timeout: float) -> None:
    angle_targets = decode_words(read_register(fd, device_id, 1486, 12, timeout))
    time.sleep(0.01)
    angle_actual = decode_words(read_register(fd, device_id, 1546, 12, timeout))
    time.sleep(0.01)
    currents = decode_words(read_register(fd, device_id, 1594, 12, timeout))
    time.sleep(0.01)
    errors_raw = read_register(fd, device_id, 1606, 6, timeout)
    time.sleep(0.01)
    statuses_raw = read_register(fd, device_id, 1612, 6, timeout)
    time.sleep(0.01)
    temperatures_raw = read_register(fd, device_id, 1618, 6, timeout)

    print(f"id={device_id} diagnostics:")
    for index, name in enumerate(JOINT_NAMES):
        target = angle_targets[index] if angle_targets else None
        actual = angle_actual[index] if angle_actual else None
        current = currents[index] if currents else None
        errors = decode_errors(errors_raw)
        error = errors[index] if errors else None
        status_value = statuses_raw[index] if statuses_raw else None
        status = STATUS_NAMES.get(status_value, f"unknown({status_value})") if status_value is not None else None
        temperature = temperatures_raw[index] if temperatures_raw else None
        print(
            f"  {name}: target={target} actual={actual} current_mA={current} "
            f"status={status} errors={error} temperature_C={temperature}"
        )


def configure_serial(fd: int) -> None:
    import termios

    attributes = termios.tcgetattr(fd)
    attributes[0] = 0
    attributes[1] = 0
    attributes[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    attributes[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
    attributes[3] = 0
    attributes[4] = termios.B115200
    attributes[5] = termios.B115200
    attributes[6][termios.VMIN] = 0
    attributes[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attributes)
    termios.tcflush(fd, termios.TCIOFLUSH)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--ids", type=int, nargs="+", default=(1, 2))
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=0.1)
    parser.add_argument("--diagnostics", action="store_true", help="Read target, actual, current, error, status and temperature registers.")
    args = parser.parse_args()

    fd = os.open(args.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        configure_serial(fd)
        for device_id in args.ids:
            responses = 0
            for attempt in range(1, max(1, args.retries) + 1):
                import termios

                termios.tcflush(fd, termios.TCIFLUSH)
                os.write(fd, position_request(device_id))
                frame = read_response(fd, 20, max(0.01, args.timeout))
                positions = decode_positions(frame, device_id)
                if positions is None:
                    print(f"id={device_id} attempt={attempt}: no valid response raw={frame.hex() or '-'}")
                else:
                    responses += 1
                    print(f"id={device_id} attempt={attempt}: positions={positions}")
                time.sleep(0.02)
            status = "online" if responses else "offline"
            print(f"id={device_id}: {status} ({responses}/{max(1, args.retries)} replies)")
            if responses and args.diagnostics:
                print_diagnostics(fd, device_id, max(0.01, args.timeout))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
