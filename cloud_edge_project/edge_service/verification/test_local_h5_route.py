# -*- coding: utf-8 -*-
"""H5 三通道并行正式路线（local_h5 后端）验证。

覆盖（依赖 torch，宿主机 conda `moment` 环境执行；无 torch 环境跳过）：
- local_h5 经有界队列/worker 异步推理：raw packet → H5 三通道（CNN/物理/工况）
  → 加权融合 EdgeResult → 包级 SUCCEEDED，与 HTTP 路线共用可靠性设施；
- 版本合同：客户端常量与 H5 制品 RUNTIME_MODEL_VERSION 一致；pin 不一致
  → readiness 不通过（version_mismatch=True）；
- 失败语义：H5 推理异常 → 降级执行器（诊断不可用）→ 包级 FAILED 终态，
  等待云复核，不产生伪诊断；
- 证据合同：H5 自带 build_evidence 产物通过 model_input_contract 校验。
"""
from __future__ import annotations

import math
import time

import pytest

pytest.importorskip("torch")

from edge_diagnosis.distilled_h5_model import (  # noqa: E402
    H5_LABELS,
    RUNTIME_MODEL_VERSION,
)
from edge_model.config import EdgeModelConfig  # noqa: E402
from edge_model.contracts import (  # noqa: E402
    EXECUTION_LOCAL_MODEL,
    REASON_CODE_FALLBACK_FAILED,
    RunRecord,
)
from edge_model.local_h5_client import (  # noqa: E402
    H5_RUNTIME_MODEL_VERSION,
    LocalH5ClientConfig,
    LocalH5ModelClient,
)
from edge_model.pipeline import EdgeModelPipeline  # noqa: E402
from edge_model.unavailable_runner import DiagnosisUnavailableRunner  # noqa: E402
from model_input_contract import validate_model_input  # noqa: E402


def _raw_packet(sequence: int = 1) -> dict:
    vibration = [
        0.35 * math.sin(2.0 * math.pi * 1_000 * index / 64_000)
        for index in range(3_200)
    ]
    operating = {
        "sample_rate_hz": 4_000,
        "sample_count": 200,
        "values": [1_350.0] * 200,
    }
    return {
        "device_id": "machine_01",
        "bearing_id": "bearing_01",
        "task_id": "task_h5_route",
        "packet_id": "packet_h5_%03d" % sequence,
        "sender_id": "sender_01",
        "sequence_number": sequence,
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


def _build_pipeline(client: LocalH5ModelClient, *, records: list, completions: list):
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "local_h5"
    return EdgeModelPipeline(
        cfg,
        client,
        DiagnosisUnavailableRunner(),
        on_run_record=records.append,
        on_packet_result=lambda _: None,
        on_packet_completed=completions.append,
        evidence_builder=client.build_evidence,
    )


def test_local_h5_pipeline_runs_three_channel_route_via_queue() -> None:
    """raw packet 经队列 worker → H5 三通道融合 → 包级 SUCCEEDED。"""
    records: list[RunRecord] = []
    completions: list = []
    client = LocalH5ModelClient()
    pipeline = _build_pipeline(client, records=records, completions=completions)

    pipeline.start()
    try:
        pipeline.ingest("sender_01", _raw_packet())
        assert pipeline.wait_idle(timeout_s=10.0)
    finally:
        pipeline.stop()

    assert len(completions) == 1
    done = completions[0]
    assert done.status == "SUCCEEDED"
    assert done.edge.diagnosis_label in H5_LABELS
    assert done.edge.model_version == RUNTIME_MODEL_VERSION
    # 队列路线：执行模式为模型路线（EXECUTION_LOCAL_MODEL），且无降级原因。
    assert records[0].execution_mode == EXECUTION_LOCAL_MODEL
    assert records[0].fallback_reason is None
    assert records[0].model_version == RUNTIME_MODEL_VERSION
    # 证据合同仍由 H5 build_evidence 产物承载并通过校验。
    validate_model_input(done.perception)


def test_local_h5_client_version_matches_frozen_artifact() -> None:
    """客户端常量与制品版本一致（无 torch 环境另有文本级守护测试）。"""
    assert H5_RUNTIME_MODEL_VERSION == RUNTIME_MODEL_VERSION


def test_local_h5_readiness_reports_ok_and_supports_version_pin() -> None:
    client = LocalH5ModelClient()
    ready = client.readiness()
    assert ready.ok is True
    assert ready.model_version == RUNTIME_MODEL_VERSION
    assert ready.version_mismatch is False

    pinned = LocalH5ModelClient(
        LocalH5ClientConfig(expected_version="distilled_h5_kd_mismatch")
    )
    mismatch = pinned.readiness()
    assert mismatch.ok is False
    assert mismatch.version_mismatch is True


def test_local_h5_failure_degrades_to_unavailable_for_cloud_review() -> None:
    """H5 推理异常 → 诊断不可用（FAILED 终态），不产生伪诊断。"""
    real = LocalH5ModelClient()
    real.readiness()  # 触发真实加载，供替身 build_evidence 使用

    class _ExplodingH5:
        model_version = RUNTIME_MODEL_VERSION

        def build_evidence(self, raw_packet):  # noqa: ANN001
            return real.build_evidence(raw_packet)

        def run(self, task):  # noqa: ANN001
            raise RuntimeError("h5 channel exploded")

    records: list[RunRecord] = []
    completions: list = []
    client = LocalH5ModelClient()
    client.attach_model_for_test(_ExplodingH5())
    pipeline = _build_pipeline(client, records=records, completions=completions)

    pipeline.start()
    try:
        pipeline.ingest("sender_01", _raw_packet())
        assert pipeline.wait_idle(timeout_s=10.0)
    finally:
        pipeline.stop()

    assert len(completions) == 1
    done = completions[0]
    assert done.status == "FAILED"
    assert done.error_code == REASON_CODE_FALLBACK_FAILED
    assert done.edge is None
    # 客户端把 H5 异常映射为 MODEL_INFERENCE_FAILED；降级执行器明确失败。
    assert "model_route_reason=MODEL_INFERENCE_FAILED" in (records[0].note or "")


def test_local_h5_and_official_share_queue_semantics() -> None:
    """local_h5 与 http 后端同样受队列容量约束（满载拒绝可观测）。"""
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "local_h5"
    cfg.queue.max_waiting_requests = 1
    cfg.queue.full_policy = "reject"
    records: list[RunRecord] = []
    completions: list = []

    gate = {"running": False}

    class _GatedH5:
        model_version = RUNTIME_MODEL_VERSION

        def build_evidence(self, raw_packet):  # noqa: ANN001
            return LocalH5ModelClient().build_evidence(raw_packet)

        def run(self, task):  # noqa: ANN001
            while gate["running"]:
                time.sleep(0.01)
            raise RuntimeError("gated")

    real = LocalH5ModelClient()
    real.readiness()
    client = LocalH5ModelClient()
    client.attach_model_for_test(_GatedH5())
    pipeline = _build_pipeline(client, records=records, completions=completions)

    pipeline.start()
    try:
        gate["running"] = True  # 占住唯一 in-flight
        pipeline.ingest("sender_01", _raw_packet(1))
        pipeline.ingest("sender_01", _raw_packet(2))  # capacity=1 → 排队
        third = _raw_packet(3)
        pipeline.ingest("sender_01", third)  # 满载拒绝 → 立即降级 FAILED
        assert pipeline.queue.queue_full_total >= 1
    finally:
        gate["running"] = False
        pipeline.stop()

    # 满载拒绝的包直接走降级（诊断不可用）终态。
    assert any(c.error_code == REASON_CODE_FALLBACK_FAILED for c in completions)
