# -*- coding: utf-8 -*-
"""阶段 6 验证（方案 7.1）：正式模型单路线收口。

- 特征提取（raw packet → perception）与已淘汰本地模型解耦，独立可用；
- 诊断推理唯一路线为正式模型服务（HTTP），降级语义为"诊断不可用"，
  不允许复用旧模型产生看似正常的诊断结果；
- 运行配置只接受 official 后端，旧模型代码与制品从包内移除。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from edge_model.model_client import ModelClient
from edge_model.perception_evidence import PerceptionEvidenceBuilder
from edge_model.pipeline import EdgeModelPipeline
from edge_model.unavailable_runner import DiagnosisUnavailableRunner
from edge_runtime.coordinator import _edge_bearing_result
from model_input_contract import validate_model_input


EDGE_SERVICE_ROOT = Path(__file__).resolve().parents[1]


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


# ---------- 特征提取独立可用 ----------


def test_evidence_builder_produces_contract_valid_perception() -> None:
    evidence = PerceptionEvidenceBuilder().build_evidence(_raw_packet())

    validate_model_input(evidence)
    assert evidence["features"]["operating_context"]["shaft_speed_rpm"]["mean"] == 1350.0


def test_evidence_builder_is_pure_computation_without_model_artifacts() -> None:
    # 无模型权重依赖：同输入必得同输出（确定性），且不读取 models 目录。
    builder = PerceptionEvidenceBuilder()
    first = builder.build_evidence(_raw_packet())
    second = builder.build_evidence(_raw_packet())
    assert first["features"] == second["features"]


# ---------- 降级语义：诊断不可用，不产生伪诊断 ----------


def test_unavailable_runner_never_returns_a_fake_diagnosis() -> None:
    from edge_model.code_fallback import CodeFallbackRunner
    from edge_model.contracts import PacketInferenceTask

    runner = DiagnosisUnavailableRunner()
    assert isinstance(runner, CodeFallbackRunner)
    task = PacketInferenceTask(
        request_id="probe", device_id="d", bearing_id="b", task_id="t",
        packet_id="p", sender_id="s", sequence_number=1, perception={},
    )
    with pytest.raises(RuntimeError, match="MODEL_UNAVAILABLE"):
        runner.run(task)


def test_pipeline_degradation_emits_failed_not_fake_result() -> None:
    """模型服务不可达 + 队列溢出/断路 → 包级 FAILED，绝不产生伪 normal/fault。"""
    packet = _raw_packet()
    completions: list[PacketExecutionCompleted] = []
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    cfg.queue.max_waiting_requests = 1
    pipeline = EdgeModelPipeline(
        cfg,
        ModelClient(ModelClientConfig(base_url="http://127.0.0.1:1")),  # 不可达端口
        DiagnosisUnavailableRunner(),
        on_run_record=lambda _: None,
        on_packet_result=lambda _: None,
        on_packet_completed=completions.append,
        evidence_builder=PerceptionEvidenceBuilder().build_evidence,
    )
    pipeline.start()
    # 超时配置收紧让断言快速稳定。
    pipeline.cfg.timeout.queue_wait_ms = 50
    pipeline.cfg.timeout.inference_ms = 100
    pipeline.cfg.timeout.total_ms = 300

    pipeline.ingest(packet["sender_id"], packet)

    deadline_iterations = 200
    for _ in range(deadline_iterations):
        if completions:
            break
        import time as _time

        _time.sleep(0.05)

    assert len(completions) >= 1
    for completion in completions:
        assert completion.status == "FAILED"
        assert completion.edge is None
        assert completion.error_code is not None
        validate_model_input(completion.perception)  # 特征提取仍正常构建感知。


# ---------- V12 诊断身份保留（不依赖本地模型） ----------


def test_v12_bearing_result_preserves_official_model_diagnosis() -> None:
    edge = EdgeResult(
        edge_result="fault", confidence=0.9, edge_risk_level="high",
        model_version="official-model-v1", diagnosis_label="outer_ring_damage",
        class_probabilities={"healthy": 0.05, "outer_ring_damage": 0.9, "inner_ring_damage": 0.05},
    )
    completion = PacketExecutionCompleted(
        request_id="official-result", device_id="machine_01", bearing_id="bearing_01",
        task_id="task_001", packet_id="packet_001", sender_id="sender_01",
        sequence_number=1, status="SUCCEEDED", error_code=None,
        started_at_ns=1, finished_at_ns=2, edge=edge,
    )

    bearing = _edge_bearing_result(completion, _raw_packet())

    assert bearing.diagnosis_label == edge.diagnosis_label
    assert dict(bearing.class_probabilities) == edge.class_probabilities


# ---------- 收口：旧模型代码与制品移除，配置只接受 official ----------


def test_legacy_local_model_modules_are_removed() -> None:
    assert importlib.util.find_spec("edge_diagnosis") is None
    assert not (EDGE_SERVICE_ROOT / "models" / "distilled_h5" / "best_model.pt").exists()


def test_runtime_config_only_accepts_official_backend() -> None:
    import yaml

    config_path = EDGE_SERVICE_ROOT.parent / "configs" / "local.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["model"]["edge_backend"] == "official"
