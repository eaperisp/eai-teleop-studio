"""Probe a BrainCo Revo2 Modbus link without sending motion commands."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
from typing import Any


def invoke(function: Any, *args: Any, **kwargs: Any) -> Any:
    async def call() -> Any:
        result = function(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return asyncio.run(call())


def close_client(library: Any, client: Any) -> None:
    try:
        close = getattr(library, "modbus_close", None)
        if close is not None:
            invoke(close, client)
        elif hasattr(client, "close"):
            client.close()
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read Revo2 device information across candidate ports and settings."
    )
    parser.add_argument("--ports", nargs="+", required=True)
    parser.add_argument("--slave-ids", nargs="+", type=int, default=[127, 1])
    parser.add_argument(
        "--baudrates",
        nargs="+",
        type=int,
        default=[460800, 115200],
        choices=[19200, 57600, 115200, 460800, 1000000, 2000000, 3000000, 4000000, 5000000, 6000000],
    )
    return parser.parse_args()


def baudrate_value(library: Any, value: int) -> Any:
    names = {
        19200: "Baud19200",
        57600: "Baud57600",
        115200: "Baud115200",
        460800: "Baud460800",
        1000000: "Baud1Mbps",
        2000000: "Baud2Mbps",
        3000000: "Baud3Mbps",
        4000000: "Baud4Mbps",
        5000000: "Baud5Mbps",
        6000000: "Baud6Mbps",
    }
    return getattr(library.Baudrate, names[value])


def main() -> int:
    args = parse_args()
    library = importlib.import_module("bc_stark_sdk.main_mod")
    success = False

    for port in args.ports:
        for baudrate in args.baudrates:
            for slave_id in args.slave_ids:
                client = None
                label = f"port={port} baud={baudrate} slave={slave_id}"
                try:
                    client = invoke(
                        library.modbus_open,
                        port_name=port,
                        baudrate=baudrate_value(library, baudrate),
                    )
                    info = invoke(client.get_device_info, slave_id)
                    print(
                        f"OK {label} serial={getattr(info, 'serial_number', '')} "
                        f"firmware={getattr(info, 'firmware_version', '')}"
                    )
                    success = True
                except Exception as exc:
                    print(f"FAIL {label}: {exc}")
                finally:
                    close_client(library, client)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
