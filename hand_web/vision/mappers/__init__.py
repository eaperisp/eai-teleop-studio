"""Compatibility aliases for device retargeters."""

from hand_web.vision.retargeters import RETARGETERS, create_retargeter


MAPPERS = RETARGETERS


def create_mapper(device_id: str, config: dict, profile: dict | None = None):
    return create_retargeter(device_id, config, profile)


__all__ = ["MAPPERS", "create_mapper"]
