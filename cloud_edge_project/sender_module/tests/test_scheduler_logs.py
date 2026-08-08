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
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "packet_size_bytes": 102400,
        "expected_packet_count": 80,
        "expected_duration_ms": 4000,
        "created_timestamp_ns": 123,
    }


def assignment_payload(**changes) -> dict:
    payload = {
        "device_id": "machine_01",
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "target_topic": "edge/edge_2/input",
    }
    payload.update(changes)
    return payload


class _SchedulerHandler(BaseHTTPRequestHandler):
    calls = 0
    requests: list[dict] = []

    def do_POST(self) -> None:
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(request)
        if type(self).calls == 1:
            self.send_response(503)
            self.end_headers()
            return
        response = assignment_payload(
            device_id=request["device_id"],
            sender_id=request["sender_id"],
            task_id=request["task_id"],
            bearing_id=request["bearing_id"],
        )
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class SchedulerClientTests(unittest.TestCase):
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

    def test_assignment_retries_and_returns_one_target_topic(self) -> None:
        from sender.scheduler_client import SchedulerClient

        request = schedule_request()
        client = SchedulerClient(
            url=f"http://127.0.0.1:{self.server.server_port}/scheduler/decide",
            timeout_seconds=1,
            max_retries=2,
            retry_delay_seconds=0,
        )

        assignment = client.assign(request)

        self.assertEqual(assignment.sender_id, "sender_01")
        self.assertEqual(assignment.bearing_id, "bearing_01")
        self.assertEqual(assignment.target_topic, "edge/edge_2/input")
        self.assertEqual(assignment.schedule_retry_count, 1)
        self.assertEqual(_SchedulerHandler.requests[0], request)

    def test_assignment_retries_mismatched_sender_response(self) -> None:
        from sender.scheduler_client import SchedulerClient

        session = SequenceSession(
            [
                FakeResponse(assignment_payload(sender_id="sender_99")),
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

        assignment = client.assign(schedule_request())

        self.assertEqual(session.calls, 2)
        self.assertEqual(assignment.schedule_retry_count, 1)

    def test_assignment_rejects_another_bearing(self) -> None:
        from sender.scheduler_client import SchedulerError, validate_assignment

        with self.assertRaisesRegex(SchedulerError, "bearing_id"):
            validate_assignment(
                assignment_payload(bearing_id="bearing_02"),
                expected_device_id="machine_01",
                expected_sender_id="sender_01",
                expected_task_id="sd_01_tk_0001",
                expected_bearing_id="bearing_01",
            )


class LocalLogTests(unittest.TestCase):
    def test_packet_and_task_records_are_valid_jsonl(self) -> None:
        from sender.local_logs import LocalLogSink

        with tempfile.TemporaryDirectory() as temp_dir:
            sink = LocalLogSink(Path(temp_dir))
            packet = {
                "sender_id": "sender_01",
                "task_id": "sd_01_tk_0001",
                "packet_id": "sd_01_tk_0001_bearing_01_pkt_001",
                "publish_status": "confirmed",
            }
            task = {"sender_id": "sender_01", "task_id": "sd_01_tk_0001", "task_status": "completed"}
            sink.write_packet(packet)
            sink.write_task(task)
            self.assertEqual(json.loads(sink.packet_path.read_text(encoding="utf-8").splitlines()[0]), packet)
            self.assertEqual(json.loads(sink.task_path.read_text(encoding="utf-8").splitlines()[0]), task)


if __name__ == "__main__":
    unittest.main()
