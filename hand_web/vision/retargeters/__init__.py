"""Map device-independent human hand features to robot joints."""

from hand_web.vision.retargeters.brainco_revo2 import BraincoRevo2Retargeter, SixJointHandRetargeter


RETARGETERS = {
    "brainco_revo2": BraincoRevo2Retargeter,
    "inspire_dfx": SixJointHandRetargeter,
    "inspire_ftp": SixJointHandRetargeter,
}


def create_retargeter(device_id: str, config: dict, profile: dict | None = None):
    try:
        return RETARGETERS[device_id](config, profile)
    except KeyError as exc:
        raise ValueError(f"{device_id} 尚未配置视觉控制重定向") from exc


__all__ = [
    "RETARGETERS",
    "BraincoRevo2Retargeter",
    "SixJointHandRetargeter",
    "create_retargeter",
]
