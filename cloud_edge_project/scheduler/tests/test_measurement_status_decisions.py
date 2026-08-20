"""EDGE-2: 决策层真正消费 cpu/queue 的 measurement_status。"""

from __future__ import annotations

import time

from scheduler.assignment_scheduler import (
    AssignmentScheduler,
    _measurement_gate,
)
from scheduler.node_registry import (
    NEUTRAL_COMPONENT_SCORE,
    EdgeNodeConfig,
    NodeRegistry,
    _compute_base_score,
)
from scheduler.task_repository import TaskRepository


def _resources(**overrides) -> dict:
    resource = {
        "logical_cpu_count": 8,
        "cpu_utilization_percent": 0.0,
        "memory_available_mb": 4096.0,
        "gpu_available": False,
        "npu_available": False,
        "queue_length": 0,
    }
    resource.update(overrides)
    return resource


def _report(edge_id: str, reported_at_ns: int, resources: dict) -> dict:
    return {
        "edge_node_id": edge_id,
        "reported_at_ns": reported_at_ns,
        "resources": resources,
        "models": [
            {"model_version": "bearing_packet_model_v1", "load_status": "LOADED"}
        ],
        "last_task_activity_ns": 0,
    }


def _registry(*edge_ids: str) -> NodeRegistry:
    configs = {
        edge_id: EdgeNodeConfig(
            edge_id,
            f"http://127.0.0.1:{8000 + int(edge_id.split('_')[1])}",
            f"edge/{edge_id}/input",
        )
        for edge_id in edge_ids
    }
    return NodeRegistry(configs)


def _push_online(registry: NodeRegistry, edge_id: str, resources: dict) -> None:
    """真实 update_status 路径写两次，使节点进入 ONLINE（需 recovery_report_count>=2）。

    单调接收时间用真实时钟，避免 refresh_liveness 判为过期而回到 OFFLINE。
    """
    for seq in (1, 2):
        registry.update_status(
            _report(edge_id, seq, resources),
            received_at_ns=time.time_ns(),
            received_monotonic_ns=time.monotonic_ns(),
        )


def test_cpu_ok_vs_degrated_same_zero_cpu() -> None:
    """T1: cpu=0 + OK 与 DEGRADED 相同，但 DEGRADED 的 CPU 分量降为中性，总分更低。"""
    assert _compute_base_score(_resources()) == 85.0  # 旧公式锚点
    ok_score = _compute_base_score(_resources(cpu_measurement_status="OK"))
    degraded_score = _compute_base_score(
        _resources(cpu_measurement_status="DEGRADED")
    )
    assert ok_score == 85.0
    assert degraded_score < ok_score
    cpu_ok_component = 100.0 - 0.0
    assert cpu_ok_component == 100.0
    # 反推 degraded 的 CPU 分量即中性值
    degraded_cpu_component = (degraded_score - 15.0 - 30.0) / 0.40
    assert abs(degraded_cpu_component - NEUTRAL_COMPONENT_SCORE) < 1e-9


def test_cpu_failed_gate() -> None:
    """T2: cpu_status=FAILED → _measurement_gate 返回 cpu_measurement_failed。"""
    assert (
        _measurement_gate(_resources(cpu_measurement_status="FAILED"))
        == "cpu_measurement_failed"
    )
    # 且 CPU 分量取中性
    failed = _compute_base_score(_resources(cpu_measurement_status="FAILED"))
    cpu_component = (failed - 15.0 - 30.0) / 0.40
    assert abs(cpu_component - NEUTRAL_COMPONENT_SCORE) < 1e-9


def test_queue_failed_neutral_and_gated() -> None:
    """T3: queue=0 + FAILED → queue 分量中性，candidate 被排除。"""
    assert (
        _measurement_gate(_resources(queue_measurement_status="FAILED"))
        == "queue_measurement_failed"
    )
    failed = _compute_base_score(
        _resources(queue_length=0, queue_measurement_status="FAILED")
    )
    queue_component = (failed - 40.0 - 15.0) / 0.30  # cpu=0 → 40; memory=50*0.3=15
    assert abs(queue_component - NEUTRAL_COMPONENT_SCORE) < 1e-9


def test_queue_stale_uses_last_known_good() -> None:
    """T4: queue=5 + STALE → 不排除，queue 分量按真实历史值 5 计算。"""
    assert _measurement_gate(_resources(queue_measurement_status="STALE")) is None
    stale = _compute_base_score(
        _resources(queue_length=5, queue_measurement_status="STALE")
    )
    assert stale == 70.0  # cpu=0→40, mem→15, queue=5→50*0.3=15
    queue_component = (stale - 40.0 - 15.0) / 0.30
    assert abs(queue_component - 50.0) < 1e-9


def test_old_report_stays_legacy() -> None:
    """T5: 无任何状态字段 → base_score 与旧公式逐位一致，gate 放行。"""
    assert _measurement_gate(_resources()) is None
    assert _compute_base_score(_resources()) == 85.0


def test_rank_excludes_failed_queue_node() -> None:
    """T6: 跨层真实路径——FAILED queue 节点不进入 ranked，OK 节点正常参与。"""
    registry = _registry("edge_01", "edge_02")
    _push_online(
        registry,
        "edge_01",
        _resources(queue_length=0, queue_measurement_status="FAILED"),
    )
    _push_online(
        registry,
        "edge_02",
        _resources(queue_length=3, queue_measurement_status="OK"),
    )
    scheduler = AssignmentScheduler(  # type: ignore[arg-type]
        registry,
        TaskRepository(None),
    )
    request = _assignment_request(sender_id="sender_01")

    ranked = scheduler._rank_candidates(
        request, deadline=time.monotonic() + 5.0  # type: ignore[arg-type]
    )

    ranked_ids = {item.state.config.edge_node_id for item in ranked}
    assert "edge_01" not in ranked_ids  # FAILED queue 被排除
    assert "edge_02" in ranked_ids


def test_rank_degrated_cpu_still_eligible_but_lower() -> None:
    """T7: cpu=0 DEGRADED 与 OK 均可参与，但 DEGRADED 总分严格更低。"""
    registry = _registry("edge_01", "edge_02")
    _push_online(
        registry,
        "edge_01",
        _resources(cpu_utilization_percent=0.0, cpu_measurement_status="DEGRADED"),
    )
    _push_online(
        registry,
        "edge_02",
        _resources(cpu_utilization_percent=0.0, cpu_measurement_status="OK"),
    )
    scheduler = AssignmentScheduler(  # type: ignore[arg-type]
        registry,
        TaskRepository(None),
    )
    request = _assignment_request(sender_id="sender_01")

    ranked = scheduler._rank_candidates(
        request, deadline=time.monotonic() + 5.0  # type: ignore[arg-type]
    )

    by_id = {item.state.config.edge_node_id: item for item in ranked}
    assert "edge_01" in by_id  # DEGRADED 不排除
    assert "edge_02" in by_id
    assert by_id["edge_01"].total_score < by_id["edge_02"].total_score


def test_cross_layer_measurement_status_reaches_decision() -> None:
    """跨层集成：真实 update_status → registry → _rank_candidates，字段名一致生效。

    Edge A: cpu=0 FAILED queue=0 OK → 被 gate 排除；
    Edge B: cpu=60 OK queue=3 OK → 正常参与。
    """
    registry = _registry("edge_01", "edge_02")
    _push_online(
        registry,
        "edge_01",
        _resources(
            cpu_utilization_percent=0.0,
            cpu_measurement_status="FAILED",
            queue_length=0,
            queue_measurement_status="OK",
        ),
    )
    _push_online(
        registry,
        "edge_02",
        _resources(
            cpu_utilization_percent=60.0,
            cpu_measurement_status="OK",
            queue_length=3,
            queue_measurement_status="OK",
        ),
    )
    # Registry 保存的字段名与决策层读取的字段名一致
    stored_a = registry._nodes["edge_01"].report["resources"]
    stored_b = registry._nodes["edge_02"].report["resources"]
    assert stored_a["cpu_measurement_status"] == "FAILED"
    assert stored_a["queue_measurement_status"] == "OK"
    assert stored_b["cpu_measurement_status"] == "OK"

    scheduler = AssignmentScheduler(  # type: ignore[arg-type]
        registry,
        TaskRepository(None),
    )
    request = _assignment_request(sender_id="sender_01")

    ranked = scheduler._rank_candidates(
        request, deadline=time.monotonic() + 5.0  # type: ignore[arg-type]
    )

    ranked_ids = {item.state.config.edge_node_id for item in ranked}
    assert "edge_01" not in ranked_ids
    assert "edge_02" in ranked_ids


def test_edge1_t5_queue_failed_cross_layer_candidate_excluded() -> None:
    """EDGE-1 T5：Edge report queue=0 + FAILED 经 update_status→registry→rank 被排除。

    即使 queue_length=0（数值上像空闲），只要 queue_measurement_status=FAILED，
    _measurement_gate 必须把它排除，不能因为 queue=0 获得空闲优势。
    """
    registry = _registry("edge_01", "edge_02")
    _push_online(
        registry,
        "edge_01",
        _resources(
            cpu_utilization_percent=0.0,
            cpu_measurement_status="OK",
            queue_length=0,
            # EDGE-1：无 Queue Provider → FAILED（不再是 0+OK）。
            queue_measurement_status="FAILED",
        ),
    )
    _push_online(
        registry,
        "edge_02",
        _resources(
            cpu_utilization_percent=0.0,
            cpu_measurement_status="OK",
            queue_length=3,
            queue_measurement_status="OK",
        ),
    )

    stored_a = registry._nodes["edge_01"].report["resources"]
    assert stored_a["queue_length"] == 0
    assert stored_a["queue_measurement_status"] == "FAILED"
    assert _measurement_gate(stored_a) == "queue_measurement_failed"

    scheduler = AssignmentScheduler(  # type: ignore[arg-type]
        registry,
        TaskRepository(None),
    )
    request = _assignment_request(sender_id="sender_01")
    ranked = scheduler._rank_candidates(
        request, deadline=time.monotonic() + 5.0  # type: ignore[arg-type]
    )

    ranked_ids = {item.state.config.edge_node_id for item in ranked}
    assert "edge_01" not in ranked_ids
    assert "edge_02" in ranked_ids


def test_memory_degraded_uses_neutral_score_and_not_hard_gated() -> None:
    """EDGE-3 T5+T6: memory=0 + DEGRADED → 内存分量取中性 50，不被 512MB 硬门控剔除。"""
    degraded = _compute_base_score(
        _resources(memory_available_mb=0.0, memory_measurement_status="DEGRADED")
    )
    # cpu=0→40, queue=0→30, 内存中性 50→15 => 85
    memory_component = (degraded - 40.0 - 30.0) / 0.30
    assert abs(memory_component - NEUTRAL_COMPONENT_SCORE) < 1e-9
    assert _measurement_gate(
        _resources(memory_available_mb=0.0, memory_measurement_status="DEGRADED")
    ) is None


def test_memory_missing_status_keeps_old_formula() -> None:
    """EDGE-3 T5: 无 memory_measurement_status → 完全使用旧内存公式（0 视为真实 0）。"""
    score = _compute_base_score(_resources(memory_available_mb=0.0))
    assert score == 70.0  # cpu=0→40, mem=0→0, queue=0→30
    assert _measurement_gate(_resources(memory_available_mb=0.0)) is None
    # 旧报告会在候选阶段被 512MB 硬门控排除（SDG 误伤根因），但 base_score 本身仍用旧公式
    assert score == 70.0


def test_memory_failed_neutral_and_gated() -> None:
    """EDGE-3 T7: memory=0 + FAILED → 内存分量中性，且被 measurement gate 排除。"""
    failed = _compute_base_score(
        _resources(memory_available_mb=0.0, memory_measurement_status="FAILED")
    )
    memory_component = (failed - 40.0 - 30.0) / 0.30
    assert abs(memory_component - NEUTRAL_COMPONENT_SCORE) < 1e-9
    assert (
        _measurement_gate(
            _resources(memory_available_mb=0.0, memory_measurement_status="FAILED")
        )
        == "memory_measurement_failed"
    )


def test_memory_telemetry_failure_not_treated_as_real_low_memory() -> None:
    """EDGE-3 T6 核心回归: DEGRADED 的 0MiB 不触发硬门控，旧报告仍被 512MB 门控。"""
    registry = _registry("edge_01", "edge_02")
    _push_online(
        registry,
        "edge_01",
        _resources(memory_available_mb=0.0, memory_measurement_status="DEGRADED"),
    )
    _push_online(
        registry,
        "edge_02",
        _resources(memory_available_mb=0.0),  # 旧报告，无 memory status
    )
    scheduler = AssignmentScheduler(  # type: ignore[arg-type]
        registry,
        TaskRepository(None),
    )
    request = _assignment_request(sender_id="sender_01")

    ranked = scheduler._rank_candidates(
        request, deadline=time.monotonic() + 5.0  # type: ignore[arg-type]
    )

    ranked_ids = {item.state.config.edge_node_id for item in ranked}
    # EDGE-3: 遥测失败(DEGRADED)的 0MiB 是 fallback，不被硬门控误杀
    assert "edge_01" in ranked_ids
    # 旧报告语义保持不变：memory=0 无 status → 仍被 <512MB 门控
    assert "edge_02" not in ranked_ids


def _assignment_request(*, sender_id: str) -> dict:
    return {
        "device_id": "machine_01",
        "sender_id": sender_id,
        "task_id": f"sd_{sender_id.split('_')[1]}_tk_0001",
        "bearing_id": "bearing_01",
        "packet_size_bytes": 1024,
        "expected_packet_count": 80,
        "expected_duration_ms": 1000,
        "created_timestamp_ns": 1,
    }