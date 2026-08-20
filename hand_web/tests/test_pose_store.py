from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hand_web.core.models import ValidationError
from hand_web.core.pose_store import PoseStore


class PoseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "poses.json"
        self.store = PoseStore(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_defaults_are_available_without_creating_a_file(self):
        result = self.store.list("brainco_revo2")
        self.assertEqual([pose["id"] for pose in result["poses"]], ["open", "half", "close"])
        self.assertFalse(self.path.exists())

    def test_create_update_delete_and_reload(self):
        created = self.store.save({
            "device_id": "brainco_revo2",
            "name_en": "Panel Knob Turn",
            "description_zh": "旋转配电柜旋钮",
            "positions": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        })["pose"]
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.store.list("brainco_revo2")["poses"]), 4)

        updated = self.store.save({
            "device_id": "brainco_revo2",
            "id": created["id"],
            "name_en": "Cabinet Knob Turn",
            "description_zh": "旋转配电柜手势",
            "positions": [0.1] * 6,
        })["pose"]
        self.assertEqual(updated["description_zh"], "旋转配电柜手势")
        self.assertEqual(PoseStore(self.path).list("brainco_revo2")["poses"][-1]["positions"], [0.1] * 6)

        result = self.store.delete({"device_id": "brainco_revo2", "id": created["id"]})
        self.assertEqual(len(result["poses"]), 3)

    def test_rejects_invalid_joint_data(self):
        with self.assertRaisesRegex(ValidationError, "关节数据"):
            self.store.save({
                "device_id": "brainco_revo2",
                "name_en": "Invalid",
                "description_zh": "无效",
                "positions": [0.2, 0.3],
            })


if __name__ == "__main__":
    unittest.main()
