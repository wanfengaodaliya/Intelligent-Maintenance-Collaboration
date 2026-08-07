import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class SequenceSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        response = self.responses[self.calls]
        self.calls += 1
        return response


def schedule_request() -> dict:
    return {
        "device_id": "machine_01",
        "task_id": "task_00001",
        "sender_id": "sender_01",
        "bearings": [
            {"bearing_id": "bearing_01", "packet_size_bytes": 50000},
            {"bearing_id": "bearing_02", "packet_size_bytes": 50000},
            {"bearing_id": "bearing_03", "packet_size_bytes": 50000},
        ],
        "expected_duration_ms": 4000,
        "created_timestamp_ns": 123,
    }


def assignment_payload(*, task_id: str = "task_00001") -> dict:
    return {
        "device_id": "machine_01",
        "task_id": task_id,
        "assignments": [
            {"bearing_id": "bearing_01", "target_topic": "edge/edge_1/input"},
            {"bearing_id": "bearing_02", "target_topic": "edge/edge_2/input"},
            {"bearing_id": "bearing_03", "target_topic": "edge/edge_1/input"},
        ],
    }


class _SchedulerHandler(BaseHTTPRequestHandler):
    calls = 0
    requests: list[dict] = []

    def do_POST(self) -> None:
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(payload)

        if type(self).calls == 1:
            self.send_response(503)
            self.end_headers()
            return

        response = assignment_payload(task_id=payload["task_id"])
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class SchedulerClientTests(unittest.TestCase):
    def test_assignment_retries_mismatched_success_response(self) -> None:
        from sender.scheduler_client import SchedulerClient

        request = schedule_request()
        session = SequenceSession(
            [
                FakeResponse(assignment_payload(task_id="task_99999")),
                FakeResponse(assignment_payload()),
            ]
        )
        client = SchedulerClient(
            url="http://scheduler.test/scheduler/decide",
            timeout_seconds=1,
            max_retries=2,
            retry_delay_seconds=0,
            session=session,
        )

        assignment = client.assign(request)

        self.assertEqual(session.calls, 2)
        self.assertEqual(assignment.schedule_retry_count, 1)
        self.assertEqual(assignment.topic_for("bearing_02"), "edge/edge_2/input")

    def setUp(self) -> None:
        _SchedulerHandler.calls = 0
        _SchedulerHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SchedulerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_assignment_retries_and_validates_correlated_response(self) -> None:
        from sender.scheduler_client import SchedulerClient

        request = schedule_request()
        client = SchedulerClient(
            url=f"http://127.0.0.1:{self.server.server_port}/scheduler/decide",
            timeout_seconds=1,
            max_retries=2,
            retry_delay_seconds=0,
        )

        assignment = client.assign(request)

        self.assertEqual(assignment.device_id, "machine_01")
        self.assertEqual(assignment.topic_for("bearing_02"), "edge/edge_2/input")
        self.assertEqual(assignment.schedule_retry_count, 1)
        self.assertEqual(_SchedulerHandler.calls, 2)
        self.assertEqual(_SchedulerHandler.requests[0], request)

    def test_assignment_rejects_response_for_another_task(self) -> None:
        from sender.scheduler_client import SchedulerError, validate_assignment

        with self.assertRaises(SchedulerError):
            validate_assignment(
                assignment_payload(task_id="task_99999"),
                expected_device_id="machine_01",
                expected_task_id="task_00001",
                expected_bearing_ids={"bearing_01", "bearing_02", "bearing_03"},
            )

    def test_assignment_rejects_missing_bearing(self) -> None:
        from sender.scheduler_client import SchedulerError, validate_assignment

        payload = assignment_payload()
        payload["assignments"].pop()
        with self.assertRaises(SchedulerError):
            validate_assignment(
                payload,
                expected_device_id="machine_01",
                expected_task_id="task_00001",
                expected_bearing_ids={"bearing_01", "bearing_02", "bearing_03"},
            )

    def test_assignment_rejects_duplicate_bearing(self) -> None:
        from sender.scheduler_client import SchedulerError, validate_assignment

        payload = assignment_payload()
        payload["assignments"][2]["bearing_id"] = "bearing_02"
        with self.assertRaises(SchedulerError):
            validate_assignment(
                payload,
                expected_device_id="machine_01",
                expected_task_id="task_00001",
                expected_bearing_ids={"bearing_01", "bearing_02", "bearing_03"},
            )


class LocalLogTests(unittest.TestCase):
    def test_packet_and_task_records_are_valid_jsonl(self) -> None:
        from sender.local_logs import LocalLogSink

        with tempfile.TemporaryDirectory() as temp_dir:
            sink = LocalLogSink(Path(temp_dir))
            packet = {
                "task_id": "task_00001",
                "packet_id": "task_00001_bearing_01_pkt_001",
                "publish_status": "confirmed",
            }
            task = {"task_id": "task_00001", "task_status": "completed"}

            sink.write_packet(packet)
            sink.write_task(task)

            packet_lines = sink.packet_path.read_text(encoding="utf-8").splitlines()
            task_lines = sink.task_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(packet_lines[0]), packet)
            self.assertEqual(json.loads(task_lines[0]), task)


if __name__ == "__main__":
    unittest.main()
