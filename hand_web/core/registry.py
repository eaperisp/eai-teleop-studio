"""Explicit device registry; adding a hand does not change the web server."""

from __future__ import annotations

from typing import Type

from hand_web.adapters.brainco import BraincoAdapter
from hand_web.core.models import HandAdapter


ADAPTERS: dict[str, Type[HandAdapter]] = {
    "brainco_revo2": BraincoAdapter,
}


def adapter_class(device_id: str) -> Type[HandAdapter]:
    try:
        return ADAPTERS[device_id]
    except KeyError as exc:
        raise ValueError(f"不支持的灵巧手: {device_id}") from exc


def device_capabilities() -> list[dict]:
    return [adapter.capabilities() for adapter in ADAPTERS.values()]


__all__ = ["ADAPTERS", "adapter_class", "device_capabilities"]
