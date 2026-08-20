from __future__ import annotations

import unittest
from argparse import Namespace

from tools.generate_h2_camera_config import build_config


class GenerateH2CameraConfigTests(unittest.TestCase):
    def test_requested_head_depth_is_enabled(self):
        args = Namespace(
            head_serial="head",
            torso_serial="torso",
            left_serial="left",
            right_serial="right",
            allow_missing=True,
            color_size=(1920, 1080),
            head_color_size=(1920, 1080),
            depth_size=(640, 480),
            head_depth_size=(1280, 800),
            color_fps=30,
            depth_fps=30,
            prefer_color_interface="1.4",
            prefer_color_index=0,
            prefer_depth_interface="1.0",
            prefer_depth_index=0,
            depth="head",
        )

        blocks = dict(build_config(args, []))

        self.assertEqual(blocks["head_camera"]["image_shape"], [1080, 1920])
        self.assertEqual(blocks["torso_camera"]["image_shape"], [1080, 1920])
        self.assertEqual(blocks["left_wrist_camera"]["image_shape"], [1080, 1920])
        self.assertEqual(blocks["right_wrist_camera"]["image_shape"], [1080, 1920])
        self.assertTrue(blocks["head_rgbd_camera"]["enable_zmq"])
        self.assertEqual(blocks["head_rgbd_camera"]["depth"]["image_shape"], [800, 1280])
        self.assertNotIn("torso_rgbd_camera", blocks)


if __name__ == "__main__":
    unittest.main()
