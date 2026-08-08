import json
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


def packet_data(index: int) -> dict:
    data = {
        name: {"sample_rate_hz": rate, "sample_count": 1, "values": [float(index)]}
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
            yield SignalWindow(index + 1, index * 0.05, (index + 1) * 0.05, packet_data(index))


class SchedulerHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        with type(self).lock:
            type(self).requests.append(request)
        response = {
            "device_id": request["device_id"],
            "sender_id": request["sender_id"],
            "task_id": request["task_id"],
            "bearing_id": request["bearing_id"],
            "target_topic": "edge/edge_1/input",
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class ThreeSenderIntegrationTests(unittest.TestCase):
    def test_three_real_mqtt_clients_publish_two_hundred_forty_packets(self) -> None:
        try:
            with socket.create_connection(("127.0.0.1", 1883), timeout=0.5):
                pass
        except OSError:
            self.skipTest("local Mosquitto is not running")

        from sender.config import load_config
        from sender.controller import run_all_senders

        SchedulerHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), SchedulerHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            module_root = Path(__file__).resolve().parents[1]
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = load_config(module_root / "config" / "local.json")
                scheduler_url = f"http://127.0.0.1:{server.server_port}/scheduler/decide"
                nodes = tuple(replace(node, scheduler_url=scheduler_url) for node in config.senders)
                config = replace(config, senders=nodes, log_dir=root / "logs", state_dir=root / "state")
                with patch("sender.controller.load_mat_record", return_value=FakeRecord()):
                    summaries = run_all_senders(
                        config,
                        {
                            "sender_01": Path("one.mat"),
                            "sender_02": Path("two.mat"),
                            "sender_03": Path("three.mat"),
                        },
                        realtime=False,
                    )
                packet_logs = [
                    json.loads(line)
                    for line in (root / "logs" / "packet_logs.jsonl").read_text(encoding="utf-8").splitlines()
                ]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(len(SchedulerHandler.requests), 3)
        self.assertEqual({item["sender_id"] for item in SchedulerHandler.requests}, {"sender_01", "sender_02", "sender_03"})
        self.assertEqual([item["confirmed_packet_count"] for item in summaries], [80, 80, 80])
        self.assertTrue(all(item["task_status"] == "completed" for item in summaries))
        self.assertEqual(len(packet_logs), 240)
        self.assertEqual(len({item["packet_id"] for item in packet_logs}), 240)
        self.assertEqual({item["sender_id"] for item in packet_logs}, {"sender_01", "sender_02", "sender_03"})


if __name__ == "__main__":
    unittest.main()
