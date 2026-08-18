from __future__ import annotations

from cloud_review.contracts import CloudReviewError, parse_cloud_bearing_result


def _payload() -> dict:
    return {
        "schema_version": "cloud-bearing-result/2.0",
        "result_id": "cloud_dw_01_v1",
        "review_id": "review_01",
        "device_id": "machine_01",
        "task_id": "task_001",
        "bearing_id": "bearing_02",
        "sender_id": "sender_02",
        "decision_round_id": "round_01",
        "diagnosis_window_id": "dw_01",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "window_start_ns": 0,
        "window_end_ns": 50_000_000,
        "bearing_state": "warning",
        "confidence": 0.90,
        "data_quality_score": 1.0,
        "risk_level": "medium",
        "action_grade": 2,
        "recommended_action": "scheduled_inspection",
        "model_version": "cloud_model_v1",
        "created_at_ns": 60,
    }


def test_cloud_bearing_result_requires_all_v12_identity_fields() -> None:
    result = parse_cloud_bearing_result(_payload())

    assert result.diagnosis_window_id == "dw_01"
    assert result.decision_round_id == "round_01"
    assert result.action_grade == 2


def test_cloud_bearing_result_rejects_action_grade_mismatch() -> None:
    payload = _payload()
    payload["action_grade"] = 3

    try:
        parse_cloud_bearing_result(payload)
    except CloudReviewError as error:
        assert error.code == "INVALID_CLOUD_BEARING_RESULT"
    else:
        raise AssertionError("mismatched action grade must be rejected")
