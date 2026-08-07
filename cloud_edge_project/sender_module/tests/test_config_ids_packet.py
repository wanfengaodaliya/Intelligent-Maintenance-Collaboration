import json
import tempfile
import unittest
from pathlib import Path


class ConfigTests(unittest.TestCase):
    def test_load_config_resolves_runtime_paths_and_validates_timeouts(self) -> None:
        from sender.config import load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sender_id": "sender_01",
                        "scheduler_url": "http://127.0.0.1:8003/scheduler/decide",
                        "scheduler_timeout_seconds": 2.0,
                        "schedule_max_retries": 2,
                        "mqtt_host": "127.0.0.1",
                        "mqtt_port": 1883,
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
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.sender_id, "sender_01")
            self.assertEqual(config.log_dir, root / "runtime" / "logs")
            self.assertEqual(config.state_dir, root / "runtime" / "state")
            self.assertLess(
                config.puback_warning_timeout_ms,
                config.packet_delivery_timeout_ms,
            )

    def test_load_config_rejects_inconsistent_packet_count(self) -> None:
        from sender.config import ConfigError, load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bad.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sender_id": "sender_01",
                        "scheduler_url": "http://127.0.0.1:8003/scheduler/decide",
                        "scheduler_timeout_seconds": 2.0,
                        "schedule_max_retries": 2,
                        "mqtt_host": "127.0.0.1",
                        "mqtt_port": 1883,
                        "mqtt_keepalive_seconds": 30,
                        "qos": 1,
                        "retain": False,
                        "puback_warning_timeout_ms": 500,
                        "packet_delivery_timeout_ms": 1000,
                        "max_publish_retries": 2,
                        "pending_queue_max_packets": 80,
                        "task_duration_ms": 4000,
                        "packet_interval_ms": 50,
                        "expected_packet_count": 79,
                        "log_dir": "runtime/logs",
                        "state_dir": "runtime/state",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)


class IdTests(unittest.TestCase):
    def test_task_ids_persist_across_store_instances(self) -> None:
        from sender.ids import TaskIdStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task_counter.txt"
            self.assertEqual(TaskIdStore(path).next_task_id(), "task_00001")
            self.assertEqual(TaskIdStore(path).next_task_id(), "task_00002")


class PacketTests(unittest.TestCase):
    def test_packet_uses_actual_sample_counts_and_expected_ids(self) -> None:
        from sender.packet import build_sensor_packet

        data = {
            "vibration": {
                "sample_rate_hz": 64000,
                "sample_count": 3,
                "values": [0.1, 0.2, 0.3],
            },
            "phase_current_1_A": {
                "sample_rate_hz": 64000,
                "sample_count": 2,
                "values": [1.0, 1.1],
            },
            "phase_current_2_A": {
                "sample_rate_hz": 64000,
                "sample_count": 2,
                "values": [-1.0, -1.1],
            },
            "shaft_speed_rpm": {
                "sample_rate_hz": 4000,
                "sample_count": 1,
                "values": [900.0],
            },
            "load_torque_nm": {
                "sample_rate_hz": 4000,
                "sample_count": 1,
                "values": [1.2],
            },
            "bearing_radial_load_n": {
                "sample_rate_hz": 4000,
                "sample_count": 1,
                "values": [1000.0],
            },
            "bearing_module_temperature_c": 46.3,
        }

        packet = build_sensor_packet(
            device_id="machine_01",
            task_id="task_00001",
            bearing_id="bearing_01",
            sender_id="sender_01",
            sequence_number=1,
            data=data,
            end_generate_timestamp_ns=123,
        )

        self.assertEqual(packet["device_id"], "machine_01")
        self.assertEqual(packet["bearing_id"], "bearing_01")
        self.assertEqual(packet["packet_id"], "task_00001_bearing_01_pkt_001")
        self.assertEqual(packet["data"]["vibration"]["sample_count"], 3)
        self.assertNotIn("ground_truth", packet)

    def test_packet_rejects_sample_count_mismatch(self) -> None:
        from sender.packet import PacketValidationError, build_sensor_packet

        data = {
            "vibration": {
                "sample_rate_hz": 64000,
                "sample_count": 2,
                "values": [0.1],
            }
        }

        with self.assertRaises(PacketValidationError):
            build_sensor_packet(
                device_id="machine_01",
                task_id="task_00001",
                bearing_id="bearing_01",
                sender_id="sender_01",
                sequence_number=1,
                data=data,
                end_generate_timestamp_ns=123,
            )

    def test_packet_rejects_empty_signal_arrays(self) -> None:
        from sender.packet import PacketValidationError, build_sensor_packet

        data = {
            name: {"sample_rate_hz": rate, "sample_count": 0, "values": []}
            for name, rate in {
                "vibration": 64000,
                "phase_current_1_A": 64000,
                "phase_current_2_A": 64000,
                "shaft_speed_rpm": 4000,
                "load_torque_nm": 4000,
                "bearing_radial_load_n": 4000,
            }.items()
        }
        data["bearing_module_temperature_c"] = 46.0

        with self.assertRaises(PacketValidationError):
            build_sensor_packet(
                device_id="machine_01",
                task_id="task_00001",
                bearing_id="bearing_01",
                sender_id="sender_01",
                sequence_number=80,
                data=data,
                end_generate_timestamp_ns=123,
            )

    def test_packet_rejects_invalid_bearing_id(self) -> None:
        from sender.packet import PacketValidationError, build_sensor_packet

        with self.assertRaises(PacketValidationError):
            build_sensor_packet(
                device_id="machine_01",
                task_id="task_00001",
                bearing_id="bearing-one",
                sender_id="sender_01",
                sequence_number=1,
                data={},
                end_generate_timestamp_ns=123,
            )

    def test_serialized_packet_is_utf8_json_without_nan(self) -> None:
        from sender.packet import PacketValidationError, serialize_packet

        with self.assertRaises(PacketValidationError):
            serialize_packet({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
