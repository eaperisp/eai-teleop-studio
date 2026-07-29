"""Default message factories for Inspire FTP hand DDS messages."""

from .messages import ANGLE_CONTROL_MODE, ANGLE_MAX, INSPIRE_HAND_DOF
from .messages import inspire_hand_ctrl, inspire_hand_state


def get_inspire_hand_ctrl():
    return inspire_hand_ctrl(
        angle_set=[ANGLE_MAX for _ in range(INSPIRE_HAND_DOF)],
        mode=ANGLE_CONTROL_MODE,
    )


def get_inspire_hand_state():
    return inspire_hand_state(
        angle_act=[0 for _ in range(INSPIRE_HAND_DOF)],
    )


__all__ = ["get_inspire_hand_ctrl", "get_inspire_hand_state"]
