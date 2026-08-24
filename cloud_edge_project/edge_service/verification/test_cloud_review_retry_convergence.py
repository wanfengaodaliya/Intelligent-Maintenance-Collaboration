from __future__ import annotations

from cloud_review.service import CloudReviewService


class _Store:
    def __init__(self, control: dict) -> None:
        self.decision = {
            "control": control,
            "phase": "CLOUD_RETRY_WAIT",
            "review_id": None,
            "response": None,
            "attempt_count": 1,
            "next_retry_at_ns": 0,
        }
        self.released = False

    def list_decisions(self, *, phase: str | None = None):
        if phase is None or self.decision["phase"] == phase:
            return (dict(self.decision),)
        return ()

    def get_decision(self, _decision_id: str):
        return dict(self.decision)

    def get(self, _task_id: str, _bearing_id: str, _packet_id: str):
        return {
            "edge_perception_result": {"packet_id": "packet_01"},
            "raw_packet": {
                "sender_id": "sender_01",
                "window_start_ns": 1,
                "window_end_ns": 2,
                "contributing_packet_ids": ["packet_01"],
                "sample_rate_hz": 64_000,
                "sample_count": 3_200,
                "data": {"vibration": {"sample_rate_hz": 64_000, "sample_count": 3_200}},
            },
        }

    def save_decision(self, control: dict, **fields):
        self.decision = {"control": control, **fields}
        return dict(self.decision)

    def release(self, _task_id: str, _bearing_id: str, _packet_id: str) -> bool:
        self.released = True
        return True


class _Cloud:
    def __init__(self, result: dict | None = None) -> None:
        self.calls = 0
        self.result = {} if result is None else result

    def infer(self, _cloud_node_id: str, _endpoint: str, _payload: dict):
        self.calls += 1
        # HTTP transport succeeded, but the result body violates the V1.2
        # bearing-result contract.  This used to remain retryable forever.
        return {
            "success": True,
            "review_id": "review_01",
            "cloud_packet_result": self.result,
        }


class _Scheduler:
    def __init__(self) -> None:
        self.reports: list[dict] = []

    def report(self, payload: dict):
        self.reports.append(dict(payload))
        return {"accepted": True}


def _control() -> dict:
    return {
        "decision_id": "decision_01",
        "cloud_task_id": "cloud_task_01",
        "device_id": "machine_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "packet_id": "packet_01",
        "decision_round_id": "round_01",
        "diagnosis_window_id": "window_01",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "trigger_reasons": ["EDGE_LOW_CONFIDENCE"],
        "source": {
            "holder_id": "edge_02",
            "raw_data_ref": "edge-cache://edge_02/task_01/bearing_01/packet_01",
            "context_ref": None,
        },
        "target": {"cloud_node_id": "cloud_01", "endpoint": "/cloud/infer"},
        "created_at_ns": 1,
    }


def _cloud_result() -> dict:
    return {
        "schema_version": "cloud-bearing-result/2.0",
        "result_id": "cloud_window_01",
        "review_id": "review_01",
        "device_id": "machine_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "decision_round_id": "round_01",
        "diagnosis_window_id": "window_01",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "window_start_ns": 1,
        "window_end_ns": 2,
        "bearing_state": "normal",
        "confidence": 0.9,
        "data_quality_score": 1.0,
        "risk_level": "low",
        "action_grade": 0,
        "recommended_action": "continue_operation",
        "model_version": "cloud-v1",
        "created_at_ns": 10,
    }


def test_invalid_200_cloud_result_leaves_retry_queue_after_one_attempt() -> None:
    control = _control()
    store = _Store(control)
    cloud = _Cloud()
    scheduler = _Scheduler()
    service = CloudReviewService(
        store,
        cloud_client=cloud,
        scheduler_reporter=scheduler,
        edge_node_id="edge_02",
        max_retry_attempts=3,
        clock_ns=lambda: 10,
    )

    assert service.retry_due(now_ns=10) == 1
    assert service.retry_due(now_ns=20) == 0

    assert cloud.calls == 1
    assert store.decision["phase"] == "COMPLETED"
    assert store.decision["attempt_count"] == 2
    assert store.decision["response"]["upload_status"] == "PERMANENT_FAILED"
    assert store.decision["response"]["reason_code"] == "INVALID_CLOUD_BEARING_RESULT"
    assert scheduler.reports[-1]["upload_status"] == "PERMANENT_FAILED"
    assert store.released is True


def test_rejected_200_cloud_result_does_not_repeat_the_upload() -> None:
    class RejectingLifecycle:
        def apply_cloud_result(self, _result, *, accepted_at_ns: int):
            raise ValueError("cloud result cannot replace a final bearing decision")

    control = _control()
    store = _Store(control)
    cloud = _Cloud(_cloud_result())
    scheduler = _Scheduler()
    service = CloudReviewService(
        store,
        cloud_client=cloud,
        scheduler_reporter=scheduler,
        edge_node_id="edge_02",
        cloud_result_handler=RejectingLifecycle(),
        max_retry_attempts=3,
        clock_ns=lambda: 10,
    )

    assert service.retry_due(now_ns=10) == 1
    assert service.retry_due(now_ns=20) == 0

    assert cloud.calls == 1
    assert store.decision["phase"] == "COMPLETED"
    assert store.decision["response"]["reason_code"] == "CLOUD_RESULT_REJECTED"


def test_unexpected_200_processing_failure_consumes_the_retry_budget() -> None:
    class BrokenLifecycle:
        def apply_cloud_result(self, _result, *, accepted_at_ns: int):
            raise RuntimeError("local persistence unavailable")

    control = _control()
    store = _Store(control)
    cloud = _Cloud(_cloud_result())
    scheduler = _Scheduler()
    service = CloudReviewService(
        store,
        cloud_client=cloud,
        scheduler_reporter=scheduler,
        edge_node_id="edge_02",
        cloud_result_handler=BrokenLifecycle(),
        max_retry_attempts=3,
        retry_backoff_ns=1,
        clock_ns=lambda: 10,
    )

    assert service.retry_due(now_ns=10) == 1
    assert store.decision["phase"] == "CLOUD_RETRY_WAIT"
    assert store.decision["attempt_count"] == 2
    assert service.retry_due(now_ns=20) == 1
    assert service.retry_due(now_ns=30) == 0

    assert cloud.calls == 2
    assert store.decision["phase"] == "COMPLETED"
    assert store.decision["attempt_count"] == 3
    assert store.decision["response"]["upload_status"] == "RETRYABLE_FAILED"
    assert store.decision["response"]["reason_code"] == "CLOUD_RESULT_PROCESSING_FAILED"
