"""Compatibility tests for the schema / workflow-contract migration.

These tests pin down the *public* contract surface that must stay identical
after bearing-specific contracts/validation are moved to ``scenarios/bearing``:

1. ``common.schemas`` still exports the original bearing constants and
   validators (as a compatibility shim), with unchanged names and values.
2. Error codes and error messages produced by those validators are unchanged.
3. The JSON message structures accepted/returned are unchanged.
4. ``core.bearing_workflow_contracts`` and ``core.bearing_actions`` keep the
   old import paths working with the same validation error messages.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from common.schemas import (
    ContractError,
    DATA_TYPE,
    DURATION_MS,
    SAMPLE_COUNT,
    SAMPLE_RATE_HZ,
    canonical_summary_sha256,
    compact_packet_for_scheduler,
    error_response,
    validate_edge_feature_summary,
    validate_edge_feature_summary_batch,
    validate_sensor_packet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_sensor_packet() -> dict:
    return {
        "packet_id": "packet_01",
        "device_id": "device_01",
        "sensor_id": "sensor_01",
        "sequence_number": 1,
        "start_timestamp_ns": 1_000,
        "end_timestamp_ns": 1_000 + 50_000_000,
        "duration_ms": 50,
        "data": {
            "data_type": "bearing_timeseries",
            "vibration_sample_rate_hz": 16000,
            "vibration_sample_count": 800,
            "vibration": [0.0] * 800,
            "current": 1.24,
            "temperature": 43.2,
            "speed": 899.7,
            "load": 0.54,
        },
    }


def _channel_stats() -> dict:
    return {
        "mean": 1.0,
        "last": 1.0,
        "minimum": 1.0,
        "maximum": 1.0,
        "standard_deviation": 0.0,
    }


def _valid_edge_feature_summary() -> dict:
    return {
        "summary_id": "summary_01",
        "device_id": "device_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "packet_id": "packet_01",
        "sender_id": "sender_01",
        "edge_node_id": "edge_01",
        "sequence_number": 1,
        "end_timestamp_ns": 1000,
        "processing_status": "perception_completed",
        "summary_generated_at_ns": 2000,
        "perception_quality": {"status": "good", "flags": []},
        "features": {
            "vibration": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "unit": "mm/s",
                "rms": 1.0,
                "absolute_peak": 2.0,
                "kurtosis": 3.0,
                "dominant_frequency_hz": 100.0,
                "band_power_ratio_500_2000": 0.1,
                "spectral_entropy": 0.2,
            },
            "phase_current_1": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "unit": "A",
                "rms_a": 1.0,
                "absolute_peak_a": 2.0,
            },
            "phase_current_2": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "unit": "A",
                "rms_a": 1.0,
                "absolute_peak_a": 2.0,
            },
            "current_relationship": {"current_imbalance_ratio": 0.1},
            "operating_context": {
                "shaft_speed_rpm": _channel_stats(),
                "load_torque_nm": _channel_stats(),
                "bearing_radial_load_n": _channel_stats(),
                "bearing_module_temperature_c": 25.0,
            },
        },
        "edge_inference": {
            "edge_result": "normal",
            "edge_risk_level": "low",
            "confidence": 0.9,
        },
        "edge_model_version": "bearing_packet_model_v1",
    }


# ---------------------------------------------------------------------------
# 1. common.schemas 的轴承专有常量保持不变
# ---------------------------------------------------------------------------

def test_schema_bearing_constants_unchanged() -> None:
    assert DATA_TYPE == "bearing_timeseries"
    assert SAMPLE_RATE_HZ == 16000
    assert SAMPLE_COUNT == 800
    assert DURATION_MS == 50


def test_schema_public_validators_still_exported() -> None:
    # The migrated functions must still be reachable from common.schemas.
    import common.schemas as schemas
    for name in (
        "validate_sensor_packet",
        "validate_edge_feature_summary",
        "validate_edge_feature_summary_batch",
        "validate_edge_feature_summary_envelope",
        "compact_packet_for_scheduler",
        "canonical_summary_sha256",
    ):
        assert hasattr(schemas, name), f"common.schemas missing {name}"


# ---------------------------------------------------------------------------
# 2. validate_sensor_packet 错误码 / 错误消息保持不变
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mutate", "code", "message"),
    [
        (
            lambda p: p["data"].update({"data_type": "current_timeseries"}),
            "INVALID_PACKET",
            "data.data_type must be bearing_timeseries",
        ),
        (
            lambda p: p["data"].update({"vibration_sample_rate_hz": 8000}),
            "INVALID_PACKET",
            "data.vibration_sample_rate_hz must be 16000",
        ),
        (
            lambda p: p["data"].update({"vibration_sample_count": 400}),
            "INVALID_PACKET",
            "data.vibration_sample_count must be 800",
        ),
        (
            lambda p: p["data"].update({"vibration": 3.14}),
            "INVALID_PACKET",
            "data.vibration must be an array",
        ),
        (
            lambda p: p["data"].update({"vibration": [0.0] * 7}),
            "INVALID_PACKET",
            "vibration length does not match vibration_sample_count",
        ),
        (
            lambda p: p["data"].update({"load": 1.5}),
            "INVALID_PACKET",
            "data.load must be between 0 and 1",
        ),
    ],
)
def test_validate_sensor_packet_error_contract(mutate, code: str, message: str) -> None:
    packet = _valid_sensor_packet()
    mutate(packet)
    with pytest.raises(ContractError) as excinfo:
        validate_sensor_packet(packet)
    assert excinfo.value.code == code
    assert excinfo.value.message == message
    assert excinfo.value.packet_id == "packet_01"


def test_validate_sensor_packet_accepts_valid_packet() -> None:
    packet = _valid_sensor_packet()
    result = validate_sensor_packet(packet)
    assert result == packet


def test_validate_sensor_packet_accepts_documented_example() -> None:
    example_path = (
        Path(__file__).resolve().parents[2] / "examples" / "sensor_packet_normal.json"
    )
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    result = validate_sensor_packet(payload)
    assert result == payload


# ---------------------------------------------------------------------------
# 3. validate_edge_feature_summary 错误码保持不变
# ---------------------------------------------------------------------------

def test_validate_edge_feature_summary_accepts_valid_item() -> None:
    summary = _valid_edge_feature_summary()
    result = validate_edge_feature_summary(summary, "edge_01")
    assert result == summary


def test_validate_edge_feature_summary_batch_accepts_valid_batch() -> None:
    batch = {
        "batch_id": "batch_01",
        "edge_node_id": "edge_01",
        "sent_at_ns": 100,
        "item_count": 1,
        "summaries": [_valid_edge_feature_summary()],
    }
    result = validate_edge_feature_summary_batch(batch)
    assert result == batch


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda s: s.pop("bearing_id"), "INVALID_IDENTIFIER"),
        (lambda s: s.update({"sequence_number": 0}), "INVALID_IDENTIFIER"),
        (lambda s: s.update({"edge_node_id": "other_node"}), "INVALID_IDENTIFIER"),
        (lambda s: s.update({"end_timestamp_ns": 0}), "INVALID_TIMESTAMP"),
        (lambda s: s.update({"processing_status": "bogus"}), "INVALID_QUALITY"),
        (lambda s: s["features"].pop("vibration"), "INVALID_FEATURE_VALUE"),
    ],
)
def test_validate_edge_feature_summary_error_code(mutate, code: str) -> None:
    summary = _valid_edge_feature_summary()
    mutate(summary)
    with pytest.raises(ContractError) as excinfo:
        validate_edge_feature_summary(summary, "edge_01")
    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# 4. 消息结构保持不变
# ---------------------------------------------------------------------------

def test_compact_packet_for_scheduler_keeps_documented_shape() -> None:
    packet = _valid_sensor_packet()
    compact = compact_packet_for_scheduler(packet, 12.5)
    assert set(compact.keys()) == {
        "packet_id",
        "device_id",
        "sensor_id",
        "sequence_number",
        "duration_ms",
        "data_type",
        "vibration_sample_count",
        "payload_size_kb",
    }
    assert compact["packet_id"] == "packet_01"
    assert compact["data_type"] == "bearing_timeseries"
    assert compact["vibration_sample_count"] == 800
    assert compact["payload_size_kb"] == 12.5
    # 高采样率原始振动数据不得泄漏到调度侧。
    assert "vibration" not in compact


def test_canonical_summary_sha256_is_stable_and_key_order_independent() -> None:
    summary = {"b": 1, "a": 2, "c": {"z": True, "y": "x"}}
    first = canonical_summary_sha256(summary)
    second = canonical_summary_sha256(summary)
    assert first == second
    assert len(first) == 64

    reordered = {"a": 2, "c": {"y": "x", "z": True}, "b": 1}
    expected = hashlib.sha256(
        json.dumps(
            summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert first == expected == canonical_summary_sha256(reordered)


def test_error_response_keeps_documented_envelope() -> None:
    error = ContractError("INVALID_PACKET", "data.load must be between 0 and 1", "packet_01")
    assert error_response(error) == {
        "success": False,
        "packet_id": "packet_01",
        "error_code": "INVALID_PACKET",
        "message": "data.load must be between 0 and 1",
    }


# ---------------------------------------------------------------------------
# 5. core.bearing_workflow_contracts / core.bearing_actions 兼容层
# ---------------------------------------------------------------------------

def test_bearing_workflow_contracts_shim_validation_errors() -> None:
    from core.bearing_workflow_contracts import (
        FINAL_EDGE,
        FINAL_CLOUD,
        REVIEW_PACKET,
        REVIEW_BEARING_WINDOW,
        REVIEW_DEVICE,
        FinalPacketResult,
    )

    assert REVIEW_PACKET == "PACKET"
    assert REVIEW_BEARING_WINDOW == "BEARING_WINDOW"
    assert REVIEW_DEVICE == "DEVICE"

    result = FinalPacketResult(
        result_id="r1",
        device_id="d1",
        task_id="t1",
        bearing_id="b1",
        sender_id="s1",
        packet_id="p1",
        sequence_number=1,
        action_grade=3,
        confidence=0.9,
        data_quality_score=0.8,
        risk_level="high",
        decision_source=FINAL_EDGE,
        raw_data_ref="raw_1",
    )
    assert result.recommended_action == "urgent_intervention"
    assert result.as_dict()["recommended_action"] == "urgent_intervention"

    with pytest.raises(ValueError, match="sequence_number must be in \\[1, 80\\]"):
        FinalPacketResult(
            result_id="r1",
            device_id="d1",
            task_id="t1",
            bearing_id="b1",
            sender_id="s1",
            packet_id="p1",
            sequence_number=0,
            action_grade=1,
            confidence=0.9,
            data_quality_score=0.8,
            risk_level="low",
            decision_source=FINAL_CLOUD,
            raw_data_ref="raw_1",
        )


def test_bearing_actions_shim_values_and_errors() -> None:
    from core.bearing_actions import (
        ACTION_TO_GRADE,
        ACTION_TO_STATE,
        GRADE_TO_ACTION,
        action_for_grade,
        grade_for_action,
    )

    assert ACTION_TO_GRADE["shutdown"] == 4
    assert GRADE_TO_ACTION[0] == "continue_operation"
    assert ACTION_TO_STATE["shutdown"] == "fault"
    assert action_for_grade(4) == "shutdown"

    with pytest.raises(ValueError, match="action grade must be an integer from 0 to 4"):
        action_for_grade(5)
    with pytest.raises(ValueError, match="unsupported bearing action: nope"):
        grade_for_action("nope")