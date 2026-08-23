from __future__ import annotations

import json
import sqlite3

import pytest

from cloud_service.device_arbitration.service import DeviceArbitrationService
from scenarios.bearing.arbitration import BearingArbitrationPolicy


def _unit(
    unit_id: str,
    *,
    state: str = "normal",
    confidence: float = 0.9,
    quality: float = 0.9,
    risk: str = "low",
    action: str = "continue_operation",
) -> dict:
    return {
        "bearing_id": unit_id,
        "bearing_state": state,
        "confidence": confidence,
        "data_quality_score": quality,
        "risk_level": risk,
        "recommended_action": action,
    }


def _request(conflict_id: str, units: list[dict]) -> dict:
    return {
        "scenario_type": "bearing",
        "conflict_id": conflict_id,
        "subject_id": "machine-1",
        "task_id": "task-1",
        "scenario_payload": {"bearing_results": units},
    }


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (
            [
                _unit(
                    "bearing-a",
                    state="fault",
                    confidence=0.95,
                    risk="high",
                    action="urgent_intervention",
                ),
                _unit("bearing-b"),
            ],
            {
                "status": "resolved",
                "resolution_method": "scenario_rule",
                "final_action": "shutdown",
                "dominant_unit_id": "bearing-a",
                "triggered_rule_id": "HIGH_RISK_ABNORMAL",
            },
        ),
        (
            [_unit("bearing-a"), _unit("bearing-b", confidence=0.8)],
            {
                "status": "resolved",
                "resolution_method": "weighted_fusion",
                "final_action": "continue_operation",
                "dominant_unit_id": "bearing-a",
                "triggered_rule_id": None,
            },
        ),
        (
            [
                _unit("bearing-a", confidence=1.0, quality=1.0),
                _unit(
                    "bearing-b",
                    confidence=1.0,
                    quality=1.0,
                    action="enhanced_monitoring",
                ),
                _unit(
                    "bearing-c",
                    confidence=1.0,
                    quality=1.0,
                    action="scheduled_inspection",
                ),
            ],
            {
                "status": "manual_review",
                "resolution_method": "weighted_fusion",
                "final_action": None,
                "dominant_unit_id": None,
                "triggered_rule_id": None,
            },
        ),
    ],
)
def test_real_bearing_policy_preserves_rule_fusion_and_manual_review_paths(
    tmp_path,
    units,
    expected,
) -> None:
    request = _request(f"conflict-{len(units)}-{expected['status']}", units)

    result = DeviceArbitrationService(
        tmp_path / "cloud.db",
        BearingArbitrationPolicy(),
    ).arbitrate(request)

    for field, value in expected.items():
        if field == "triggered_rule_id":
            assert result["scenario_result"][field] == value
        else:
            assert result[field] == value


def test_duplicate_conflict_returns_the_exact_persisted_result_and_json(tmp_path) -> None:
    database_path = tmp_path / "cloud.db"
    request = _request(
        "conflict-idempotent",
        [_unit("bearing-a"), _unit("bearing-b", confidence=0.8)],
    )
    service = DeviceArbitrationService(database_path, BearingArbitrationPolicy())

    first = service.arbitrate(request)
    second = service.arbitrate(request)

    assert second == first
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT request_json, result_json FROM device_arbitration_record "
            "WHERE conflict_id=?",
            ("conflict-idempotent",),
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM device_arbitration_record WHERE conflict_id=?",
            ("conflict-idempotent",),
        ).fetchone()[0]
    assert count == 1
    assert row == (
        json.dumps(request, ensure_ascii=False, sort_keys=True),
        json.dumps(first, ensure_ascii=False, sort_keys=True),
    )
