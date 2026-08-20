from __future__ import annotations

from pathlib import Path
import sqlite3
from contextlib import closing

from cloud_service.config import CloudSettings
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from cloud_service.packet_diagnosis import PacketDiagnosis
from cloud_service.service import infer_cloud


class FixedPacketDiagnosisModel:
    model_version = "bearing_packet_model_v1"

    def predict(self, features: dict) -> PacketDiagnosis:
        assert features["vibration"]["rms"] > 0
        return PacketDiagnosis(
            label="fault",
            confidence=0.91,
            risk_level="high",
            recommended_action="inspect_bearing",
        )


def _signal(sample_count: int, value: float) -> dict:
    return {
        "sample_rate_hz": 64_000 if sample_count == 3_200 else 4_000,
        "sample_count": sample_count,
        "values": [value] * sample_count,
    }


def _operating_context() -> dict:
    statistics = {
        "mean": 1.0,
        "last": 1.0,
        "minimum": 1.0,
        "maximum": 1.0,
        "standard_deviation": 0.0,
    }
    return {
        "shaft_speed_rpm": {**statistics},
        "load_torque_nm": {**statistics},
        "bearing_radial_load_n": {**statistics},
        "bearing_module_temperature_c": 25.0,
    }


def _packet_request() -> dict:
    identity = {
        "device_id": "device_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "packet_id": "packet_01",
        "sequence_number": 1,
    }
    edge_features = {
        "vibration": {
            "rms": 1.0,
            "absolute_peak": 2.0,
            "kurtosis": 3.0,
            "dominant_frequency_hz": 100.0,
            "band_power_ratio_500_2000": 0.1,
            "spectral_entropy": 0.2,
        },
        "phase_current_1": {"rms_a": 1.0, "absolute_peak_a": 2.0},
        "phase_current_2": {"rms_a": 1.0, "absolute_peak_a": 2.0},
        "current_relationship": {"current_imbalance_ratio": 0.1},
        "operating_context": _operating_context(),
    }
    return {
        "edge_perception_result": {
            **identity,
            "end_generate_timestamp_ns": 2_000,
            "feature_generated_at_ns": 2_001,
            "features": edge_features,
            "perception_quality": {"status": "good", "flags": []},
            "edge_inference": {
                "edge_result": "fault",
                "confidence": 0.58,
                "edge_risk_level": "high",
            },
            "edge_model_version": "bearing_packet_model_v1",
        },
        "cloud_raw_packet": {
            **identity,
            "start_timestamp_ns": 1_000,
            "end_generate_timestamp_ns": 2_000,
            "data": {
                "vibration": _signal(3_200, 1.0),
                "phase_current_1_A": _signal(3_200, 1.0),
                "phase_current_2_A": _signal(3_200, 1.0),
                "shaft_speed_rpm": _signal(200, 1.0),
                "load_torque_nm": _signal(200, 1.0),
                "bearing_radial_load_n": _signal(200, 1.0),
                "bearing_module_temperature_c": 25.0,
            },
        },
    }


def test_packet_review_uses_injected_diagnosis_model_without_context_request(tmp_path: Path):
    result = infer_cloud(
        _packet_request(),
        settings=CloudSettings(
            backend="vllm",
            vllm_url="http://unused",
            vllm_model_name="unused",
            vllm_api_key="",
            vllm_timeout_seconds=1,
            database_path=tmp_path / "cloud.db",
        ),
        diagnosis_model=FixedPacketDiagnosisModel(),
    )

    packet_result = result["cloud_packet_result"]
    assert packet_result["packet_id"] == "packet_01"
    assert packet_result["edge_label"] == "fault"
    assert packet_result["cloud_label"] == "fault"
    assert packet_result["cloud_model_version"] == "bearing_packet_model_v1"
    assert "raw_context_request" not in result


def test_v12_cloud_review_processes_and_persists_the_exact_100ms_window(tmp_path: Path):
    legacy = _packet_request()
    edge = legacy["edge_perception_result"]
    edge.update({
        "packet_id": "packet_02", "sequence_number": 2,
        "end_generate_timestamp_ns": 100_000_000,
        "feature_generated_at_ns": 100_000_001,
    })
    data = legacy["cloud_raw_packet"]["data"]
    for name in ("vibration", "phase_current_1_A", "phase_current_2_A"):
        data[name]["sample_count"] = 6_400
        data[name]["values"] = data[name]["values"] * 2
    for name in ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n"):
        data[name]["sample_count"] = 400
        data[name]["values"] = data[name]["values"] * 2
    window = {
        "device_id": edge["device_id"], "task_id": edge["task_id"],
        "bearing_id": edge["bearing_id"], "sender_id": edge["sender_id"],
        "window_start_sequence": 1, "window_end_sequence": 2,
        "window_start_ns": 0, "window_end_ns": 100_000_000,
        "contributing_packet_ids": ["packet_01", "packet_02"],
        "sample_rate_hz": 64_000, "sample_count": 6_400, "data": data,
    }
    request = {
        "schema_version": "cloud-infer/2.0",
        "decision_round_id": build_decision_round_id(
            device_id=edge["device_id"], task_id=edge["task_id"],
            window_start_sequence=1, window_end_sequence=2,
        ),
        "diagnosis_window_id": build_diagnosis_window_id(
            device_id=edge["device_id"], task_id=edge["task_id"], bearing_id=edge["bearing_id"],
            sender_id=edge["sender_id"], window_start_sequence=1, window_end_sequence=2,
        ),
        "edge_perception_result": edge,
        "cloud_raw_window": window,
    }
    database_path = tmp_path / "cloud.db"

    result = infer_cloud(
        request,
        settings=CloudSettings(
            backend="mock", vllm_url="", vllm_model_name="", vllm_api_key="",
            vllm_timeout_seconds=1, database_path=database_path,
        ),
        diagnosis_model=FixedPacketDiagnosisModel(),
    )

    assert result["cloud_packet_result"]["window_start_sequence"] == 1
    assert result["cloud_packet_result"]["window_end_sequence"] == 2
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute(
            "SELECT start_timestamp_ns,sample_count FROM raw_packet_index WHERE packet_id='packet_02'"
        ).fetchone() == (0, 6_400)
