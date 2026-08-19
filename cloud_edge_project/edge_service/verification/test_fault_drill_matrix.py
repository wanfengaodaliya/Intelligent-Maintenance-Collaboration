# -*- coding: utf-8 -*-
"""阶段 6 验证（方案 7.5）：性能与故障演练矩阵。

用真实 HTTP Fake 模型服务（见 _fake_model_service.py）+ 真实 ModelClient +
真实 EdgeModelPipeline 端到端演练：

| 矩阵 | 注入 | 预期 |
|---|---|---|
| A 服务忙 | 503 MODEL_BUSY | 包级 FAILED，run record 记 MODEL_BUSY，无伪诊断 |
| B 推理超时 | 服务休眠超预算 | FAILED，记 MODEL_INFERENCE_TIMEOUT |
| C 非法 JSON / 输出合同非法 | 200 非法体 / MODEL_OUTPUT_INVALID | FAILED，分别记 MODEL_INFERENCE_FAILED / MODEL_OUTPUT_INVALID |
| D 输入字段越界 | MODEL_INPUT_INVALID | FAILED，记 MODEL_INPUT_INVALID |
| E 进程退出与恢复 | 服务 shutdown → 重启 | 失败期 MODEL_UNAVAILABLE；恢复后推理成功且断路器闭合 |
| F 版本切换与回滚 | readiness 上报 v1→v2→v1 | pin 不一致时探针报 mismatch，回滚后恢复 |
| G 断路器 | 连续失败达阈值 | breaker open，新包直接 BREAKER_OPEN 不再请求服务 |
| H 冷启动首次推理与持续吞吐 | 服务启动后连续请求 | 首包即成功；连续包全部成功且时延被记录 |
| I 无伪诊断总断言 | 遍历全部故障模式 | 所有 completed 均 FAILED 且 edge=None；成功路径版本来自服务上报 |

真实 GPU 模型的冷启动耗时、预热、显存稳定性与持续吞吐压测属于真实环境
验收项（依赖正式模型服务），本矩阵验证的是 Edge 侧链路行为；两者在交付
报告中明确区分（方案第 10 节）。
"""
from __future__ import annotations

import time

from _fake_model_service import FakeModelService
from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.contracts import (
    EXECUTION_CODE_FALLBACK,
    REASON_BREAKER_OPEN,
    REASON_CODE_FALLBACK_FAILED,
    REASON_MODEL_BUSY,
    REASON_MODEL_INFERENCE_FAILED,
    REASON_MODEL_INFERENCE_TIMEOUT,
    REASON_MODEL_INPUT_INVALID,
    REASON_MODEL_OUTPUT_INVALID,
    REASON_MODEL_UNAVAILABLE,
    PacketExecutionCompleted,
    RunRecord,
)
from edge_model.model_client import ModelClient
from edge_model.perception_evidence import PerceptionEvidenceBuilder
from edge_model.pipeline import EdgeModelPipeline
from edge_model.unavailable_runner import DiagnosisUnavailableRunner
from test_official_model_route import _raw_packet


def _build(fake_url: str, pin: str | None = None):
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    cfg.timeout.queue_wait_ms = 2000
    cfg.timeout.inference_ms = 400
    cfg.timeout.total_ms = 3000
    cfg.breaker.consecutive_failure_threshold = 3
    cfg.breaker.recovery_probe_interval_s = 0.3
    client = ModelClient(ModelClientConfig(
        base_url=fake_url, expected_version=pin, readiness_probe_interval_s=0.1,
    ))
    completions: list[PacketExecutionCompleted] = []
    records: list[RunRecord] = []
    pipeline = EdgeModelPipeline(
        cfg, client, DiagnosisUnavailableRunner(),
        on_run_record=records.append,
        on_packet_result=lambda _: None,
        on_packet_completed=completions.append,
        evidence_builder=PerceptionEvidenceBuilder().build_evidence,
    )
    return pipeline, completions, records


def _wait(completions, count: int, timeout_s: float = 6.0) -> None:
    deadline = time.monotonic() + timeout_s
    while len(completions) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(completions) >= count, "期望 %d 个包完成，实际 %d" % (count, len(completions))


def _send_one(pipeline: EdgeModelPipeline, sequence: int = 1) -> None:
    packet = _raw_packet()
    packet["sequence_number"] = sequence
    packet["packet_id"] = "packet_%03d" % sequence
    pipeline.ingest(packet["sender_id"], packet)


# ---------- 矩阵 A：服务忙 ----------


def test_drill_busy_maps_to_failed_without_fake_diagnosis() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    fake.set_mode("busy")
    pipeline.start()
    try:
        _send_one(pipeline)
        _wait(completions, 1)
        completed = completions[0]
        assert completed.status == "FAILED"
        assert completed.error_code == REASON_CODE_FALLBACK_FAILED
        assert completed.edge is None  # 无伪诊断。
        assert records[0].fallback_reason == REASON_MODEL_BUSY
        assert records[0].execution_mode == EXECUTION_CODE_FALLBACK
        assert records[0].output_valid is False
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 B：推理超时 ----------


def test_drill_inference_timeout_respects_budget() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)  # 推理预算 400ms，服务休眠 3s
    fake.set_mode("timeout")
    pipeline.start()
    try:
        started = time.monotonic()
        _send_one(pipeline)
        _wait(completions, 1, timeout_s=4.0)
        elapsed = time.monotonic() - started
        assert completions[0].status == "FAILED"
        assert completions[0].edge is None
        assert records[0].fallback_reason == REASON_MODEL_INFERENCE_TIMEOUT
        # 预算 400ms + 降级储备，不得等满服务的 3s 休眠。
        assert elapsed < 2.5, "推理超时后应在预算内失败，实际 %.2fs" % elapsed
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 C：非法 JSON 体与输出合同非法 ----------


def test_drill_bad_json_body_fails_explicitly() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    fake.set_mode("bad_json")
    pipeline.start()
    try:
        _send_one(pipeline)
        _wait(completions, 1)
        assert completions[0].status == "FAILED"
        assert completions[0].edge is None
        assert records[0].fallback_reason == REASON_MODEL_INFERENCE_FAILED
    finally:
        pipeline.stop()
        fake.stop()


def test_drill_output_contract_invalid_fails_explicitly() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    fake.set_mode("output_invalid")
    pipeline.start()
    try:
        _send_one(pipeline)
        _wait(completions, 1)
        assert completions[0].status == "FAILED"
        assert completions[0].edge is None
        assert records[0].fallback_reason == REASON_MODEL_OUTPUT_INVALID
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 D：输入字段越界 ----------


def test_drill_input_invalid_maps_service_error() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    fake.set_mode("input_invalid")
    pipeline.start()
    try:
        _send_one(pipeline)
        _wait(completions, 1)
        assert completions[0].status == "FAILED"
        assert completions[0].edge is None
        assert records[0].fallback_reason == REASON_MODEL_INPUT_INVALID
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 E：进程退出与恢复 ----------


def test_drill_service_exit_and_recovery() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    pipeline.start()
    try:
        # 正常推理成功（冷启动后首个请求）。
        _send_one(pipeline)
        _wait(completions, 1)
        assert completions[0].status == "SUCCEEDED"
        assert completions[0].edge is not None
        assert pipeline.worker.breaker_state == "closed"

        # 模拟模型服务进程退出：连接层失败。
        # Windows loopback 对已关闭端口不立即 RST，可能挂起到读超时，
        # 因此 UNAVAILABLE 与 INFERENCE_TIMEOUT 均为合法失败形态；
        # 服务退出由 readiness 探针感知（见下方 probe 断言）。
        fake.stop()
        _send_one(pipeline, sequence=2)
        _wait(completions, 2, timeout_s=4.0)
        assert completions[1].status == "FAILED"
        assert completions[1].edge is None
        assert records[-1].fallback_reason in {
            REASON_MODEL_UNAVAILABLE, REASON_MODEL_INFERENCE_TIMEOUT,
        }
        # 就绪探针感知服务退出。
        assert pipeline.probe_readiness_once()["ok"] is False

        # 恢复：服务重启（新端口），更新客户端地址并探测就绪。
        fake2 = FakeModelService().start()
        try:
            pipeline.model_client.cfg.base_url = fake2.url
            snapshot = pipeline.probe_readiness_once()
            assert snapshot["ok"] is True
            assert snapshot["model_version"] == fake2.version
            _send_one(pipeline, sequence=3)
            _wait(completions, 3)
            assert completions[2].status == "SUCCEEDED"
            assert completions[2].edge is not None
            # 成功后断路器恢复闭合。
            assert pipeline.worker.breaker_state == "closed"
        finally:
            fake2.stop()
    finally:
        pipeline.stop()


# ---------- 矩阵 F：版本切换与回滚 ----------


def test_drill_version_switch_and_rollback() -> None:
    fake = FakeModelService(version="official-v1").start()
    pipeline, _, _ = _build(fake.url, pin="official-v1")
    pipeline.start()
    try:
        # 初始：pin 与服务一致 → 就绪。
        deadline = time.monotonic() + 2.0
        while not pipeline.model_readiness().get("probed"):
            time.sleep(0.02)
        assert pipeline.probe_readiness_once()["ok"] is True

        # 模型服务升级切换到 v2：pin(v1) 不一致 → 未就绪（调度隔离）。
        fake.set_version("official-v2")
        deadline = time.monotonic() + 2.0
        while not pipeline.model_readiness().get("version_mismatch"):
            if time.monotonic() > deadline:
                raise AssertionError("未观察到版本不一致")
            time.sleep(0.02)
        snapshot = pipeline.model_readiness()
        assert snapshot["ok"] is False
        assert snapshot["model_version"] == "official-v2"

        # 回滚到 v1（或同步更新 pin）：恢复就绪。
        fake.set_version("official-v1")
        deadline = time.monotonic() + 2.0
        while not pipeline.model_readiness().get("ok"):
            if time.monotonic() > deadline:
                raise AssertionError("回滚后未恢复就绪")
            time.sleep(0.02)
        assert pipeline.model_readiness()["model_version"] == "official-v1"
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 G：断路器 ----------


def test_drill_breaker_opens_and_blocks_model_calls() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)  # 阈值 3
    fake.set_mode("busy")
    pipeline.start()
    try:
        for sequence in range(1, 4):  # 3 次连续失败 → 断路器打开。
            _send_one(pipeline, sequence=sequence)
            _wait(completions, sequence)
        assert pipeline.worker.breaker_state == "open"

        calls_before = fake.infer_requests
        _send_one(pipeline, sequence=4)
        _wait(completions, 4)
        # 断路器打开：新包直接降级，不再请求模型服务。
        assert records[-1].fallback_reason == REASON_BREAKER_OPEN
        assert fake.infer_requests == calls_before

        # 恢复探测窗口过后允许重试，服务恢复 → 成功且断路器闭合。
        fake.set_mode("ok")
        time.sleep(0.35)
        _send_one(pipeline, sequence=5)
        _wait(completions, 5)
        assert completions[4].status == "SUCCEEDED"
        assert pipeline.worker.breaker_state == "closed"
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 H：冷启动首次推理与持续吞吐 ----------


def test_drill_first_inference_and_sustained_throughput() -> None:
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    pipeline.start()
    try:
        total = 6
        for sequence in range(1, total + 1):  # 串行发送，容量内不排队超时。
            _send_one(pipeline, sequence=sequence)
            _wait(completions, sequence)
        assert all(c.status == "SUCCEEDED" for c in completions)
        # 全部走正式模型路线（LOCAL_MODEL 执行模式 + 服务上报版本）。
        assert all(r.execution_mode == "LOCAL_MODEL" and r.output_valid
                   for r in records)
        assert all(r.model_version == fake.version for r in records)
        assert fake.infer_requests == total
        # 推理时延被记录（性能观测可用）。
        assert all(r.inference_latency_ms is not None and r.inference_latency_ms >= 0
                   for r in records)
    finally:
        pipeline.stop()
        fake.stop()


# ---------- 矩阵 I：全部故障模式无伪诊断总断言 ----------


def test_no_fake_diagnosis_across_all_failure_modes() -> None:
    failure_modes = ["busy", "timeout", "bad_json", "output_invalid", "input_invalid"]
    fake = FakeModelService().start()
    pipeline, completions, records = _build(fake.url)
    pipeline.start()
    try:
        sequence = 0
        for mode in failure_modes:
            fake.set_mode(mode)
            sequence += 1
            _send_one(pipeline, sequence=sequence)
            _wait(completions, sequence, timeout_s=4.0)
        # 每种故障模式：包级 FAILED、edge=None、无成功 run record。
        for completed in completions:
            assert completed.status == "FAILED"
            assert completed.edge is None
            assert completed.data_quality_score == 0.0
        for record in records:
            assert record.output_valid is False
            assert record.execution_mode == EXECUTION_CODE_FALLBACK
            assert record.edge_result is None
        # 正式路线没有任何旧模型版本结果。
        versions = {r.model_version for r in records if r.output_valid}
        assert versions == set()
    finally:
        pipeline.stop()
        fake.stop()
