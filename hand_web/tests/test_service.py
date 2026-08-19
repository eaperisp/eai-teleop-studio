from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hand_web.core.service import HandControlService
from teleop.robot_control.devices.base import normalize_positions
from teleop.robot_control.devices.brainco import BraincoHandSDK
from teleop.robot_control.devices.brainco.dds_transport import BraincoDDSTransport
from teleop.robot_control.devices.brainco.modbus_transport import BraincoModbusTransport
from teleop.robot_control.devices.brainco.modbus_transport import _normalize_port
from teleop.utils.daily_file_logger import DailyFileLogger


class FakeAdapter:
    def __init__(self) -> None:
        self.connected = False
        self.transport = None
        self.options = None

    def connect(self, transport, options):
        self.connected = True
        self.transport = transport
        self.options = options
        return {"ok": True, "message": "connected"}

    def disconnect(self):
        self.connected = False
        return {"ok": True, "message": "disconnected"}

    def status(self):
        return {
            "ok": True,
            "connected": self.connected,
            "hands": {"right": {"online": self.connected, "positions": [0.0] * 6}},
        }

    def command(self, side, positions, duration_ms):
        return {
            "ok": True,
            "side": side,
            "positions": positions,
            "duration_ms": duration_ms,
        }

    def stop(self):
        return {"ok": True, "message": "stopped"}


class FakeMotorStatus:
    positions = [500] * 6


class FakeModbusClient:
    def __init__(self) -> None:
        self.position_commands = []
        self.speed_commands = []

    def set_finger_positions_and_durations(self, slave_id, positions, durations):
        self.position_commands.append((slave_id, positions, durations))

    def set_finger_speeds(self, slave_id, speeds):
        self.speed_commands.append((slave_id, speeds))

    def get_motor_status(self, slave_id):
        return FakeMotorStatus()


class HandControlServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = HandControlService(Path(self.temp_dir.name) / "missing.json")

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_device_schema_is_transport_neutral(self):
        payload = self.service.devices()
        device = payload["devices"][0]
        self.assertEqual(device["id"], "brainco_revo2")
        self.assertEqual(len(device["joints"]), 6)
        self.assertEqual({item["id"] for item in device["transports"]}, {"dds", "modbus"})

    @patch("hand_web.core.service.adapter_class", return_value=FakeAdapter)
    def test_connect_command_status_and_disconnect(self, _adapter_class):
        result = self.service.connect({
            "device_id": "brainco_revo2",
            "transport": "modbus",
            "options": {"side": "right"},
        })
        self.assertTrue(result["ok"])
        self.assertTrue(self.service.status()["connected"])

        command = self.service.command({
            "side": "right",
            "positions": [0.25] * 6,
            "duration_ms": 350,
        })
        self.assertEqual(command["positions"], [0.25] * 6)
        self.assertEqual(command["duration_ms"], 350)
        self.assertTrue(self.service.disconnect()["ok"])
        self.assertFalse(self.service.status()["connected"])

    @patch("hand_web.core.service.adapter_class", return_value=FakeAdapter)
    def test_operations_use_shared_daily_logger(self, _adapter_class):
        logger = DailyFileLogger(Path(self.temp_dir.name) / "logs", filename_prefix="hand_web")
        service = HandControlService(Path(self.temp_dir.name) / "missing.json", logger=logger)
        try:
            service.connect({
                "device_id": "brainco_revo2",
                "transport": "modbus",
                "options": {"side": "right"},
            })
            service.command({"side": "right", "positions": [0.2] * 6, "duration_ms": 300})
            service.command({
                "side": "right",
                "positions": [0.3] * 6,
                "duration_ms": 300,
                "continuous": True,
            })
            service.command({
                "side": "right",
                "positions": [0.4] * 6,
                "duration_ms": 300,
                "continuous": True,
            })
            service.disconnect()
        finally:
            service.close()

        log_path = logger._path_for_today()
        self.assertTrue(log_path.name.startswith("hand_web_"))
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("hand connected", content)
        self.assertIn("hand command sent", content)
        self.assertEqual(content.count("hand continuous command sampled"), 1)
        self.assertIn("hand disconnected", content)


class BraincoModelTests(unittest.TestCase):
    @patch.object(BraincoModbusTransport, "_load_sdk")
    def test_modbus_noop_target_and_idle_stop_do_not_write(self, _load_sdk):
        transport = BraincoModbusTransport(side="right")
        client = FakeModbusClient()
        transport._client = client
        transport._connected = True
        transport._positions = [0.5] * 6
        transport._targets = [0.5] * 6

        transport.command("right", [0.5] * 6, 500)
        idle_stop = transport.stop()

        self.assertEqual(client.position_commands, [])
        self.assertEqual(client.speed_commands, [])
        self.assertEqual(idle_stop["message"], "当前没有执行中的运动")

        transport.command("right", [0.7] * 6, 500)
        active_stop = transport.stop()
        self.assertEqual(len(client.position_commands), 1)
        self.assertEqual(client.speed_commands, [(127, [0] * 6)])
        self.assertEqual(active_stop["message"], "已发送停止指令")

    def test_dds_idle_stop_does_not_publish(self):
        transport = BraincoDDSTransport(sides="right", continuous_publish=True)
        transport._connected = True
        result = transport.stop()
        self.assertEqual(result["message"], "当前没有执行中的运动")

    def test_serial_port_normalization_preserves_linux_paths(self):
        self.assertEqual(_normalize_port(" com6 "), "COM6")
        self.assertEqual(_normalize_port("/dev/ttyUSB0"), "/dev/ttyUSB0")

    def test_positions_are_strictly_normalized(self):
        self.assertEqual(normalize_positions([0, 0.25, 0.5, 0.75, 1, 0], 6)[4], 1.0)
        with self.assertRaises(ValueError):
            normalize_positions([0, 0, 0, 0, 0, 1.1], 6)
        with self.assertRaises(ValueError):
            normalize_positions([0, 0], 6)

    def test_brainco_joint_order_is_exposed_once(self):
        capabilities = BraincoHandSDK.capabilities()
        self.assertEqual(
            [joint["id"] for joint in capabilities["joints"]],
            ["thumb", "thumb_aux", "index", "middle", "ring", "pinky"],
        )


if __name__ == "__main__":
    unittest.main()
