import asyncio
import importlib.util
import sys
import types
import unittest
from multiprocessing import Array, Value
from pathlib import Path


try:
    import vuer  # noqa: F401
except ImportError:
    vuer_module = types.ModuleType("vuer")
    vuer_module.Vuer = object
    schemas_module = types.ModuleType("vuer.schemas")
    for name in (
        "ImageBackground",
        "Hands",
        "MotionControllers",
        "WebRTCVideoPlane",
        "WebRTCStereoVideoPlane",
    ):
        setattr(schemas_module, name, object)
    sys.modules["vuer"] = vuer_module
    sys.modules["vuer.schemas"] = schemas_module


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "televuer" / "televuer.py"
SPEC = importlib.util.spec_from_file_location("televuer_motion_payload_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TeleVuer = MODULE.TeleVuer


class Event:
    def __init__(self, value):
        self.value = value


def make_controller_receiver():
    receiver = TeleVuer.__new__(TeleVuer)
    receiver._controller_move_event_count = 0
    receiver._controller_move_error_log_time = 0.0
    receiver.left_arm_pose_shared = Array("d", 16, lock=True)
    receiver.right_arm_pose_shared = Array("d", 16, lock=True)
    receiver.motion_data_ready_shared = Value("b", False, lock=True)
    receiver.motion_data_last_update_shared = Value("d", 0.0, lock=True)
    receiver.motion_data_stale_timeout = 0.5
    for side in ("left", "right"):
        for name in ("trigger", "squeeze", "thumbstick", "aButton", "bButton"):
            setattr(receiver, f"{side}_ctrl_{name}_shared", Value("b", False, lock=True))
        for name in ("triggerValue", "squeezeValue"):
            setattr(receiver, f"{side}_ctrl_{name}_shared", Value("d", 0.0, lock=True))
        setattr(receiver, f"{side}_ctrl_thumbstickValue_shared", Array("d", 2, lock=True))
    return receiver


class MotionPayloadTests(unittest.TestCase):
    def test_fixed_vector_accepts_flat_nested_matrix_and_numeric_mapping(self):
        flat = list(range(16))
        nested = [flat[index:index + 4] for index in range(0, 16, 4)]
        mapping = {str(index): value for index, value in reversed(list(enumerate(flat)))}

        self.assertEqual(TeleVuer._fixed_vector(flat, 16, "pose"), flat)
        self.assertEqual(TeleVuer._fixed_vector(nested, 16, "pose"), flat)
        self.assertEqual(TeleVuer._fixed_vector(mapping, 16, "pose"), flat)
        self.assertEqual(TeleVuer._fixed_vector([flat, True], 16, "pose"), flat)

    def test_fixed_vector_rejects_wrong_size_and_non_finite_values(self):
        with self.assertRaisesRegex(ValueError, "expected 16 values"):
            TeleVuer._fixed_vector([1, 2, 3], 16, "pose")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            TeleVuer._fixed_vector([0.0] * 15 + [float("nan")], 16, "pose")
        with self.assertRaisesRegex(ValueError, "tracking is invalid"):
            TeleVuer._fixed_vector([[0.0] * 16, False], 16, "pose")

    def test_valid_side_remains_ready_when_peer_or_optional_state_is_malformed(self):
        receiver = make_controller_receiver()
        right_pose = list(range(16))
        event = Event({
            "left": [1, 2, 3],
            "right": [{str(index): value for index, value in enumerate(right_pose)}, True],
            "leftState": {"thumbstickValue": [0.25]},
            "rightState": {"thumbstickValue": [0.5, -0.5]},
        })

        asyncio.run(receiver.on_controller_move(event, None))

        self.assertTrue(receiver.motion_data_ready_shared.value)
        self.assertEqual(list(receiver.right_arm_pose_shared[:]), right_pose)
        self.assertEqual(list(receiver.right_ctrl_thumbstickValue_shared[:]), [0.5, -0.5])

    def test_empty_initial_event_does_not_mark_motion_ready(self):
        receiver = make_controller_receiver()

        asyncio.run(receiver.on_controller_move(Event({}), None))

        self.assertFalse(receiver.motion_data_ready_shared.value)

    def test_motion_ready_expires_when_controller_updates_stop(self):
        receiver = make_controller_receiver()
        receiver._mark_motion_data_ready()
        self.assertTrue(receiver.motion_data_ready)

        with receiver.motion_data_last_update_shared.get_lock():
            receiver.motion_data_last_update_shared.value -= 1.0

        self.assertFalse(receiver.motion_data_ready)


if __name__ == "__main__":
    unittest.main()
