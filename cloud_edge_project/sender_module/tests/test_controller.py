import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


def packet_data() -> dict:
    data = {
        name: {"sample_rate_hz": rate, "sample_count": 1, "values": [float(rate)]}
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
    return data


class FakeRecord:
    def windows(self, *, duration_ms: int, count: int):
        from sender.mat_reader import SignalWindow

        for index in range(count):
            yield SignalWindow(index + 1, index * 0.05, (index + 1) * 0.05, packet_data())


class FakeScheduler:
    def __init__(self) -> None:
        self.request = None
        self.call_count = 0

    def assign(self, request: dict):
        from sender.scheduler_client import BearingAssignment, ScheduleAssignment

        self.call_count += 1
        self.request = request
        assignments = tuple(
            BearingAssignment(
                bearing["bearing_id"],
                "edge/edge_2/input" if bearing["bearing_id"] == "bearing_02" else "edge/edge_1/input",
            )
            for bearing in request["bearings"]
        )
        return ScheduleAssignment(request["device_id"], request["task_id"], assignments, 0)


class FakePublisher:
    def __init__(self, **kwargs) -> None:
        self.published: list[tuple[dict, bytes, str]] = []
        self.reconnect_count = 0
        self.publish_retry_total = 0
        self.status_counts = {"confirmed": 240, "failed": 0, "dropped": 0}

    def start(self) -> None:
        return

    def publish(self, packet: dict, payload: bytes, topic: str) -> None:
        self.published.append((packet, payload, topic))

    def wait_until_settled(self, timeout_seconds: float) -> bool:
        return True

    def stop(self) -> None:
        return


class StartFailingPublisher(FakePublisher):
    def start(self) -> None:
        raise RuntimeError("broker unavailable")


class ControllerTests(unittest.TestCase):
    def test_mqtt_start_failure_is_written_to_task_log(self) -> None:
        from sender.config import load_config
        from sender.controller import run_task
        from sender.ids import TaskIdStore
        from sender.local_logs import LocalLogSink

        module_root = Path(__file__).resolve().parents[1]
        base_config = load_config(module_root / "config" / "local.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(base_config, log_dir=root / "logs", state_dir=root / "state")
            sink = LocalLogSink(config.log_dir)

            with patch("sender.controller.load_mat_record", return_value=FakeRecord()):
                with self.assertRaisesRegex(RuntimeError, "broker unavailable"):
                    run_task(
                        config,
                        "machine_01",
                        {
                            "bearing_01": Path("one.mat"),
                            "bearing_02": Path("two.mat"),
                            "bearing_03": Path("three.mat"),
                        },
                        realtime=False,
                        scheduler=FakeScheduler(),
                        publisher=StartFailingPublisher(),
                        log_sink=sink,
                        task_ids=TaskIdStore(config.state_dir / "task_counter.txt"),
                    )

            saved = json.loads(sink.task_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(saved["task_status"], "failed")
            self.assertEqual(saved["error_code"], "MQTT_TASK_ERROR")

    def test_run_task_schedules_once_and_publishes_three_bearing_streams(self) -> None:
        from sender.config import load_config
        from sender.controller import run_task
        from sender.ids import TaskIdStore
        from sender.local_logs import LocalLogSink

        module_root = Path(__file__).resolve().parents[1]
        base_config = load_config(module_root / "config" / "local.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(base_config, log_dir=root / "logs", state_dir=root / "state")
            scheduler = FakeScheduler()
            publisher = FakePublisher()
            sink = LocalLogSink(config.log_dir)

            with patch("sender.controller.load_mat_record", return_value=FakeRecord()):
                summary = run_task(
                    config,
                    "machine_01",
                    {
                        "bearing_01": Path("one.mat"),
                        "bearing_02": Path("two.mat"),
                        "bearing_03": Path("three.mat"),
                    },
                    realtime=False,
                    scheduler=scheduler,
                    publisher=publisher,
                    log_sink=sink,
                    task_ids=TaskIdStore(config.state_dir / "task_counter.txt"),
                )

            self.assertEqual(len(publisher.published), 240)
            self.assertEqual(len({item[0]["packet_id"] for item in publisher.published}), 240)
            self.assertEqual(
                [item[0]["bearing_id"] for item in publisher.published[:3]],
                ["bearing_01", "bearing_02", "bearing_03"],
            )
            self.assertEqual(
                [item[2] for item in publisher.published[:3]],
                ["edge/edge_1/input", "edge/edge_2/input", "edge/edge_1/input"],
            )
            self.assertEqual(
                publisher.published[0][0]["packet_id"],
                "task_00001_bearing_01_pkt_001",
            )
            self.assertEqual(
                publisher.published[-1][0]["packet_id"],
                "task_00001_bearing_03_pkt_080",
            )
            self.assertEqual(scheduler.call_count, 1)
            self.assertEqual(scheduler.request["device_id"], "machine_01")
            self.assertEqual(scheduler.request["expected_duration_ms"], 4000)
            self.assertNotIn("packet_size_bytes", scheduler.request)
            self.assertEqual(len(scheduler.request["bearings"]), 3)
            self.assertTrue(
                all(item["packet_size_bytes"] > 0 for item in scheduler.request["bearings"])
            )
            self.assertEqual(summary["task_status"], "completed")
            self.assertEqual(summary["replay_mode"], "accelerated")
            self.assertEqual(summary["expected_packet_total"], 240)

            saved = json.loads(sink.task_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(saved, summary)

    def test_run_task_requires_exactly_three_bearings(self) -> None:
        from sender.config import load_config
        from sender.controller import run_task

        module_root = Path(__file__).resolve().parents[1]
        config = load_config(module_root / "config" / "local.json")
        with self.assertRaisesRegex(ValueError, "exactly three bearings"):
            run_task(
                config,
                "machine_01",
                {"bearing_01": Path("one.mat"), "bearing_02": Path("two.mat")},
                realtime=False,
            )


if __name__ == "__main__":
    unittest.main()
