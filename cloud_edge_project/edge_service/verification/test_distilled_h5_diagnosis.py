from __future__ import annotations

import math

import pytest

from edge_diagnosis.distilled_h5_model import (
    H5_LABELS,
    DistilledH5DiagnosticModel,
)
from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.contracts import PacketInferenceTask
from edge_model.model_client import ModelClient
from edge_model.pipeline import EdgeModelPipeline
from edge_runtime.coordinator import _edge_bearing_result
from edge_model.contracts import PacketExecutionCompleted
from model_input_contract import validate_model_input


def _raw_packet() -> dict:
    vibration = [0.35 * math.sin(2.0 * math.pi * 1_000 * index / 64_000) for index in range(3_200)]
    operating = {
        "sample_rate_hz": 4_000,
        "sample_count": 200,
        "values": [1_350.0] * 200,
    }
    return {
        "device_id": "machine_01",
        "bearing_id": "bearing_01",
        "task_id": "task_001",
        "packet_id": "packet_001",
        "sender_id": "sender_01",
        "sequence_number": 1,
        "end_generate_timestamp_ns": 50_000_000,
        "data": {
            "vibration": {
                "sample_rate_hz": 64_000,
                "sample_count": 3_200,
                "values": vibration,
                "unit": "mm/s",
            },
            "phase_current_1_A": {
                "sample_rate_hz": 64_000,
                "sample_count": 3_200,
                "values": [1.0] * 3_200,
                "unit": "A",
            },
            "phase_current_2_A": {
                "sample_rate_hz": 64_000,
                "sample_count": 3_200,
                "values": [1.0] * 3_200,
                "unit": "A",
            },
            "shaft_speed_rpm": operating,
            "load_torque_nm": {**operating, "values": [1.1] * 200},
            "bearing_radial_load_n": {**operating, "values": [880.0] * 200},
            "bearing_module_temperature_c": 46.0,
        },
    }


def _task(packet: dict) -> PacketInferenceTask:
    return PacketInferenceTask(
        request_id="h5-probe",
        device_id=packet["device_id"],
        bearing_id=packet["bearing_id"],
        task_id=packet["task_id"],
        packet_id=packet["packet_id"],
        sender_id=packet["sender_id"],
        sequence_number=packet["sequence_number"],
        perception={},
        raw_packet=packet,
    )


def test_h5_runtime_dependencies_are_part_of_the_edge_service_package() -> None:
    from edge_diagnosis.h5_features import _compute_single
    from edge_diagnosis.h5_network import PhysicalFusionModel

    assert _compute_single([0.0] * 3_200).shape == (19,)
    assert PhysicalFusionModel(use_h4_cnn=False).vibration_dim == 128


def test_distilled_h5_loads_verified_checkpoint_and_returns_dual_layer_result() -> None:
    model = DistilledH5DiagnosticModel()

    vibration, physical_features, condition = model.prepare_inputs(_raw_packet())
    result = model.run(_task(_raw_packet()))

    assert model.deployment_status == "production"
    assert vibration.shape == (1, 800)
    assert physical_features.shape == (1, 19)
    assert condition.shape == (1, 13)
    assert result.diagnosis_label in H5_LABELS
    assert set(result.class_probabilities) == set(H5_LABELS)
    assert sum(result.class_probabilities.values()) == pytest.approx(1.0)
    if result.diagnosis_label == "healthy":
        assert (result.edge_result, result.edge_risk_level) == ("normal", "low")
    else:
        assert (result.edge_result, result.edge_risk_level) == ("fault", "high")


def test_distilled_h5_builds_a_compatible_evidence_record_from_raw_data() -> None:
    evidence = DistilledH5DiagnosticModel().build_evidence(_raw_packet())

    validate_model_input(evidence)
    assert evidence["features"]["operating_context"]["shaft_speed_rpm"]["mean"] == 1350.0


def test_local_pipeline_runs_distilled_h5_from_a_raw_50ms_packet() -> None:
    packet = _raw_packet()
    completions = []
    pipeline = EdgeModelPipeline(
        EdgeModelConfig(),
        ModelClient(ModelClientConfig()),
        DistilledH5DiagnosticModel(),
        on_run_record=lambda _: None,
        on_packet_result=lambda _: None,
        on_packet_completed=completions.append,
    )

    pipeline.start()
    pipeline.ingest(packet["sender_id"], packet)

    assert len(completions) == 1
    assert completions[0].status == "SUCCEEDED"
    assert completions[0].edge.diagnosis_label in H5_LABELS
    validate_model_input(completions[0].perception)


def test_v12_bearing_result_preserves_the_authoritative_h5_diagnosis() -> None:
    edge = DistilledH5DiagnosticModel().run(_task(_raw_packet()))
    completion = PacketExecutionCompleted(
        request_id="h5-result",
        device_id="machine_01",
        bearing_id="bearing_01",
        task_id="task_001",
        packet_id="packet_001",
        sender_id="sender_01",
        sequence_number=1,
        status="SUCCEEDED",
        error_code=None,
        started_at_ns=1,
        finished_at_ns=2,
        edge=edge,
    )

    bearing = _edge_bearing_result(completion, _raw_packet())

    assert bearing.diagnosis_label == edge.diagnosis_label
    assert dict(bearing.class_probabilities) == edge.class_probabilities
