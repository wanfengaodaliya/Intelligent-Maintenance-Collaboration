import json
import tempfile
import threading
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
        from sender.scheduler_client import ScheduleAssignment

        self.call_count += 1
        self.request = request
        return ScheduleAssignment(
            request["device_id"],
            request["sender_id"],
            request["task_id"],
            request["bearing_id"],
            "edge/edge_2/input",
            0,
        )


class FakePublisher:
    def __init__(self, **kwargs) -> None:
        self.published: list[tuple[dict, bytes, str]] = []
        self.reconnect_count = 0
        self.publish_retry_total = 0
        self.status_counts = {"confirmed": 80, "failed": 0, "dropped": 0}

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
    def _config(self, root: Path):
        from sender.config import load_config

        module_root = Path(__file__).resolve().parents[1]
        base_config = load_config(module_root / "config" / "local.json")
        return replace(base_config, log_dir=root / "logs", state_dir=root / "state")

    def test_one_sender_schedules_once_and_publishes_eighty_packets(self) -> None:
        from sender.controller import run_sender_task
        from sender.ids import TaskIdStore
        from sender.local_logs import LocalLogSink

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            node = config.senders[0]
            scheduler = FakeScheduler()
            publisher = FakePublisher()
            sink = LocalLogSink(config.log_dir)
            ids = TaskIdStore(config.state_dir / "sender_01_task_counter.txt", node.sender_id)

            with patch("sender.controller.load_mat_record", return_value=FakeRecord()):
                summary = run_sender_task(
                    config,
                    node,
                    Path("one.mat"),
                    realtime=False,
                    scheduler=scheduler,
                    publisher=publisher,
                    log_sink=sink,
                    task_ids=ids,
                )

            self.assertEqual(len(publisher.published), 80)
            self.assertEqual(publisher.published[0][0]["packet_id"], "sd_01_tk_0001_bearing_01_pkt_001")
            self.assertEqual(publisher.published[-1][0]["packet_id"], "sd_01_tk_0001_bearing_01_pkt_080")
            self.assertTrue(all(item[2] == "edge/edge_2/input" for item in publisher.published))
            self.assertEqual(scheduler.call_count, 1)
            self.assertEqual(scheduler.request["sender_id"], "sender_01")
            self.assertEqual(scheduler.request["bearing_id"], "bearing_01")
            self.assertEqual(scheduler.request["expected_packet_count"], 80)
            self.assertGreater(scheduler.request["packet_size_bytes"], 0)
            self.assertEqual(summary["expected_packet_count"], 80)
            self.assertEqual(summary["confirmed_packet_count"], 80)
            self.assertEqual(summary["task_status"], "completed")
            self.assertIsNone(summary["error_code"])
            saved = json.loads(sink.task_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(saved, summary)

    def test_mqtt_start_failure_is_written_to_task_log(self) -> None:
        from sender.controller import run_sender_task
        from sender.local_logs import LocalLogSink

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            node = config.senders[0]
            sink = LocalLogSink(config.log_dir)
            with patch("sender.controller.load_mat_record", return_value=FakeRecord()):
                with self.assertRaisesRegex(RuntimeError, "broker unavailable"):
                    run_sender_task(
                        config,
                        node,
                        Path("one.mat"),
                        realtime=False,
                        scheduler=FakeScheduler(),
                        publisher=StartFailingPublisher(),
                        log_sink=sink,
                    )
            saved = json.loads(sink.task_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(saved["sender_id"], "sender_01")
            self.assertEqual(saved["bearing_id"], "bearing_01")
            self.assertEqual(saved["task_status"], "failed")
            self.assertEqual(saved["error_code"], "MQTT_TASK_ERROR")

    def test_all_senders_start_concurrently_and_keep_separate_sources(self) -> None:
        from sender.controller import run_all_senders

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir))
            barrier = threading.Barrier(3)
            calls: list[tuple[str, str, Path]] = []
            lock = threading.Lock()

            def fake_runner(config, node, source_path, *, realtime, log_sink):
                with lock:
                    calls.append((node.sender_id, node.bearing_id, Path(source_path)))
                barrier.wait(timeout=2)
                return {"sender_id": node.sender_id, "task_status": "completed"}

            summaries = run_all_senders(
                config,
                {
                    "sender_01": Path("one.mat"),
                    "sender_02": Path("two.mat"),
                    "sender_03": Path("three.mat"),
                },
                realtime=False,
                runner=fake_runner,
            )

            self.assertEqual(
                sorted(calls),
                [
                    ("sender_01", "bearing_01", Path("one.mat")),
                    ("sender_02", "bearing_02", Path("two.mat")),
                    ("sender_03", "bearing_03", Path("three.mat")),
                ],
            )
            self.assertEqual([item["sender_id"] for item in summaries], ["sender_01", "sender_02", "sender_03"])

    def test_all_senders_requires_one_source_per_configured_sender(self) -> None:
        from sender.controller import run_all_senders

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "source files"):
                run_all_senders(config, {"sender_01": Path("one.mat")}, realtime=False)

    def test_one_sender_failure_does_not_hide_other_sender_results(self) -> None:
        from sender.controller import run_all_senders

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir))

            def sometimes_failing_runner(config, node, source_path, *, realtime, log_sink):
                if node.sender_id == "sender_02":
                    raise RuntimeError("sender_02 unavailable")
                return {"sender_id": node.sender_id, "task_status": "completed", "error_code": None}

            summaries = run_all_senders(
                config,
                {
                    "sender_01": Path("one.mat"),
                    "sender_02": Path("two.mat"),
                    "sender_03": Path("three.mat"),
                },
                realtime=False,
                runner=sometimes_failing_runner,
            )

            self.assertEqual([item["sender_id"] for item in summaries], ["sender_01", "sender_02", "sender_03"])
            self.assertEqual(summaries[0]["task_status"], "completed")
            self.assertEqual(summaries[1]["task_status"], "failed")
            self.assertEqual(summaries[1]["error_code"], "SENDER_TASK_EXCEPTION")
            self.assertIn("sender_02 unavailable", summaries[1]["error_message"])
            self.assertEqual(summaries[2]["task_status"], "completed")


if __name__ == "__main__":
    unittest.main()
