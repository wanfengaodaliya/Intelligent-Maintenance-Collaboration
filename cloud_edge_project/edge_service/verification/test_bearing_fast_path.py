# -*- coding: utf-8 -*-
"""H3: 本地 bearing 结果 fast path 发布——enqueue 前移到模型完成回调。

验证点：
1. 窗口已注册时，完成回调内直接构造并发布 EdgeBearingResult（created_at_ns
   等于 model finished 时刻），无需等待 dispatcher。
2. 无模型输出 / v12 未装配 / raw packet 缺失时静默跳过。
3. 发布回调抛异常不外传（dispatcher 路径会重试并走上报/park）。
4. raw packet 缺失窗口但 cache 可用时走 packet_snapshot 回退路径。
5. on_packet_completed 内 fast path 先于 dispatcher.submit 执行。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from core.diagnosis_contracts import EdgeBearingResult
from diagnosis_window import DiagnosisWindowAssembler
from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from edge_runtime.coordinator import (
    EdgeRuntimeCoordinator,
    _completion_identity,
    _merge_diagnosis_window,
)


_DEFAULT_EDGE = object()


def _completion(finished_at_ns: int = 1000, edge=_DEFAULT_EDGE):
    return PacketExecutionCompleted(
        request_id="request_01", device_id="machine_01", task_id="task_001",
        bearing_id="bearing_a", sender_id="sender_a", packet_id="packet_001",
        sequence_number=1, status="SUCCEEDED", error_code=None, started_at_ns=1,
        finished_at_ns=finished_at_ns,
        edge=(
            edge
            if edge is not _DEFAULT_EDGE
            else EdgeResult("fault", 0.9, "high", "edge_model_v1")
        ),
        data_quality_score=0.8,
    )


def _window_pair():
    packet = {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "packet_id": "packet_001", "sequence_number": 1,
        "run_id": "run_01",
        "start_generate_timestamp_ns": 0, "end_generate_timestamp_ns": 50_000_000,
        "data": {"vibration": {"sample_rate_hz": 64_000, "values": []}},
    }
    assembler = DiagnosisWindowAssembler(window_ms=50)
    windows = assembler.append(packet)
    assert windows
    window = windows[0]
    merged = _merge_diagnosis_window(window)
    return window, merged


def _coordinator(**overrides):
    published = []
    kwargs = dict(
        edge_node_id="edge_01",
        ingress=mock.Mock(),
        cache=mock.Mock(),
        pipeline=SimpleNamespace(queue_length=0),
        scheduler=mock.Mock(),
        v12_flow=mock.Mock(),
        on_local_bearing_result=published.append,
    )
    kwargs.update(overrides)
    coordinator = EdgeRuntimeCoordinator(**kwargs)
    return coordinator, published


def test_fast_path_publishes_from_active_window() -> None:
    coordinator, published = _coordinator()
    window, merged = _window_pair()
    completion = _completion(finished_at_ns=123_456_789)
    coordinator._active_diagnosis_windows[_completion_identity(completion)] = (
        window, merged,
    )
    coordinator._publish_bearing_result_fast_path(completion)

    assert len(published) == 1
    result = published[0]
    assert isinstance(result, EdgeBearingResult)
    # 关键：payload created_at_ns == model finished 时刻。
    assert result.created_at_ns == 123_456_789
    assert result.window_start_sequence == 1
    assert result.window_end_sequence == 1
    assert result.bearing_state == "fault"
    assert result.run_id == "run_01"


def test_fast_path_skips_without_edge_output() -> None:
    coordinator, published = _coordinator()
    coordinator._publish_bearing_result_fast_path(_completion(edge=None))
    assert published == []


def test_fast_path_skips_without_v12_flow() -> None:
    coordinator, published = _coordinator(v12_flow=None)
    coordinator._publish_bearing_result_fast_path(_completion())
    assert published == []


def test_fast_path_skips_without_raw_packet() -> None:
    # 窗口未注册且 ingress.packet_snapshot 返回 None：静默跳过，不抛异常。
    coordinator, published = _coordinator()
    coordinator.ingress.packet_snapshot.return_value = None
    coordinator._publish_bearing_result_fast_path(_completion())
    assert published == []


def test_fast_path_exposes_publish_errors_to_publisher_counter() -> None:
    def boom(_):
        raise RuntimeError("outbox down")

    coordinator, _ = _coordinator(on_local_bearing_result=boom)
    window, merged = _window_pair()
    completion = _completion()
    coordinator._active_diagnosis_windows[_completion_identity(completion)] = (
        window, merged,
    )
    with pytest.raises(RuntimeError, match="outbox down"):
        coordinator._publish_bearing_result_fast_path(completion)


def test_fast_path_falls_back_to_packet_snapshot_when_window_missing() -> None:
    coordinator, published = _coordinator()
    _, merged = _window_pair()
    packet_record = SimpleNamespace(raw_packet_ref=("sender_a", "task_001", 1))
    coordinator.ingress.packet_snapshot.return_value = packet_record
    coordinator.cache.read.return_value = dict(merged)
    coordinator._publish_bearing_result_fast_path(_completion())

    assert len(published) == 1
    assert published[0].window_start_sequence == 1


def test_on_packet_completed_publishes_before_dispatcher_submit() -> None:
    coordinator, published = _coordinator()
    window, merged = _window_pair()
    completion = _completion()
    coordinator._active_diagnosis_windows[_completion_identity(completion)] = (
        window, merged,
    )
    order = []
    real_publish = coordinator.on_local_bearing_result

    def tracked_publish(result):
        order.append("publish")
        real_publish(result)

    coordinator.on_local_bearing_result = tracked_publish
    dispatcher = mock.Mock()
    dispatcher.submit.side_effect = lambda item: order.append("submit")
    coordinator.dispatcher = dispatcher

    coordinator.on_packet_completed(completion)

    assert order == ["publish", "submit"]
    assert len(published) == 1
