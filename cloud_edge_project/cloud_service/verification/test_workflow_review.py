from __future__ import annotations

from cloud_service.workflow_review import WorkflowReviewService
from scenarios.bearing.cloud.workflow_review_scenario import (
    BearingWorkflowReviewScenario,
)


def _packet_result(sequence=1):
    return {
        "result_id": "packet-result-%d" % sequence,
        "device_id": "device-1",
        "task_id": "task-1",
        "bearing_id": "bearing-1",
        "sender_id": "sender-1",
        "packet_id": "packet-%d" % sequence,
        "sequence_number": sequence,
        "action_grade": 0,
        "confidence": 0.5,
        "data_quality_score": 1.0,
        "risk_level": "low",
        "decision_source": "FINAL_EDGE",
        "raw_data_ref": "edge-cache://sender-1/task-1/%d" % sequence,
        "valid": True,
    }


def _raw(sequence):
    return {
        "device_id": "device-1",
        "task_id": "task-1",
        "bearing_id": "bearing-1",
        "sender_id": "sender-1",
        "packet_id": "packet-%d" % sequence,
        "sequence_number": sequence,
        "data": {"vibration": {"values": [0.2, -0.2, 0.2, -0.2]}},
    }


def test_packet_review_job_is_idempotent_and_queryable(tmp_path):
    service = WorkflowReviewService(
        tmp_path / "cloud.db", scenario_reviewer=BearingWorkflowReviewScenario()
    )
    request = {
        "review_id": "packet-review-1",
        "device_id": "device-1",
        "task_id": "task-1",
        "bearing_id": "bearing-1",
        "packet_result": _packet_result(),
        "raw_packet": _raw(1),
    }
    first = service.submit("PACKET", request)
    second = service.submit("PACKET", request)
    assert first["review_id"] == second["review_id"]
    service.process(first["review_id"])
    completed = service.get(first["review_id"])
    assert completed["status"] == "SUCCEEDED"
    assert completed["result"]["decision_source"] == "FINAL_CLOUD"


def test_window_review_requires_exact_ordered_twenty_packet_batch(tmp_path):
    service = WorkflowReviewService(
        tmp_path / "cloud.db", scenario_reviewer=BearingWorkflowReviewScenario()
    )
    window = {
        "result_id": "window-1",
        "device_id": "device-1",
        "task_id": "task-1",
        "bearing_id": "bearing-1",
        "sender_id": "sender-1",
        "window_index": 1,
        "sequence_start": 1,
        "sequence_end": 20,
        "packet_count": 20,
        "valid_packet_count": 20,
        "action_grade": 2,
        "confidence": 0.7,
        "data_quality_score": 0.9,
        "result_source": "FINAL_EDGE",
        "review_status": "PENDING",
        "review_required": True,
        "review_reasons": ["ACTION_GRADE_CONFLICT"],
        "packet_result_ids": [],
        "raw_data_refs": [],
    }
    service.submit("BEARING_WINDOW", {
        "review_id": "window-review-1",
        "device_id": "device-1",
        "task_id": "task-1",
        "bearing_id": "bearing-1",
        "window_result": window,
    })
    service.upload_window_raw("window-review-1", {"raw_packets": [_raw(i) for i in range(1, 21)]})
    service.process("window-review-1")
    completed = service.get("window-review-1")
    assert completed["status"] == "SUCCEEDED"
    assert completed["result"]["reviewed_raw_packet_count"] == 20


def test_device_review_reuses_existing_arbitration_and_returns_safe_action(tmp_path):
    service = WorkflowReviewService(
        tmp_path / "cloud.db", scenario_reviewer=BearingWorkflowReviewScenario()
    )
    bearings = []
    for bearing_id, grade in (("bearing-1", 0), ("bearing-2", 2)):
        bearings.append({
            "result_id": "bearing-result-" + bearing_id,
            "device_id": "device-1",
            "task_id": "task-1",
            "bearing_id": bearing_id,
            "sender_id": "sender-" + bearing_id[-1],
            "window_results": [],
            "latest_action_grade": grade,
            "max_action_grade": grade,
            "persistent_action_grade": grade,
            "recommended_action_grade": grade,
            "confidence": 0.9,
            "data_quality_score": 1.0,
            "trend": "STABLE",
            "rule_facts": [],
        })
    device = {
        "result_id": "device-result-1",
        "device_id": "device-1",
        "task_id": "task-1",
        "expected_bearing_ids": ["bearing-1", "bearing-2"],
        "bearing_results": bearings,
        "status": "REVIEW_REQUIRED",
        "action_grade": 2,
        "conflict": True,
        "conflict_reasons": ["DEVICE_ACTION_GRADE_CONFLICT"],
        "decision_source": "PENDING_CLOUD",
        "final_report": None,
    }
    service.submit("DEVICE", {
        "review_id": "device-review-1",
        "device_id": "device-1",
        "task_id": "task-1",
        "device_result": device,
    })
    service.process("device-review-1")
    completed = service.get("device-review-1")
    assert completed["status"] == "SUCCEEDED"
    assert completed["result"]["action_grade"] == 2
    assert completed["result"]["final_report"]["report_status"] == "STRUCTURED_RESULT_READY_FOR_LLM"
