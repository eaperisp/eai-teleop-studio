import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from teleop_web.training_prep import TrainingPrepManager
from tools.convert_h2_to_lerobot import validate_episode_ee_types, vector_layout_metadata
from tools.h2_openpi_official_vla import configure_policy_layout, validate_actions


class MotorDatasetPipelineTests(unittest.TestCase):
    def test_converter_records_motor_index_and_neutral_state(self):
        layout, indices, defaults = vector_layout_metadata(
            [("left_arm", 7), ("right_arm", 7), ("left_ee", 1), ("right_ee", 1)],
            {"left_ee": ["motor"], "right_ee": ["rubber"]},
        )
        self.assertEqual(layout[-2], {"key": "left_ee", "dim": 1, "offset": 14})
        self.assertEqual(indices, [14])
        self.assertEqual(defaults[14:], [0.5, 0.0])

    def test_converter_rejects_mixed_end_effector_types(self):
        episodes = [
            (Path("episode_0001"), {"data": [{"states": {"left_ee": {"type": "motor"}}}]}),
            (Path("episode_0002"), {"data": [{"states": {"left_ee": {"type": "rubber"}}}]}),
        ]
        with self.assertRaisesRegex(ValueError, "mixes multiple left_ee types"):
            validate_episode_ee_types(episodes)

    def test_converter_rejects_motor_and_none_mix(self):
        episodes = [
            (Path("episode_0001"), {"data": [{"states": {"left_ee": {"type": "motor"}}}]}),
            (Path("episode_0002"), {"data": [{"states": {"left_ee": {"type": "none"}}}]}),
        ]
        with self.assertRaisesRegex(ValueError, "mixes multiple left_ee types"):
            validate_episode_ee_types(episodes)

    def test_policy_keeps_motor_dimension_and_uses_neutral_state(self):
        args = argparse.Namespace(
            state_tail_values="",
            state_tail_zeros=2,
            motor_action_indices="14",
            motor_control_url="http://127.0.0.1:1/api/motor/control",
            motor_left_max=0.25,
            motor_right_min=0.75,
            extra_action_dims_policy="crop",
            action_horizon=16,
            max_abs_q=5.0,
        )
        controller = configure_policy_layout(args)
        self.assertEqual(args._expected_real_action_dim, 16)
        self.assertTrue(np.allclose(args._state_tail, [0.5, 0.0]))
        actions = validate_actions(np.zeros((3, 16), dtype=np.float32), args)
        self.assertEqual(actions.shape, (3, 16))
        with patch.object(controller, "_send") as send:
            self.assertEqual(controller.pulse(np.asarray([0.0] * 14 + [1.0, 0.0]), 1.0), "right")
            controller.stop()
        self.assertEqual([call.args[0] for call in send.call_args_list], ["right", "stop"])

    def test_training_set_real_action_dim_follows_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lerobot_home = root / "lerobot"
            repo_dir = lerobot_home / "local" / "motor_task"
            (repo_dir / "meta").mkdir(parents=True)
            (repo_dir / "metadata.json").write_text(
                json.dumps({"state_dim": 15, "action_dim": 15, "vector_dims": []}),
                encoding="utf-8",
            )
            (repo_dir / "meta" / "info.json").write_text(
                json.dumps({"total_episodes": 1, "total_frames": 10, "features": {}}),
                encoding="utf-8",
            )
            manager = TrainingPrepManager(
                root,
                dataset_root=root / "robot",
                lerobot_home=lerobot_home,
                openpi_assets_dir=root / "assets",
            )
            task = {
                "id": 1,
                "name": "motor_task",
                "instruction": "Open the door",
                "existing_episodes": 1,
                "postprocess_status": {
                    "repo_id": "local/motor_task",
                    "lerobot_ready": True,
                    "lerobot_episodes": 1,
                },
            }
            training_set = manager.create_training_set(
                {
                    "name": "motor_training",
                    "config_name": "pi05_motor",
                    "task_ids": [1],
                    "real_action_dim": 14,
                },
                [task],
            )
            self.assertEqual(training_set["real_action_dim"], 15)


if __name__ == "__main__":
    unittest.main()
