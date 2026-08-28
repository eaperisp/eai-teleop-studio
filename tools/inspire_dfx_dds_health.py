"""Inspect raw Inspire DFX DDS state and serial-loss counters."""

from __future__ import annotations

import argparse
import time


STATE_TOPIC = "rt/inspire/state"
SIDES = (("right", 0), ("left", 6))


def _value(item, name: str):
    value = getattr(item, name)
    return value() if callable(value) else value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-interface", default=None)
    parser.add_argument("--domain", type=int, default=0)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_

    if args.network_interface:
        ChannelFactoryInitialize(args.domain, networkInterface=args.network_interface)
    else:
        ChannelFactoryInitialize(args.domain)

    subscriber = ChannelSubscriber(STATE_TOPIC, MotorStates_)
    subscriber.Init()
    history: dict[str, list[list[int]]] = {side: [] for side, _ in SIDES}

    for sample_index in range(max(1, args.samples)):
        message = subscriber.Read(1.0)
        if message is None or len(message.states) < 12:
            print(f"sample={sample_index + 1}: state timeout")
        else:
            for side, offset in SIDES:
                states = message.states[offset:offset + 6]
                positions = [round(float(_value(state, "q")), 3) for state in states]
                lost = [int(_value(state, "lost")) for state in states]
                history[side].append(lost)
                print(f"sample={sample_index + 1} side={side} q={positions} lost={lost}")
        if sample_index + 1 < args.samples:
            time.sleep(max(0.0, args.interval))

    print("summary:")
    for side, _ in SIDES:
        samples = history[side]
        if len(samples) < 2:
            print(f"  {side}: insufficient DDS samples")
            continue
        delta = [samples[-1][index] - samples[0][index] for index in range(6)]
        status = "serial feedback missing" if any(value > 0 for value in delta) else "feedback stable"
        print(f"  {side}: lost_delta={delta} ({status})")


if __name__ == "__main__":
    main()
