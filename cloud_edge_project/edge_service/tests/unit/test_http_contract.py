# -*- coding: utf-8 -*-
"""本地 HTTP 契约测试：假 ModelRunner + 真实 HTTP 服务 + 真实 ModelClient。

不加载 torch。验证 Windows↔WSL HTTP 边界契约：
- /health /readiness 正确；
- /infer 成功/输出非法/MODEL_BUSY 的客户端识别；
- 服务繁忙立即返回，不在服务端形成隐式队列（无请求堆积）；
- 剩余时间不足立即拒绝。
"""
from __future__ import annotations

import threading
import time

import pytest

from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.model_client import ModelClient
from model_service.app import make_server
from model_input_contract import model_input_probe

PORT = 18123


class FakeRunner:
    """模拟 ModelRunner 的接口（ready/load_error/infer），用真实非阻塞锁模拟 MODEL_BUSY。"""

    def __init__(self, ready: bool = True, fail_mode: str = "none", infer_sleep_s: float = 0.0):
        self._ready = ready
        self._load_error = None
        self.fail_mode = fail_mode
        self.infer_sleep_s = infer_sleep_s
        self._lock = threading.Lock()
        self.infer_calls = 0

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def load_error(self):
        return self._load_error

    def infer(self, model_input, request_id=None, remaining_timeout_ms=None):
        self.infer_calls += 1
        if remaining_timeout_ms is not None and remaining_timeout_ms <= 0:
            return {"valid": False, "error": "MODEL_INFERENCE_TIMEOUT", "request_id": request_id}
        # 非阻塞锁：忙 → 立即 MODEL_BUSY，不排队
        if not self._lock.acquire(blocking=False):
            return {"valid": False, "error": "MODEL_BUSY", "request_id": request_id}
        try:
            if self.infer_sleep_s:
                time.sleep(self.infer_sleep_s)
            if self.fail_mode == "invalid":
                return {"valid": False, "error": "MODEL_OUTPUT_INVALID", "request_id": request_id}
            return {"valid": True, "edge_result": "warning", "edge_risk_level": "medium",
                    "confidence": 0.7, "model_version": "fake/v1", "request_id": request_id}
        finally:
            self._lock.release()


@pytest.fixture
def http():
    cfg = ModelClientConfig(base_url="http://127.0.0.1:%d" % PORT)
    runner = FakeRunner()
    server = make_server("127.0.0.1", PORT, runner)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield runner, ModelClient(cfg), server
    server.shutdown()
    server.server_close()


def _edge_cfg() -> EdgeModelConfig:
    return EdgeModelConfig()


def test_health_ok(http):
    runner, client, _srv = http
    h = client.health()
    assert h.ok is True


def test_readiness_reflects_runner(http):
    runner, client, _srv = http
    assert client.readiness().ok is True
    runner._ready = False
    assert client.readiness().ok is False
    runner._ready = True
    assert client.readiness().ok is True


def test_infer_success(http):
    runner, client, _srv = http
    r = client.infer(model_input_probe())
    assert r.success is True
    assert r.edge is not None
    assert r.edge.edge_result == "warning"
    assert r.edge.model_version == "fake/v1"
    assert r.error is None


def test_infer_output_invalid_surfaces_error_code(http):
    runner, client, _srv = http
    runner.fail_mode = "invalid"
    r = client.infer(model_input_probe())
    assert r.success is False
    assert r.error == "MODEL_OUTPUT_INVALID"


def test_model_busy_returns_immediately(http):
    runner, client, _srv = http
    runner.infer_sleep_s = 0.5  # 模拟推理占用
    # 第一个请求占用锁，随后并发请求应立即 MODEL_BUSY（不等锁）
    results = []

    def _call():
        results.append(client.infer(model_input_probe(), inference_timeout_ms=3000))

    threads = [threading.Thread(target=_call) for _ in range(8)]
    t0 = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.monotonic() - t0
    # 不应有服务端队列堆积：总耗时接近单次推理（0.5s），而不是 8×0.5s
    assert elapsed < 2.0, "服务端形成了隐式队列: %.2fs" % elapsed
    busy = [r for r in results if r.error == "MODEL_BUSY"]
    assert len(busy) >= 1, "并发请求应至少有一个 MODEL_BUSY"
    assert any(r.success for r in results), "首个请求应成功"


def test_remaining_timeout_zero_rejected(http):
    runner, client, _srv = http
    r = client.infer(model_input_probe(), remaining_timeout_ms=0)
    assert r.success is False
    assert r.error == "MODEL_INFERENCE_TIMEOUT"


def test_not_ready_returns_503_not_served(http):
    runner, client, _srv = http
    runner._ready = False
    request_id = "request-not-ready"
    r = client.infer(model_input_probe(), request_id=request_id)
    assert r.success is False
    assert r.error == "MODEL_UNAVAILABLE"  # 服务端 503 → 客户端映射为不可用
    assert r.request_id == request_id


def test_incomplete_input_returns_deterministic_error_and_request_id(http):
    runner, client, _srv = http
    model_input = model_input_probe()
    del model_input["features"]["phase_current_2"]
    request_id = "request-input-invalid"

    result = client.infer(model_input, request_id=request_id)

    assert result.success is False
    assert result.error == "MODEL_INPUT_INVALID"
    assert result.request_id == request_id
    assert runner.infer_calls == 0
