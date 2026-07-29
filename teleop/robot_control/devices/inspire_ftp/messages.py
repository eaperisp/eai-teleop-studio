"""DDS message types for the Inspire FTP dexterous hand.

The teleoperation controller only needs angle-position mode:

- ``inspire_hand_ctrl.angle_set``: six desired joint positions, scaled 0-1000.
- ``inspire_hand_ctrl.mode``: control mode, where 1 means angle control.
- ``inspire_hand_state.angle_act``: six measured joint positions, scaled 0-1000.

When CycloneDDS is installed these classes are real ``IdlStruct`` types and can
be passed to ``unitree_sdk2py.core.channel.ChannelPublisher`` /
``ChannelSubscriber``. The small fallback keeps local static checks importable
on development machines that do not have the robot DDS stack installed.
"""

from dataclasses import dataclass, field


try:
    import cyclonedds.idl as idl
    import cyclonedds.idl.annotations as annotate
    import cyclonedds.idl.types as types
except ModuleNotFoundError:
    idl = None
    annotate = None
    types = None


INSPIRE_HAND_DOF = 6
ANGLE_MIN = 0
ANGLE_MAX = 1000
ANGLE_CONTROL_MODE = 0b0001


def _zero_angles():
    return [0 for _ in range(INSPIRE_HAND_DOF)]


def _open_angles():
    return [ANGLE_MAX for _ in range(INSPIRE_HAND_DOF)]


if idl is not None:

    @dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class inspire_hand_ctrl(idl.IdlStruct, typename="inspire_hand_ctrl"):
        angle_set: types.array[types.uint16, INSPIRE_HAND_DOF] = field(default_factory=_open_angles)
        mode: types.uint8 = ANGLE_CONTROL_MODE


    @dataclass
    @annotate.final
    @annotate.autoid("sequential")
    class inspire_hand_state(idl.IdlStruct, typename="inspire_hand_state"):
        angle_act: types.array[types.uint16, INSPIRE_HAND_DOF] = field(default_factory=_zero_angles)

else:

    @dataclass
    class inspire_hand_ctrl:
        angle_set: list[int] = field(default_factory=_open_angles)
        mode: int = ANGLE_CONTROL_MODE


    @dataclass
    class inspire_hand_state:
        angle_act: list[int] = field(default_factory=_zero_angles)


__all__ = [
    "ANGLE_CONTROL_MODE",
    "ANGLE_MAX",
    "ANGLE_MIN",
    "INSPIRE_HAND_DOF",
    "inspire_hand_ctrl",
    "inspire_hand_state",
]
