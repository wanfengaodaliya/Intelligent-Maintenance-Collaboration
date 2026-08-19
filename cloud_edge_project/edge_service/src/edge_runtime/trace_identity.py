# -*- coding: utf-8 -*-
"""Unified trace identity fields for edge outbound business payloads.

阶段 4：所有跨模块出站载荷统一携带 trace_id / task_id / decision_round_id /
edge_node_id / route_id，使一次任务能够跨 Sender、Scheduler、Cloud 与 Edge 追踪。

trace_id 从 task_id 确定性派生（`trace-{task_id}`），保证重复发布、重试或重启后
同一任务产生相同 trace_id，下游无需额外传递协议即可对齐。
"""

from __future__ import annotations

from typing import Any, Mapping


TRACE_FIELDS = (
    "trace_id",
    "task_id",
    "decision_round_id",
    "edge_node_id",
    "route_id",
)


def trace_id_for_task(task_id: str) -> str:
    """Deterministic trace id derived from the task id."""
    return f"trace-{task_id}"


def build_trace_identity(
    *,
    edge_node_id: str,
    task_id: str | None,
    decision_round_id: str | None,
    route_id: str,
) -> dict[str, str | None]:
    """Build the unified identity block attached to outbound payloads."""
    return {
        "trace_id": trace_id_for_task(task_id) if task_id else None,
        "task_id": task_id,
        "decision_round_id": decision_round_id,
        "edge_node_id": edge_node_id,
        "route_id": route_id,
    }


def with_trace_identity(
    payload: Mapping[str, Any],
    *,
    edge_node_id: str,
    route_id: str,
) -> dict[str, Any]:
    """Return a payload copy enriched with the unified identity fields.

    task_id / decision_round_id 取自业务载荷自身；已存在的 trace_id 不覆盖，
    以便上游显式指定的追踪标识优先。
    """
    enriched = dict(payload)
    identity = build_trace_identity(
        edge_node_id=edge_node_id,
        task_id=payload.get("task_id"),
        decision_round_id=payload.get("decision_round_id"),
        route_id=route_id,
    )
    for field, value in identity.items():
        if field == "trace_id" and enriched.get(field):
            continue
        enriched[field] = value
    return enriched
