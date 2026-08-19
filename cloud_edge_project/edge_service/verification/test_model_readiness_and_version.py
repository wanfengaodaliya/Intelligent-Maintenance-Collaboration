# -*- coding: utf-8 -*-
"""阶段 6 验证（方案 7.2 / 7.4）：模型版本对齐与 readiness 调度隔离。

- 7.2：模型服务 readiness 暴露 model_version；边缘 EDGE_MODEL_VERSION pin
  不一致 → 未就绪；
- 7.4：边缘后台就绪探针缓存结果，/health/ready 纳入 model_service_ready，
  模型不可用时不接新任务。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.model_client import ModelClient
from edge_model.pipeline import EdgeModelPipeline
from edge_model.unavailable_runner import DiagnosisUnavailableRunner
from model_service.app import readiness_payload


# ---------- 7.2：模型服务 readiness 载荷 ----------


def test_readiness_payload_includes_model_version() -> None:
    runner = SimpleNamespace(ready=True, load_error=None, model_version="qwen2.5-1.5b/phase1")
    payload = readiness_payload(runner)
    assert payload["ready"] is True
    assert payload["model_version"] == "qwen2.5-1.5b/phase1"
    assert payload["load_error"] is None


def test_readiness_payload_for_unready_or_missing_runner() -> None:
    unready = SimpleNamespace(ready=False, load_error="load_failed", model_version="v9")
    assert readiness_payload(unready) == {
        "ready": False, "load_error": "load_failed", "model_version": "v9",
    }
    assert readiness_payload(None)["ready"] is False
    assert readiness_payload(None)["model_version"] is None


# ---------- 7.2：客户端 pin 校验 ----------


def _client_with_body(body, expected_version=None):
    client = ModelClient(ModelClientConfig(
        base_url="http://127.0.0.1:8012", expected_version=expected_version,
    ))
    client._request_json = lambda path, payload=None, read_timeout_s=None: dict(body)
    return client


def test_client_readiness_parses_version_without_pin() -> None:
    result = _client_with_body(
        {"ready": True, "load_error": None, "model_version": "qwen2.5-1.5b/phase1"}
    ).readiness()
    assert result.ok is True
    assert result.model_version == "qwen2.5-1.5b/phase1"
    assert result.version_mismatch is False


def test_client_readiness_pin_match_passes() -> None:
    result = _client_with_body(
        {"ready": True, "model_version": "official-v2"},
        expected_version="official-v2",
    ).readiness()
    assert result.ok is True
    assert result.version_mismatch is False


def test_client_readiness_pin_mismatch_blocks() -> None:
    result = _client_with_body(
        {"ready": True, "model_version": "official-v2"},
        expected_version="official-v3",
    ).readiness()
    assert result.ok is False
    assert result.version_mismatch is True
    assert "mismatch" in result.detail


def test_client_readiness_service_down_has_no_mismatch() -> None:
    client = ModelClient(ModelClientConfig(base_url="http://127.0.0.1:1"))
    result = client.readiness()
    assert result.ok is False
    assert result.version_mismatch is False  # 连接失败 ≠ 版本不一致。


# ---------- 7.4：管线后台就绪探针 ----------


class _StubModelClient:
    def __init__(self, results, probe_interval_s=0.05):
        self._results = list(results)
        self.readiness_calls = 0
        # 探针周期从实际客户端配置读取（与 ModelClient.cfg 同构）。
        self.cfg = SimpleNamespace(readiness_probe_interval_s=probe_interval_s)

    def readiness(self):
        self.readiness_calls += 1
        item = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        return item

    def infer(self, *args, **kwargs):
        raise RuntimeError("stub does not infer")


def _pipeline(stub) -> EdgeModelPipeline:
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    cfg.model_client.base_url = "http://127.0.0.1:8012"
    cfg.model_client.readiness_probe_interval_s = 0.05
    return EdgeModelPipeline(
        cfg, stub, DiagnosisUnavailableRunner(),
        on_run_record=lambda _: None, on_packet_result=lambda _: None,
    )


def _ok(version):
    from edge_model.model_client import ReadinessResult
    return ReadinessResult(ok=True, model_version=version, detail="ready")


def _mismatch():
    from edge_model.model_client import ReadinessResult
    return ReadinessResult(
        ok=False, model_version="official-v2", version_mismatch=True, detail="mismatch",
    )


def test_pipeline_probe_updates_cached_snapshot() -> None:
    stub = _StubModelClient([_ok("official-v1")])
    pipeline = _pipeline(stub)
    assert pipeline.model_readiness() == {"probed": False, "ok": False}

    pipeline.start()
    try:
        deadline = time.monotonic() + 2.0
        while not pipeline.model_readiness().get("probed"):
            if time.monotonic() > deadline:
                raise AssertionError("probe did not run")
            time.sleep(0.01)
        snapshot = pipeline.model_readiness()
        assert snapshot["ok"] is True
        assert snapshot["model_version"] == "official-v1"
        assert snapshot["checked_at_ns"] > 0
    finally:
        pipeline.stop()
    assert stub.readiness_calls >= 1


def test_pipeline_probe_reflects_version_mismatch() -> None:
    stub = _StubModelClient([_mismatch()])
    pipeline = _pipeline(stub)
    pipeline.start()
    try:
        deadline = time.monotonic() + 2.0
        while not pipeline.model_readiness().get("probed"):
            if time.monotonic() > deadline:
                raise AssertionError("probe did not run")
            time.sleep(0.01)
        snapshot = pipeline.model_readiness()
        assert snapshot["ok"] is False
        assert snapshot["version_mismatch"] is True
    finally:
        pipeline.stop()


def test_pipeline_probe_manual_call_updates_without_start() -> None:
    stub = _StubModelClient([_ok("manual-v1")])
    pipeline = _pipeline(stub)
    snapshot = pipeline.probe_readiness_once()
    assert snapshot["probed"] is True
    assert pipeline.model_readiness()["model_version"] == "manual-v1"
