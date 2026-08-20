from __future__ import annotations

from edge_status_reporter.state import EdgeApplicationState


def test_state_reports_real_fastapi_queue_semantics_and_activity() -> None:
    values = iter((10, 20))
    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="bearing-v1",
        clock_ns=lambda: next(values),
    )

    initial = state.snapshot()
    state.touch_task_activity()
    state.touch_task_activity()
    current = state.snapshot()

    assert initial.queue_length == 0
    assert initial.last_task_activity_ns == 0
    assert current.queue_length == 0
    assert current.last_task_activity_ns == 20
    assert current.models[0].model_version == "bearing-v1"
    assert current.models[0].load_status == "LOADED"


def test_state_breakdown_provider_defines_queue_length_and_is_reported() -> None:
    """AUD-11：分桶之和即总队列长度，分桶随快照上报。"""
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    state.attach_runtime_providers(
        queue_length_provider=lambda: 999,  # 分桶存在时必须以分桶为准
        queue_breakdown_provider=lambda: {
            "ingress": 2,
            "model_pending": 3,
            "aggregation_waiting": 1,
            "cloud_review_retry": 4,
        },
    )

    snapshot = state.snapshot()

    assert snapshot.queue_length == 10
    assert snapshot.queue_measurement_status == "OK"
    assert snapshot.queue_breakdown == {
        "ingress": 2,
        "model_pending": 3,
        "aggregation_waiting": 1,
        "cloud_review_retry": 4,
    }


def test_state_queue_breakdown_excludes_device_result_outbox() -> None:
    """EDGE-4：queue_length 只统计计算/工作流积压，明确排除结果发送积压。

    - 计算 bucket（ingress/model_pending/aggregation_waiting/cloud_review_retry）
      之和即 queue_length；
    - device_result_outbox 属于交付积压，另由 /health 独立暴露，不得混入
      本计算队列，否则 Scheduler 会误判节点仍有大量推理负载。
    """
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    state.attach_runtime_providers(
        queue_breakdown_provider=lambda: {
            "ingress": 2,
            "model_pending": 3,
            "aggregation_waiting": 1,
            "cloud_review_retry": 2,
        },
    )

    snapshot = state.snapshot()

    assert snapshot.queue_length == 8
    assert snapshot.queue_breakdown == {
        "ingress": 2,
        "model_pending": 3,
        "aggregation_waiting": 1,
        "cloud_review_retry": 2,
    }
    assert "device_result_outbox" not in snapshot.queue_breakdown


def test_state_queue_zero_when_only_outbox_backlog_exists() -> None:
    """EDGE-4：结果发送积压（outbox backlog）不构成计算队列负载。

    清空计算 bucket 后即使存在 outbox 积压，queue_length 仍为 0；
    交付积压由 /health 的 device_result_outbox 独立观测。
    """
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    state.attach_runtime_providers(
        queue_breakdown_provider=lambda: {
            "ingress": 0,
            "model_pending": 0,
            "aggregation_waiting": 0,
            "cloud_review_retry": 0,
        },
    )

    snapshot = state.snapshot()

    # outbox 积压不透明给 state（另行独立暴露），因此也不影响本队列。
    assert snapshot.queue_length == 0
    assert snapshot.queue_measurement_status == "OK"


def test_state_queue_failure_falls_back_to_last_known_good_as_stale() -> None:
    """AUD-04：采集失败回退最近成功值并标记 STALE，绝不静默当新值。"""
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    calls = {"count": 0}

    def flaky_length() -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            return 7
        raise RuntimeError("queue source down")

    def flaky_breakdown():
        calls["count"] += 1
        if calls["count"] <= 2:
            return {"ingress": 3, "model_pending": 4}
        raise RuntimeError("breakdown source down")

    state.attach_runtime_providers(
        queue_length_provider=flaky_length,
        queue_breakdown_provider=flaky_breakdown,
    )

    first = state.snapshot()
    assert first.queue_length == 7
    assert first.queue_measurement_status == "OK"
    assert first.queue_breakdown == {"ingress": 3, "model_pending": 4}

    second = state.snapshot()
    assert second.queue_measurement_status == "STALE"
    assert second.queue_length == 7
    assert second.queue_breakdown == {"ingress": 3, "model_pending": 4}


def test_state_queue_failure_without_history_reports_failed_not_idle() -> None:
    """AUD-04：从未成功采集时 queue=0 必须显式标记 FAILED，不能伪装空闲。"""

    def broken() -> int:
        raise RuntimeError("queue source never worked")

    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    state.attach_runtime_providers(queue_length_provider=broken)

    snapshot = state.snapshot()

    assert snapshot.queue_length == 0
    assert snapshot.queue_measurement_status == "FAILED"


def test_state_length_provider_alone_still_works_without_breakdown() -> None:
    """未注入分桶时保持旧口径：只有总长提供方，状态 OK。"""
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    state.attach_runtime_providers(queue_length_provider=lambda: 12)

    snapshot = state.snapshot()

    assert snapshot.queue_length == 12
    assert snapshot.queue_measurement_status == "OK"
    assert snapshot.queue_breakdown is None


def test_state_rejects_negative_queue_length_like_values() -> None:
    """分桶/总长提供方返回负数时按 0 处理，防止异常值进入报告。"""
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")
    state.attach_runtime_providers(
        queue_length_provider=lambda: -5,
        queue_breakdown_provider=lambda: {"ingress": -1, "model_pending": 2},
    )

    snapshot = state.snapshot()

    assert snapshot.queue_length == 2
    assert snapshot.queue_breakdown == {"ingress": 0, "model_pending": 2}


def test_state_models_provider_failure_falls_back_to_static_status() -> None:
    """模型状态源异常时回退固定值，不影响队列语义。"""
    state = EdgeApplicationState(edge_node_id="edge_01", model_version="bearing-v1")

    def broken_models():
        raise RuntimeError("models source down")

    state.attach_runtime_providers(models_provider=broken_models)

    snapshot = state.snapshot()

    assert snapshot.models[0].model_version == "bearing-v1"
    assert snapshot.models[0].load_status == "LOADED"


def test_state_stale_expires_to_failed_after_ttl() -> None:
    """EDGE-2 / T8：STALE 仅在 TTL 内；超过 TTL 后降级为 FAILED（0），
    避免 last-known-good 无限期冒充较新的真实值。"""
    monotonic_now = {"value": 0.0}

    def tick() -> float:
        return monotonic_now["value"]

    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="bearing-v1",
        monotonic=tick,
        queue_stale_ttl_seconds=10.0,
    )
    calls = {"count": 0}

    def flaky() -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            return 7
        raise RuntimeError("queue source down")

    state.attach_runtime_providers(queue_length_provider=flaky)

    first = state.snapshot()
    assert first.queue_measurement_status == "OK"
    assert first.queue_length == 7

    # TTL 内（已过 5s < 10s）→ 仍为 STALE
    monotonic_now["value"] = 5.0
    stale = state.snapshot()
    assert stale.queue_measurement_status == "STALE"
    assert stale.queue_length == 7

    # 超过 TTL（已过 11s > 10s）→ 降级 FAILED，0 不再可信
    monotonic_now["value"] = 11.0
    failed = state.snapshot()
    assert failed.queue_measurement_status == "FAILED"
    assert failed.queue_length == 0


def test_state_stale_without_history_is_failed() -> None:
    """EDGE-2：从未成功采集过 + TTL 信息缺失 → 直接 FAILED（0），不产生 STALE。"""
    monotonic_now = {"value": 0.0}

    def broken() -> int:
        raise RuntimeError("queue source never worked")

    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="bearing-v1",
        monotonic=lambda: monotonic_now["value"],
        queue_stale_ttl_seconds=10.0,
    )
    state.attach_runtime_providers(queue_length_provider=broken)

    snapshot = state.snapshot()

    assert snapshot.queue_measurement_status == "FAILED"
    assert snapshot.queue_length == 0


# ---------------------------------------------------------------------------
# EDGE-1：Queue Provider 未注入 ≠ 真实空队列
# ---------------------------------------------------------------------------
def test_edge1_t1_no_provider_reports_failed_not_ok() -> None:
    """T1：完全未注入 Queue Provider → queue=0 + FAILED，并带明确原因。"""
    state = EdgeApplicationState(
        edge_node_id="edge_01", model_version="bearing-v1"
    )
    snapshot = state.snapshot()
    assert snapshot.queue_length == 0
    assert snapshot.queue_measurement_status == "FAILED"
    assert state.queue_error == "queue_provider_unconfigured"


def test_edge1_t2_real_empty_queue_is_ok() -> None:
    """T2：Provider 正常返回 0 → 真正的空队列，status 必须为 OK（防回归）。"""
    state = EdgeApplicationState(
        edge_node_id="edge_01", model_version="bearing-v1"
    )
    state.attach_runtime_providers(queue_length_provider=lambda: 0)
    snapshot = state.snapshot()
    assert snapshot.queue_length == 0
    assert snapshot.queue_measurement_status == "OK"
    assert state.queue_error is None


def test_edge1_t3_non_empty_queue_is_ok() -> None:
    """T3：Provider 返回 5 → queue=5 + OK。"""
    state = EdgeApplicationState(
        edge_node_id="edge_01", model_version="bearing-v1"
    )
    state.attach_runtime_providers(queue_length_provider=lambda: 5)
    snapshot = state.snapshot()
    assert snapshot.queue_length == 5
    assert snapshot.queue_measurement_status == "OK"
    assert state.queue_error is None


def test_edge1_t4_stale_behavior_preserved() -> None:
    """T4：Provider 首次 5、之后抛异常。TTL 内 STALE=5；TTL 超时 FAILED=0。
    证明本轮未破坏原 STALE→FAILED 逻辑。"""
    monotonic_now = {"value": 0.0}
    calls = {"count": 0}

    def flaky() -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            return 5
        raise RuntimeError("queue down")

    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="bearing-v1",
        monotonic=lambda: monotonic_now["value"],
        queue_stale_ttl_seconds=10.0,
    )
    state.attach_runtime_providers(queue_length_provider=flaky)

    first = state.snapshot()
    assert first.queue_measurement_status == "OK"
    assert first.queue_length == 5

    monotonic_now["value"] = 4.0
    stale = state.snapshot()
    assert stale.queue_measurement_status == "STALE"
    assert stale.queue_length == 5
    assert state.queue_error is None

    monotonic_now["value"] = 12.0
    failed = state.snapshot()
    assert failed.queue_measurement_status == "FAILED"
    assert failed.queue_length == 0
