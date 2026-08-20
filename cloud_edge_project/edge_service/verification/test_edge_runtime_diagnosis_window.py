from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from diagnosis_window import DiagnosisWindowAssembler
from edge_runtime.coordinator import EdgeRuntimeCoordinator, _merge_diagnosis_window
from edge_perception import (
    ConstantDetectionConfig, EdgePerception, PerceptionConfig,
    PerceptionInvocationContext, file_sha256,
)
from edge_task_ingress import INGRESS_ACCEPTED


def _packet(sequence: int) -> dict:
    return {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "packet_id": f"packet_{sequence:03d}",
        "sequence_number": sequence,
        "start_generate_timestamp_ns": (sequence - 1) * 50_000_000,
        "end_generate_timestamp_ns": sequence * 50_000_000,
        "data": {
            "vibration": {
                "sample_rate_hz": 64_000, "sample_count": 2,
                "unit": "mm/s", "values": [float(sequence), float(sequence) + .5],
            },
            "bearing_module_temperature_c": 40.0 + sequence,
        },
    }


class _Ingress:
    def __init__(self):
        self.completions = []

    def receive_packet(self, packet):
        return SimpleNamespace(
            status=INGRESS_ACCEPTED, validated_packet=packet,
            received_at_ns=packet["end_generate_timestamp_ns"],
        )

    def record_packet_completion(self, **payload):
        self.completions.append(payload)
        return True


class _Pipeline:
    def __init__(self):
        self.inputs = []
        self.queue_length = 0
        self.on_packet_completed = None

    def ingest(self, sender_id, payload):
        self.inputs.append((sender_id, payload))


def test_runtime_directs_each_50ms_packet_after_complete_window() -> None:
    pipeline = _Pipeline()
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id="edge_01", ingress=_Ingress(), cache=object(),
        pipeline=pipeline, scheduler=object(),
        diagnosis_window_assembler=DiagnosisWindowAssembler(
            window_ms=50, step_ms=50, overlap_enabled=False,
        ),
    )

    assert coordinator.receive_raw_packet(_packet(1)) is True
    merged = pipeline.inputs[0][1]
    assert merged["packet_id"] == "packet_001"
    assert merged["window_start_sequence"] == 1
    assert merged["window_end_sequence"] == 1
    assert merged["contributing_packet_ids"] == ["packet_001"]
    assert merged["data"]["vibration"]["sample_count"] == 2
    assert merged["data"]["vibration"]["values"] == [1.0, 1.5]
    assert len(pipeline.inputs) == 1


def test_merged_50ms_window_preserves_edge_and_cloud_manifest_identity() -> None:
    assembler = DiagnosisWindowAssembler(window_ms=50)
    window = next(
        result for sequence in range(1, 2)
        for result in assembler.append(_packet(sequence))
    )

    merged = _merge_diagnosis_window(window)

    assert merged["diagnosis_window_id"] == window.diagnosis_window_id
    assert merged["decision_round_id"] == window.decision_round_id
    assert merged["window_start_ns"] == 0
    assert merged["window_end_ns"] == 50_000_000
    assert merged["data"]["vibration"]["sample_count"] == 2


@pytest.mark.parametrize("window_ms", [100, 150])
def test_assembler_rejects_non_50ms_diagnosis_windows(window_ms: int) -> None:
    with pytest.raises(ValueError, match="locked at 50"):
        DiagnosisWindowAssembler(window_ms=window_ms)


def test_real_edge_perception_accepts_50ms_window() -> None:
    fir = Path(__file__).parents[2] / "scenarios" / "bearing" / "edge" / "assets" / "fir_64k_to_16k_369.txt"
    source = "development_test"
    perception = EdgePerception(
        PerceptionConfig(
            profile=source, fir_coefficients_path=fir, fir_sha256=file_sha256(fir),
            fir_asset_source=source, fir_asset_version="test-v1",
            running_speed_threshold_rpm=100.0,
            running_speed_threshold_source=source, running_speed_threshold_version="test-v1",
            constant_detection={
                name: ConstantDetectionConfig(True, 1e-9, source, "test-v1")
                for name in ("vibration", "phase_current_1_A", "phase_current_2_A")
            },
            feature_zero_rms_threshold=1e-10, feature_zero_power_threshold=1e-20,
            current_relationship_zero_rms_threshold=1e-10,
            numerical_threshold_source=source, numerical_threshold_version="test-v1",
            feature_extractor_version="test-v1", runtime_dependencies={"numpy": np.__version__},
            absolute_tolerance=1e-12, relative_tolerance=1e-9,
        ),
        clock_ns=lambda: 200_000_001,
    )
    packet_count = 1
    high_count = 3_200 * packet_count
    context_count = 200 * packet_count
    high_t = np.arange(high_count) / 64_000.0
    packet = {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "packet_id": f"packet_{packet_count:03d}",
        "sequence_number": packet_count,
        "end_generate_timestamp_ns": packet_count * 50_000_000,
        "data": {
            "vibration": {"sample_rate_hz": 64_000, "sample_count": high_count, "unit": "mm/s", "values": np.sin(2 * np.pi * 1_000 * high_t)},
            "phase_current_1_A": {"sample_rate_hz": 64_000, "sample_count": high_count, "unit": "A", "values": 2 + np.sin(2 * np.pi * 200 * high_t)},
            "phase_current_2_A": {"sample_rate_hz": 64_000, "sample_count": high_count, "unit": "A", "values": 2 + np.sin(2 * np.pi * 210 * high_t)},
            "shaft_speed_rpm": {"sample_rate_hz": 4_000, "sample_count": context_count, "values": np.full(context_count, 900.0)},
            "load_torque_nm": {"sample_rate_hz": 4_000, "sample_count": context_count, "values": np.full(context_count, 100.0)},
            "bearing_radial_load_n": {"sample_rate_hz": 4_000, "sample_count": context_count, "values": np.full(context_count, 1_000.0)},
            "bearing_module_temperature_c": 40.0,
        },
    }
    context = PerceptionInvocationContext(
        edge_node_id="edge_01", perception_received_at_ns=1,
    )

    downsampled = perception.downsample(packet, context)
    assert downsampled.status.success
    assert downsampled.payload["data"]["vibration"]["sample_count"] == 800 * packet_count
    result = perception.perceive(downsampled.payload, context)
    assert result.status.success
    assert result.payload["sequence_number"] == packet_count


def test_runtime_never_emits_partial_50ms_windows() -> None:
    ingress = _Ingress()
    pipeline = _Pipeline()
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id="edge_01", ingress=ingress, cache=object(),
        pipeline=pipeline, scheduler=object(),
        diagnosis_window_assembler=DiagnosisWindowAssembler(window_ms=50),
    )

    for sequence in range(1, 4):
        coordinator.receive_raw_packet(_packet(sequence))

    # 50ms 下每一包都自成完整窗口，直接进入推理，绝不产生 incomplete tail。
    assert len(pipeline.inputs) == 3
    assert ingress.completions == []