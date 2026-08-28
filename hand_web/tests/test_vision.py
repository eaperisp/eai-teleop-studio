from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hand_web.core.service import HandControlService
from hand_web.vision.features import HandFeatures
from hand_web.vision.manager import VisionManager
from hand_web.vision.features import HandFeatureExtractor
from hand_web.vision.mappers.brainco_revo2 import BraincoRevo2VisionMapper
from hand_web.vision.mediapipe_runtime import MediaPipeRuntime
from hand_web.vision.models import HandDetection


class Point:
    def __init__(self, x: float, y: float, z: float = 0.0) -> None:
        self.x = x
        self.y = y
        self.z = z


def landmarks() -> list[Point]:
    return [Point((index % 5) * 0.08, (index // 5) * 0.08, index * 0.002) for index in range(21)]


class FakeAdapter:
    def __init__(self) -> None:
        self.connected = False
        self.commands = []

    def connect(self, transport, options):
        self.connected = True
        return {"ok": True, "message": "connected"}

    def disconnect(self):
        self.connected = False
        return {"ok": True}

    def status(self):
        return {"ok": True, "connected": self.connected, "hands": {"right": {"online": True, "positions": [0.0] * 6}}}

    def command(self, side, positions, duration_ms):
        self.commands.append(list(positions))
        return {"ok": True, "positions": positions}

    def stop(self):
        return {"ok": True, "message": "stopped"}


class FakeRuntime:
    def __init__(self, _config):
        self.closed = False

    def read(self):
        time.sleep(0.005)
        return object(), HandDetection(landmarks(), "right", "Fist")

    def encode(self, _frame, _detection, _lines):
        return b"jpeg"

    def close(self):
        self.closed = True


class BrowserRuntime(FakeRuntime):
    def __init__(self, config):
        super().__init__(config)
        self.frames = []

    def read(self):
        time.sleep(0.005)
        return None, None

    def submit_frame(self, payload):
        self.frames.append(payload)


class CalibrationMapper:
    def __init__(self):
        self.last_features = None
        self.profile = None

    def map(self, points, _gesture=None, _timestamp=None):
        marker = round(points[0].x)
        values = {
            0: HandFeatures(5, 50, 5, 5, 5, 5),
            1: HandFeatures(65, 30, 85, 85, 85, 85),
            2: HandFeatures(5, 52, 5, 5, 5, 5),
            3: HandFeatures(5, 10, 5, 5, 5, 5),
        }
        self.last_features = values[marker]
        return [0.0] * 6

    def set_profile(self, profile):
        self.profile = profile

    def reset(self):
        self.last_features = None


class CalibrationRuntime(FakeRuntime):
    def __init__(self, config):
        super().__init__(config)
        self.marker = 0

    def read(self):
        time.sleep(0.002)
        points = landmarks()
        points[0].x = float(self.marker)
        return object(), HandDetection(points, "right", None, world_landmarks=points)


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = HandControlService(Path(self.temp_dir.name) / "missing.json")

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_mapper_applies_joint_limits(self):
        mapper = BraincoRevo2VisionMapper({"joint_limits": [[0, 0.2]] * 6, "endpoint_snap": 0})
        with patch.object(HandFeatureExtractor, "extract") as extract:
            extract.return_value = type("Features", (), {
                "thumb_flex": 100.0, "thumb_aux": 0.0, "index": 150.0,
                "middle": 150.0, "ring": 150.0, "pinky": 150.0,
            })()
            result = mapper.map(landmarks(), timestamp=1.0)
        self.assertEqual(result, [0.2] * 6)

    def test_open_hand_gesture_anchors_four_fingers_when_curls_agree(self):
        mapper = BraincoRevo2VisionMapper({
            "endpoint_snap": 0,
            "deadband": 0,
            "thumb_flex_aux_coupling": 0.9,
        })
        with patch.object(HandFeatureExtractor, "extract") as extract:
            extract.return_value = HandFeatures(5.0, 25.0, 12.0, 12.0, 12.0, 12.0)
            result = mapper.map(landmarks(), "Open Hand", timestamp=1.0)
        self.assertEqual(result, [0.72, 0.8, 0.0, 0.0, 0.0, 0.0])

    def test_open_hand_misclassification_keeps_visible_finger_curls(self):
        mapper = BraincoRevo2VisionMapper({
            "endpoint_snap": 0,
            "deadband": 0,
            "thumb_flex_aux_coupling": 0.9,
        })
        with patch.object(HandFeatureExtractor, "extract") as extract:
            extract.return_value = HandFeatures(5.0, 25.0, 35.0, 35.0, 35.0, 35.0)
            result = mapper.map(landmarks(), "Open Hand", timestamp=1.0)

        self.assertTrue(all(value > 0.0 for value in result[2:]))

    def test_thumb_adduction_couples_into_revo2_thumb_flex(self):
        mapper = BraincoRevo2VisionMapper({
            "endpoint_snap": 0,
            "deadband": 0,
            "thumb_flex_aux_coupling": 0.9,
        })
        with patch.object(HandFeatureExtractor, "extract") as extract:
            extract.return_value = HandFeatures(5.0, 25.0, 8.0, 8.0, 8.0, 8.0)
            result = mapper.map(landmarks(), None, timestamp=1.0)
        self.assertAlmostEqual(result[0], 0.72)
        self.assertAlmostEqual(result[1], 0.8)

    def test_default_ranges_align_fully_closed_fingers(self):
        mapper = BraincoRevo2VisionMapper({"deadband": 0})
        with patch.object(HandFeatureExtractor, "extract") as extract:
            extract.return_value = HandFeatures(75.0, 15.0, 48.0, 60.0, 78.0, 75.6)
            result = mapper.map(landmarks(), None, timestamp=1.0)
        self.assertEqual(result[2:], [1.0, 1.0, 1.0, 1.0])

    def test_thumb_aux_uses_palm_local_angle(self):
        points = landmarks()
        points[0] = Point(0.0, 0.0)
        points[5] = Point(0.5, 0.8)
        points[9] = Point(0.0, 0.9)
        points[17] = Point(-0.5, 0.7)
        points[1] = Point(0.2, 0.2)
        points[2] = Point(0.3, 0.35)
        points[4] = Point(0.9, 0.55)
        abducted = HandFeatureExtractor().extract(points).thumb_aux
        points[4] = Point(0.35, 0.85)
        adducted = HandFeatureExtractor().extract(points).thumb_aux
        self.assertGreater(abducted, adducted)

    def test_four_open_fingers_with_folded_thumb_is_not_open_hand(self):
        points = [Point(0.5, 0.8) for _ in range(21)]
        points[5] = Point(0.35, 0.55)
        points[17] = Point(0.75, 0.58)
        points[4] = Point(0.70, 0.58)
        for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
            points[tip] = Point(0.5, 0.2)
            points[pip] = Point(0.5, 0.4)

        self.assertIsNone(MediaPipeRuntime._gesture(points))

    def test_recognition_only_does_not_require_device_connection(self):
        manager = VisionManager(self.service, runtime_factory=FakeRuntime)
        try:
            manager.start({"dry_run": True, "side": "right"})
            time.sleep(0.04)
            result = manager.stop()
            self.assertFalse(result["running"])
            self.assertGreater(result["detections"], 0)
            self.assertEqual(result["commands"], 0)
            self.assertIsNone(self.service.status()["control_owner"])
        finally:
            manager.close()

    def test_browser_frame_is_forwarded_to_running_runtime(self):
        runtime = BrowserRuntime({})
        manager = VisionManager(self.service, runtime_factory=lambda _config: runtime)
        try:
            status = manager.start({"source": "browser", "dry_run": True, "side": "right"})
            self.assertEqual(status["source"], "browser")
            self.assertEqual(manager.submit_frame(b"jpeg"), {"ok": True})
            self.assertEqual(runtime.frames, [b"jpeg"])
        finally:
            manager.close()

    def test_manager_prefers_world_landmarks_and_saves_calibration(self):
        runtime = CalibrationRuntime({})
        mapper = CalibrationMapper()
        calibration_path = Path(self.temp_dir.name) / "vision_calibration.json"
        with patch("hand_web.vision.manager.create_mapper", return_value=mapper):
            manager = VisionManager(
                self.service,
                runtime_factory=lambda _config: runtime,
                calibration_path=calibration_path,
            )
            try:
                manager.start({"dry_run": True, "side": "right"})
                for marker, stage in enumerate(("open", "fist", "thumb_abducted", "thumb_adducted")):
                    runtime.marker = marker
                    manager.capture_calibration({"stage": stage, "sample_count": 12})
                    deadline = time.time() + 1.0
                    while manager.status()["calibration"]["active_stage"] is not None and time.time() < deadline:
                        time.sleep(0.005)
                status = manager.status()
                self.assertEqual(status["tracking_space"], "world")
                self.assertTrue(status["calibration"]["profile_available"])
                self.assertEqual(mapper.profile["ranges"]["thumb_aux"], [52.0, 10.0])
                self.assertTrue(calibration_path.exists())
            finally:
                manager.close()

    def test_concurrent_start_is_rejected_while_camera_opens(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_runtime(config):
            entered.set()
            release.wait(1.0)
            return FakeRuntime(config)

        manager = VisionManager(self.service, runtime_factory=slow_runtime)
        thread = threading.Thread(target=lambda: manager.start({"dry_run": True, "side": "right"}))
        try:
            thread.start()
            self.assertTrue(entered.wait(0.5))
            self.assertTrue(manager.status()["starting"])
            with self.assertRaisesRegex(RuntimeError, "已经在运行"):
                manager.start({"dry_run": True, "side": "right"})
        finally:
            release.set()
            thread.join(1.0)
            manager.close()

    @patch("hand_web.core.service.adapter_class", return_value=FakeAdapter)
    def test_visual_control_blocks_manual_and_releases_owner(self, _adapter_class):
        self.service.connect({"device_id": "brainco_revo2", "transport": "modbus", "options": {"side": "right"}})
        manager = VisionManager(self.service, runtime_factory=FakeRuntime)
        try:
            manager.start({
                "side": "right",
                "warmup_frames": 1,
                "min_interval": 0.01,
                "dry_run": False,
                "control_enabled": True,
            })
            time.sleep(0.04)
            self.assertEqual(self.service.status()["control_owner"], "vision")
            with self.assertRaises(RuntimeError):
                self.service.command({"side": "right", "positions": [0.2] * 6})
            result = manager.stop()
            self.assertGreater(result["commands"], 0)
            self.assertIsNone(self.service.status()["control_owner"])
        finally:
            manager.close()

    @patch("hand_web.core.service.adapter_class", return_value=FakeAdapter)
    def test_visual_control_requires_explicit_confirmation(self, _adapter_class):
        self.service.connect({"device_id": "brainco_revo2", "transport": "modbus", "options": {"side": "right"}})
        manager = VisionManager(self.service, runtime_factory=FakeRuntime)
        with self.assertRaisesRegex(ValueError, "control_enabled"):
            manager.start({"side": "right", "dry_run": False})


if __name__ == "__main__":
    unittest.main()
