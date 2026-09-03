from __future__ import annotations

import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import patch

from hand_web.core.service import HandControlService
from hand_web.adapters.inspire import InspireDFXAdapter, InspireFTPAdapter
from hand_web.server import ExclusiveThreadingHTTPServer, SingleInstanceLock, validate_hand_web_port
from teleop.robot_control.devices.base import normalize_positions
from teleop.robot_control.devices.brainco import BraincoHandSDK
from teleop.robot_control.devices.brainco.dds_transport import BraincoDDSTransport
from teleop.robot_control.devices.brainco.modbus_transport import BraincoModbusTransport
from teleop.robot_control.devices.brainco.modbus_transport import _normalize_port
from teleop.robot_control.devices.inspire_dfx import InspireDFXHandSDK
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


class FakeInspireState:
    def __init__(self, side, angles, lost=None):
        self.side = side
        self.angles = tuple(angles)
        self.lost = tuple(lost or [0] * 6)


class FakeInspireSDK:
    state_angles = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    state_lost = [0] * 6

    def __init__(self, **options):
        self.options = options
        self.initialized = False
        self.commands = []
        self.bursts = []

    def initialize(self):
        self.initialized = True

    def read_states(self, timeout=0.0):
        del timeout
        result = {}
        if self.options["enable_left"]:
            result["left"] = FakeInspireState("left", self.state_angles, self.state_lost)
        if self.options["enable_right"]:
            result["right"] = FakeInspireState("right", self.state_angles, self.state_lost)
        return result

    def command(self, side, angles):
        values = list(angles)
        self.commands.append((side, values))
        return values

    def command_burst(self, side, angles, duration=0.4, rate=20.0):
        values = self.command(side, angles)
        self.bursts.append((side, values, duration, rate))
        return values

    def cancel_command_burst(self):
        return None


class FakeDDSMotor:
    def __init__(self, q, lost=0):
        self.q = q
        self.lost = lost


class FakeDDSState:
    def __init__(self):
        self.states = [FakeDDSMotor(index / 10) for index in range(12)]


class FakeDDSSubscriber:
    def __init__(self):
        self.read_count = 0

    def Read(self, timeout=0.0):
        del timeout
        self.read_count += 1
        return FakeDDSState()


class FakeDDSPublisher:
    def __init__(self):
        self.writes = []

    def Write(self, message):
        self.writes.append([command.q for command in message.cmds])


class FakeDDSCommandMessage:
    def __init__(self):
        self.cmds = [FakeDDSMotor(1.0) for _ in range(12)]


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
        self.assertEqual(payload["defaults"]["brainco_revo2"]["dds"]["network_interface"], "enp86s0")
        self.assertEqual(payload["default_device"], "inspire_dfx")
        self.assertEqual(
            {item["id"] for item in payload["devices"]},
            {"brainco_revo2", "inspire_dfx", "inspire_ftp"},
        )

    @patch("hand_web.core.service.adapter_class", return_value=FakeAdapter)
    def test_connect_command_status_and_disconnect(self, _adapter_class):
        result = self.service.connect({
            "device_id": "brainco_revo2",
            "transport": "modbus",
            "options": {"side": "right"},
        })
        self.assertTrue(result["ok"])
        self.assertTrue(self.service.status()["connected"])

        adapter = self.service._adapter
        repeated = self.service.connect({
            "device_id": "brainco_revo2",
            "transport": "modbus",
            "options": {"side": "right"},
        })
        self.assertTrue(repeated["reused"])
        self.assertIs(self.service._adapter, adapter)

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

    def test_single_instance_lock_blocks_a_second_http_port(self):
        lock_path = Path(self.temp_dir.name) / "hand-web.lock"
        first = SingleInstanceLock(lock_path)
        second = SingleInstanceLock(lock_path)
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_hand_web_port_does_not_overlap_robot_sync(self):
        self.assertEqual(validate_hand_web_port(18089), 18089)
        with self.assertRaisesRegex(ValueError, "数据同步服务"):
            validate_hand_web_port(18090)

    @unittest.skipIf(os.name == "nt", "Windows uses SO_EXCLUSIVEADDRUSE")
    def test_http_port_can_be_rebound_immediately_after_shutdown(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format, *args):
                del args

        server = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                self.assertEqual(response.status, 204)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        rebound = ExclusiveThreadingHTTPServer(("127.0.0.1", port), Handler)
        rebound.server_close()

    def test_http_connections_have_a_bounded_read_timeout(self):
        class Handler(BaseHTTPRequestHandler):
            pass

        server = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), Handler)
        client = socket.create_connection(server.server_address, timeout=2)
        request = None
        try:
            request, _client_address = server.get_request()
            self.assertEqual(request.gettimeout(), server.request_timeout_seconds)
        finally:
            if request is not None:
                request.close()
            client.close()
            server.server_close()

    def test_expected_tls_disconnect_does_not_print_a_traceback(self):
        server = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            error = ssl.SSLError("certificate unknown")
            with patch("hand_web.server.sys.exc_info", return_value=(ssl.SSLError, error, None)):
                with patch.object(ThreadingHTTPServer, "handle_error") as inherited:
                    server.handle_error(None, ("127.0.0.1", 12345))
            inherited.assert_not_called()
        finally:
            server.server_close()


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


class InspireAdapterTests(unittest.TestCase):
    def test_dfx_command_burst_republishes_latest_target(self):
        sdk = InspireDFXHandSDK(enable_left=True, enable_right=True)
        publisher = FakeDDSPublisher()
        sdk._initialized = True
        sdk._state_synced = True
        sdk._publisher = publisher
        sdk._cmd_msg = FakeDDSCommandMessage()

        sdk.command_burst("left", [0.25] * 6, duration=0.06, rate=50.0)
        time.sleep(0.09)

        self.assertGreaterEqual(len(publisher.writes), 3)
        self.assertEqual(publisher.writes[-1][:6], [1.0] * 6)
        self.assertEqual(publisher.writes[-1][6:], [0.25] * 6)

    def test_dfx_reads_one_aggregate_sample_for_both_hands(self):
        sdk = InspireDFXHandSDK(enable_left=True, enable_right=True)
        subscriber = FakeDDSSubscriber()
        sdk._initialized = True
        sdk._subscriber = subscriber

        states = sdk.read_states(timeout=0.01)

        self.assertEqual(subscriber.read_count, 1)
        self.assertEqual(states["right"].angles, (0.0, 0.1, 0.2, 0.3, 0.4, 0.5))
        self.assertEqual(states["left"].angles, (0.6, 0.7, 0.8, 0.9, 1.0, 1.1))
        self.assertEqual(states["left"].lost, (0, 0, 0, 0, 0, 0))

    def test_dfx_stable_cumulative_lost_feedback_is_online(self):
        with patch.object(InspireDFXAdapter, "sdk_type", FakeInspireSDK):
            with patch.object(FakeInspireSDK, "state_lost", [3] * 6):
                adapter = InspireDFXAdapter()
                adapter.connect("dds", {"sides": "left"})
                status = adapter.status()

        self.assertTrue(status["hands"]["left"]["online"])
        self.assertIsNotNone(status["hands"]["left"]["positions"])
        self.assertEqual(status["error"], "")

    def test_dfx_advancing_lost_feedback_is_reported_offline(self):
        with patch.object(InspireDFXAdapter, "sdk_type", FakeInspireSDK):
            with patch.object(FakeInspireSDK, "state_lost", [3] * 6):
                adapter = InspireDFXAdapter()
                adapter.connect("dds", {"sides": "left"})
            with patch.object(FakeInspireSDK, "state_lost", [4] * 6):
                status = adapter.status()

        self.assertFalse(status["hands"]["left"]["online"])
        self.assertIsNone(status["hands"]["left"]["positions"])
        self.assertIn("左手", status["error"])

    def test_dfx_mapping_uses_web_order_and_open_closed_convention(self):
        with patch.object(InspireDFXAdapter, "sdk_type", FakeInspireSDK):
            adapter = InspireDFXAdapter()
            adapter.connect("dds", {"network_interface": "enp86s0", "sides": "right"})
            result = adapter.command("right", [0.0, 0.1, 0.2, 0.3, 0.4, 0.5], 500)

        self.assertEqual(result["positions"], [0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
        self.assertEqual(adapter._sdk.commands[-1], ("right", [0.5, 0.6, 0.7, 0.8, 1.0, 0.9]))
        self.assertEqual(adapter._sdk.bursts[-1][2:], (0.5, 20.0))
        self.assertEqual(
            adapter.status()["hands"]["right"]["positions"],
            [0.5, 0.4, 0.6, 0.7, 0.8, 0.9],
        )

    def test_ftp_mapping_scales_to_vendor_units(self):
        with patch.object(InspireFTPAdapter, "sdk_type", FakeInspireSDK):
            adapter = InspireFTPAdapter()
            adapter.connect("dds", {"sides": "left"})
            adapter.command("left", [0.0, 0.1, 0.2, 0.3, 0.4, 0.5], 500)

        self.assertEqual(adapter._sdk.commands[-1], ("left", [500, 600, 700, 800, 1000, 900]))

    def test_idle_stop_does_not_publish_and_active_stop_holds_state(self):
        with patch.object(InspireDFXAdapter, "sdk_type", FakeInspireSDK):
            adapter = InspireDFXAdapter()
            adapter.connect("dds", {"sides": "right"})
            sdk = adapter._sdk
            idle = adapter.stop()
            adapter.command("right", [0.5] * 6, 0)
            active = adapter.stop()

        self.assertEqual(idle["message"], "当前没有执行中的运动")
        self.assertEqual(len(sdk.commands), 2)
        self.assertEqual(active["message"], "已停止更新目标并保持当前位置")

    def test_inspire_capabilities_keep_semantic_joint_order(self):
        capabilities = InspireDFXAdapter.capabilities()
        self.assertEqual(
            [joint["id"] for joint in capabilities["joints"]],
            ["thumb", "thumb_aux", "index", "middle", "ring", "pinky"],
        )
        self.assertEqual(capabilities["preview"]["urdf"], "inspire_hand_{side}.urdf")


if __name__ == "__main__":
    unittest.main()
