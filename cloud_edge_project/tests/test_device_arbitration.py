from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.arbitration_contracts import ArbitrationValidationError
from cloud_service.device_arbitration.fusion import calculate_fusion
from cloud_service.device_arbitration.service import DeviceArbitrationService
from cloud_service.storage.database import connect
from scenarios.bearing.cloud.device_arbitration.adapter import (
    BearingDeviceArbitrationAdapter,
)


def valid_arbitration_request() -> dict:
    return {
        "scenario_type": "bearing",
        "conflict_id": "conflict_machine_01_task_0001",
        "subject_id": "machine_01",
        "task_id": "task_0001",
        "scenario_payload": {
            "bearing_results": [
                {
                    "bearing_id": "bearing_01",
                    "bearing_state": "normal",
                    "confidence": 0.92,
                    "data_quality_score": 0.95,
                    "risk_level": "low",
                    "recommended_action": "continue_operation",
                },
                {
                    "bearing_id": "bearing_02",
                    "bearing_state": "abnormal",
                    "confidence": 0.93,
                    "data_quality_score": 0.96,
                    "risk_level": "high",
                    "recommended_action": "shutdown",
                    "rule_facts": ["SUSTAINED_ABNORMAL"],
                    "sender_id": "sender_02",
                    "packet_summary": {"packet_count": 4},
                    "summary": "任务内存在持续异常",
                },
            ]
        },
    }


def test_adapter_builds_decision_units_and_keeps_optional_fields() -> None:
    context = BearingDeviceArbitrationAdapter().build_context(
        valid_arbitration_request()
    )

    assert context.scenario_type == "bearing"
    assert context.subject_id == "machine_01"
    assert [unit.unit_id for unit in context.decision_units] == [
        "bearing_01",
        "bearing_02",
    ]
    assert context.decision_units[1].scenario_payload == {
        "rule_facts": ["SUSTAINED_ABNORMAL"],
        "sender_id": "sender_02",
        "packet_summary": {"packet_count": 4},
        "summary": "任务内存在持续异常",
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("confidence", 1.1, "INVALID_CONFIDENCE"),
        ("data_quality_score", -0.1, "INVALID_DATA_QUALITY"),
        ("recommended_action", "reduce_load", "INVALID_ACTION"),
        ("risk_level", "critical", "INVALID_RISK_LEVEL"),
    ],
)
def test_adapter_rejects_invalid_bearing_values(
    field: str, value: object, code: str
) -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][1][field] = value

    with pytest.raises(ArbitrationValidationError) as error:
        BearingDeviceArbitrationAdapter().build_context(request)

    assert error.value.code == code


def test_adapter_rejects_duplicate_bearing_id() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][1]["bearing_id"] = (
        "bearing_01"
    )

    with pytest.raises(ArbitrationValidationError) as error:
        BearingDeviceArbitrationAdapter().build_context(request)

    assert error.value.code == "DUPLICATE_BEARING"


def test_adapter_accepts_missing_optional_fields() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][1] = {
        key: value
        for key, value in request["scenario_payload"]["bearing_results"][1].items()
        if key
        not in {"rule_facts", "sender_id", "packet_summary", "summary"}
    }

    context = BearingDeviceArbitrationAdapter().build_context(request)

    assert context.decision_units[1].scenario_payload["rule_facts"] == []


@pytest.mark.parametrize(
    ("result", "rule_id"),
    [
        (
            {
                "bearing_state": "abnormal",
                "risk_level": "high",
                "confidence": 0.90,
            },
            "HIGH_RISK_ABNORMAL",
        ),
        (
            {
                "risk_level": "medium",
                "rule_facts": ["CONFIRMED_SEVERE_FAULT"],
            },
            "CONFIRMED_SEVERE_FAULT",
        ),
        (
            {
                "rule_facts": [
                    "SUSTAINED_ABNORMAL",
                    "CLOUD_REVIEW_CONFIRMED",
                ],
                "confidence": 0.90,
                "risk_level": "medium",
            },
            "SUSTAINED_CLOUD_CONFIRMED_ABNORMAL",
        ),
    ],
)
def test_bearing_safety_rules_shutdown(
    result: dict[str, object], rule_id: str
) -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][1].update(result)

    decision = BearingDeviceArbitrationAdapter().evaluate_rules(
        BearingDeviceArbitrationAdapter().build_context(request)
    )

    assert decision.triggered is True
    assert decision.rule_id == rule_id
    assert decision.final_action == "shutdown"
    assert decision.dominant_unit_id == "bearing_02"


def test_multiple_high_risk_bearings_trigger_shutdown() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][0].update(
        {
            "bearing_state": "normal",
            "confidence": 0.85,
            "risk_level": "high",
            "recommended_action": "enhanced_monitoring",
        }
    )
    request["scenario_payload"]["bearing_results"][1].update(
        {
            "bearing_state": "normal",
            "confidence": 0.85,
            "risk_level": "high",
            "recommended_action": "enhanced_monitoring",
            "rule_facts": [],
        }
    )

    decision = BearingDeviceArbitrationAdapter().evaluate_rules(
        BearingDeviceArbitrationAdapter().build_context(request)
    )

    assert decision.rule_id == "MULTIPLE_HIGH_RISK"
    assert decision.final_action == "shutdown"


def test_no_bearing_safety_rule_allows_fusion() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][1].update(
        {"risk_level": "medium", "rule_facts": []}
    )

    decision = BearingDeviceArbitrationAdapter().evaluate_rules(
        BearingDeviceArbitrationAdapter().build_context(request)
    )

    assert decision.triggered is False


def test_fusion_uses_confidence_times_quality_and_resolves_clear_winner() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][1].update(
        {
            "risk_level": "medium",
            "rule_facts": [],
            "confidence": 0.80,
            "data_quality_score": 0.50,
        }
    )
    adapter = BearingDeviceArbitrationAdapter()
    context = adapter.build_context(request)

    result = calculate_fusion(
        context.decision_units,
        action_severity=adapter.action_severity(),
        min_top_score=0.40,
        min_margin=0.05,
    )

    assert result["status"] == "resolved"
    assert result["final_action"] == "continue_operation"
    assert result["action_scores"]["continue_operation"] == pytest.approx(
        0.92 * 0.95 / (0.92 * 0.95 + 0.80 * 0.50)
    )


def test_fusion_returns_manual_review_for_insufficient_margin() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][0].update(
        {"confidence": 0.51, "data_quality_score": 1.0}
    )
    request["scenario_payload"]["bearing_results"][1].update(
        {
            "confidence": 0.49,
            "data_quality_score": 1.0,
            "risk_level": "medium",
            "rule_facts": [],
        }
    )
    adapter = BearingDeviceArbitrationAdapter()
    context = adapter.build_context(request)

    result = calculate_fusion(
        context.decision_units,
        action_severity=adapter.action_severity(),
        min_top_score=0.40,
        min_margin=0.05,
    )

    assert result["status"] == "manual_review"
    assert result["final_action"] is None


def test_fusion_uses_more_severe_action_for_exact_tie() -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][0].update(
        {"confidence": 0.5, "data_quality_score": 1.0}
    )
    request["scenario_payload"]["bearing_results"][1].update(
        {
            "confidence": 0.5,
            "data_quality_score": 1.0,
            "risk_level": "medium",
            "rule_facts": [],
        }
    )
    adapter = BearingDeviceArbitrationAdapter()
    context = adapter.build_context(request)

    result = calculate_fusion(
        context.decision_units,
        action_severity=adapter.action_severity(),
        min_top_score=0.40,
        min_margin=0.05,
    )

    assert result["status"] == "resolved"
    assert result["final_action"] == "shutdown"


def test_service_persists_rule_result_and_reuses_conflict_id(
    tmp_path: Path,
) -> None:
    service = DeviceArbitrationService(
        tmp_path / "cloud.db", BearingDeviceArbitrationAdapter()
    )
    first = service.arbitrate(valid_arbitration_request())
    changed_request = valid_arbitration_request()
    changed_request["scenario_payload"]["bearing_results"][1][
        "recommended_action"
    ] = "continue_operation"

    second = service.arbitrate(changed_request)

    assert first["status"] == "resolved"
    assert first["resolution_method"] == "scenario_rule"
    assert first["final_state"] == "abnormal"
    assert first["final_action"] == "shutdown"
    assert second == first
    assert service.get(first["conflict_id"]) == first
    with connect(tmp_path / "cloud.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM device_arbitration_record"
        ).fetchone()[0]
    assert count == 1


def test_service_returns_manual_review_when_fusion_is_ambiguous(
    tmp_path: Path,
) -> None:
    request = valid_arbitration_request()
    request["scenario_payload"]["bearing_results"][0].update(
        {"confidence": 0.51, "data_quality_score": 1.0}
    )
    request["scenario_payload"]["bearing_results"][1].update(
        {
            "confidence": 0.49,
            "data_quality_score": 1.0,
            "risk_level": "medium",
            "rule_facts": [],
        }
    )

    result = DeviceArbitrationService(
        tmp_path / "cloud.db", BearingDeviceArbitrationAdapter()
    ).arbitrate(request)

    assert result["status"] == "manual_review"
    assert result["final_state"] == "unknown"
    assert result["final_action"] is None
    assert result["resolution_method"] == "weighted_fusion"
