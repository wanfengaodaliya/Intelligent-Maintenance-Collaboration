"""Minimal V0.1 rules for documented edge/cloud scheduling decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ScheduleDecision:
    """The six public fields defined by the V0.1 interface document."""

    task_id: str
    route: str
    target_node: str
    reason: str
    estimated_total_latency_ms: float
    upload_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "route": self.route,
            "target_node": self.target_node,
            "reason": self.reason,
            "estimated_total_latency_ms": self.estimated_total_latency_ms,
            "upload_required": self.upload_required,
        }


class PreDDPGScheduler:
    """Compatibility facade that exposes the V0.1 deterministic rules."""

    def decide(self, request: Mapping[str, Any]) -> ScheduleDecision:
        return ScheduleDecision(**decide_schedule(request))


def decide_schedule(request: Mapping[str, Any]) -> dict[str, Any]:
    """Apply V0.1 rules: unavailable cloud, low confidence, then edge."""

    task = _mapping(request.get("task"))
    edge_result = _mapping(request.get("edge_result"))
    network_state = _mapping(request.get("network_state"))

    task_id = str(task.get("task_id", ""))
    edge_latency_ms = _number(edge_result.get("edge_latency_ms"), 0.0)
    source_node = str(task.get("source_node") or "edge_1")

    if not bool(network_state.get("cloud_available", True)):
        return ScheduleDecision(
            task_id=task_id,
            route="fallback_edge",
            target_node=source_node,
            reason="cloud_available is false; use edge fallback",
            estimated_total_latency_ms=edge_latency_ms,
            upload_required=False,
        ).to_dict()

    confidence = _number(edge_result.get("confidence"), 0.0)
    if confidence < 0.80:
        return ScheduleDecision(
            task_id=task_id,
            route="cloud",
            target_node="cloud_1",
            reason="edge confidence is below 0.80",
            estimated_total_latency_ms=_cloud_latency(task, edge_result, network_state),
            upload_required=True,
        ).to_dict()

    return ScheduleDecision(
        task_id=task_id,
        route="edge",
        target_node=source_node,
        reason="edge confidence is at least 0.80",
        estimated_total_latency_ms=edge_latency_ms,
        upload_required=False,
    ).to_dict()


def _cloud_latency(
    task: Mapping[str, Any],
    edge_result: Mapping[str, Any],
    network_state: Mapping[str, Any],
) -> float:
    edge_latency_ms = _number(edge_result.get("edge_latency_ms"), 0.0)
    network_latency_ms = _number(network_state.get("latency_ms"), 0.0)
    bandwidth_mbps = max(_number(network_state.get("bandwidth_mbps"), 1.0), 0.1)
    data_size_kb = _number(task.get("data_size_kb"), 0.0)
    transfer_ms = data_size_kb * 8.0 / 1024.0 / bandwidth_mbps * 1000.0
    return round(edge_latency_ms + network_latency_ms + transfer_ms + 45.0, 3)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
