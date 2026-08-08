import json
import tempfile
import unittest
from pathlib import Path


def config_payload() -> dict:
    return {
        "device_id": "machine_01",
        "senders": [
            {
                "sender_id": "sender_01",
                "bearing_id": "bearing_01",
                "scheduler_url": "http://127.0.0.1:18001/scheduler/decide",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": 11881,
            },
            {
                "sender_id": "sender_02",
                "bearing_id": "bearing_02",
                "scheduler_url": "http://127.0.0.1:18002/scheduler/decide",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": 11882,
            },
            {
                "sender_id": "sender_03",
                "bearing_id": "bearing_03",
                "scheduler_url": "http://127.0.0.1:18003/scheduler/decide",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": 11883,
            },
        ],
        "scheduler_timeout_seconds": 2.0,
        "schedule_max_retries": 2,
        "mqtt_keepalive_seconds": 30,
        "qos": 1,
        "retain": False,
        "puback_warning_timeout_ms": 500,
        "packet_delivery_timeout_ms": 1000,
        "max_publish_retries": 2,
        "pending_queue_max_packets": 80,
        "task_duration_ms": 4000,
        "packet_interval_ms": 50,
        "expected_packet_count": 80,
        "log_dir": "runtime/logs",
        "state_dir": "runtime/state",
    }


def valid_data() -> dict:
    data = {
        name: {"sample_rate_hz": rate, "sample_count": 1, "values": [1.0]}
        for name, rate in {
            "vibration": 64000,
            "phase_current_1_A": 64000,
            "phase_current_2_A": 64000,
            "shaft_speed_rpm": 4000,
            "load_torque_nm": 4000,
            "bearing_radial_load_n": 4000,
        }.items()
    }
    data["bearing_module_temperature_c"] = 46.3
    return data


class ConfigTests(unittest.TestCase):
    def _write(self, root: Path, payload: dict) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_load_config_returns_three_independent_sender_nodes(self) -> None:
        from sender.config import load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = load_config(self._write(root, config_payload()))

            self.assertEqual(config.device_id, "machine_01")
            self.assertEqual([item.sender_id for item in config.senders], ["sender_01", "sender_02", "sender_03"])
            self.assertEqual([item.bearing_id for item in config.senders], ["bearing_01", "bearing_02", "bearing_03"])
            self.assertEqual(config.senders[1].mqtt_port, 11882)
            self.assertEqual(config.log_dir, root / "runtime" / "logs")
            self.assertEqual(config.state_dir, root / "runtime" / "state")

    def test_load_config_rejects_duplicate_sender_or_bearing(self) -> None:
        from sender.config import ConfigError, load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            duplicate_sender = config_payload()
            duplicate_sender["senders"][1]["sender_id"] = "sender_01"
            with self.assertRaisesRegex(ConfigError, "sender_id"):
                load_config(self._write(root, duplicate_sender))

            duplicate_bearing = config_payload()
            duplicate_bearing["senders"][1]["bearing_id"] = "bearing_01"
            with self.assertRaisesRegex(ConfigError, "bearing_id"):
                load_config(self._write(root, duplicate_bearing))

    def test_load_config_rejects_inconsistent_packet_count(self) -> None:
        from sender.config import ConfigError, load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            payload = config_payload()
            payload["expected_packet_count"] = 79
            with self.assertRaises(ConfigError):
                load_config(self._write(Path(temp_dir), payload))

    def test_load_config_keeps_fixed_fifty_ms_eighty_packet_contract(self) -> None:
        from sender.config import ConfigError, load_config

        invalid_values = (
            ("packet_interval_ms", 100),
            ("expected_packet_count", 40),
            ("task_duration_ms", 2000),
        )
        for field, value in invalid_values:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                payload = config_payload()
                payload[field] = value
                with self.assertRaisesRegex(ConfigError, "50 ms.*80 packets.*4000 ms"):
                    load_config(self._write(Path(temp_dir), payload))


class IdTests(unittest.TestCase):
    def test_sender_task_ids_persist_independently(self) -> None:
        from sender.ids import TaskIdStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            one = root / "sender_01_task_counter.txt"
            two = root / "sender_02_task_counter.txt"
            self.assertEqual(TaskIdStore(one, "sender_01").next_task_id(), "sd_01_tk_0001")
            self.assertEqual(TaskIdStore(one, "sender_01").next_task_id(), "sd_01_tk_0002")
            self.assertEqual(TaskIdStore(two, "sender_02").next_task_id(), "sd_02_tk_0001")


class PacketTests(unittest.TestCase):
    def test_packet_uses_sender_task_and_bearing_in_packet_id(self) -> None:
        from sender.packet import build_sensor_packet

        packet = build_sensor_packet(
            device_id="machine_01",
            task_id="sd_01_tk_0001",
            bearing_id="bearing_01",
            sender_id="sender_01",
            sequence_number=1,
            data=valid_data(),
            end_generate_timestamp_ns=123,
        )

        self.assertEqual(packet["packet_id"], "sd_01_tk_0001_bearing_01_pkt_001")
        self.assertEqual(packet["sender_id"], "sender_01")
        self.assertNotIn("ground_truth", packet)

    def test_packet_rejects_task_id_owned_by_another_sender(self) -> None:
        from sender.packet import PacketValidationError, build_sensor_packet

        with self.assertRaisesRegex(PacketValidationError, "sender_id"):
            build_sensor_packet(
                device_id="machine_01",
                task_id="sd_02_tk_0001",
                bearing_id="bearing_01",
                sender_id="sender_01",
                sequence_number=1,
                data=valid_data(),
                end_generate_timestamp_ns=123,
            )

    def test_packet_rejects_sample_count_mismatch(self) -> None:
        from sender.packet import PacketValidationError, build_sensor_packet

        data = valid_data()
        data["vibration"]["sample_count"] = 2
        with self.assertRaises(PacketValidationError):
            build_sensor_packet(
                device_id="machine_01",
                task_id="sd_01_tk_0001",
                bearing_id="bearing_01",
                sender_id="sender_01",
                sequence_number=1,
                data=data,
                end_generate_timestamp_ns=123,
            )

    def test_serialized_packet_rejects_nan(self) -> None:
        from sender.packet import PacketValidationError, serialize_packet

        with self.assertRaises(PacketValidationError):
            serialize_packet({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
