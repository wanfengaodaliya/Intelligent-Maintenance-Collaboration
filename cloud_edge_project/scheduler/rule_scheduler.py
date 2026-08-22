#最核心的文件，真正决定任务走 edge、cloud 还是 fallback_edge
# 该模块同时承载 P1 策略接入与 R0 固定规则回退。
"""Legacy PER-DDPG rules plus the separate documented V0.1 projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ScheduleDecision:
    """Legacy scheduling result, including internal policy details."""

    task_id: str
    route: str
    target_node: str
    reason: str
    estimated_total_latency_ms: float
    upload_required: bool
    scheduler: str = "PER-DDPG-rule-minimal"
    policy_score: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreDDPGScheduler:
    """Decide whether a legacy task stays on edge or is uploaded to cloud."""

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.80,
        max_packet_loss: float = 0.10,
        min_bandwidth_mbps: float = 2.0,
        cloud_score_threshold: float = 0.50,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.max_packet_loss = max_packet_loss
        self.min_bandwidth_mbps = min_bandwidth_mbps
        self.cloud_score_threshold = cloud_score_threshold

    def decide(self, request: Mapping[str, Any]) -> ScheduleDecision:
        task = self._mapping(request.get("task"))
        edge_result = self._mapping(request.get("edge_result"))
        network_state = self._mapping(request.get("network_state"))
        node_state = self._mapping(request.get("node_state"))

        confidence = self._float(edge_result.get("confidence"), default=0.0) or 0.0
        need_cloud = bool(edge_result.get("need_cloud", False))
        cloud_available = bool(network_state.get("cloud_available", True))
        packet_loss = self._float(network_state.get("packet_loss"), default=0.0) or 0.0
        bandwidth_mbps = self._float(network_state.get("bandwidth_mbps"), default=0.0) or 0.0

        estimates = self._estimate_latency(task, edge_result, network_state, node_state)
        policy_score = self._actor_scores(task, edge_result, network_state, node_state, estimates)

        if not cloud_available:
            return self._edge_decision(
                task,
                route="fallback_edge",
                reason="cloud_available is false; use edge fallback",
                estimated_total_latency_ms=estimates["edge"],
                policy_score=policy_score,
            )
        if packet_loss > self.max_packet_loss:
            return self._edge_decision(
                task,
                route="fallback_edge",
                reason=f"packet_loss {packet_loss:.2f} is too high for cloud upload",
                estimated_total_latency_ms=estimates["edge"],
                policy_score=policy_score,
            )
        if bandwidth_mbps < self.min_bandwidth_mbps:
            return self._edge_decision(
                task,
                route="fallback_edge",
                reason=f"bandwidth {bandwidth_mbps:.2f} Mbps is below upload threshold",
                estimated_total_latency_ms=estimates["edge"],
                policy_score=policy_score,
            )
        if confidence < self.confidence_threshold:
            return self._cloud_decision(
                task,
                reason=f"edge confidence {confidence:.2f} is below {self.confidence_threshold:.2f}",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )
        if need_cloud:
            return self._cloud_decision(
                task,
                reason="edge model set need_cloud to true",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )
        if policy_score["cloud"] >= self.cloud_score_threshold and estimates["cloud"] <= estimates["edge"]:
            return self._cloud_decision(
                task,
                reason=f"PER-DDPG rule score favors cloud ({policy_score['cloud']:.2f})",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )
        if estimates["edge"] > estimates["deadline"] and estimates["cloud"] < estimates["edge"]:
            return self._cloud_decision(
                task,
                reason="estimated edge latency exceeds deadline and cloud is faster",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )
        return self._edge_decision(
            task,
            route="edge",
            reason="edge confidence and latency satisfy scheduler policy",
            estimated_total_latency_ms=estimates["edge"],
            policy_score=policy_score,
        )

    def _estimate_latency(
        self,
        task: Mapping[str, Any],
        edge_result: Mapping[str, Any],
        network_state: Mapping[str, Any],
        node_state: Mapping[str, Any],
    ) -> dict[str, float]:
        edge_latency = self._float(edge_result.get("edge_latency_ms"), default=0.0) or 0.0
        data_size_kb = self._float(task.get("data_size_kb"), default=0.0) or 0.0
        deadline = self._float(task.get("deadline_ms"), default=200.0) or 200.0
        network_latency = self._float(network_state.get("latency_ms"), default=0.0) or 0.0
        bandwidth_mbps = self._float(network_state.get("bandwidth_mbps"), default=1.0) or 1.0
        packet_loss = self._float(network_state.get("packet_loss"), default=0.0) or 0.0
        edge_cpu = self._float(node_state.get("edge_cpu_usage"), default=0.0) or 0.0
        edge_memory = self._float(node_state.get("edge_memory_usage"), default=0.0) or 0.0
        cloud_queue = self._float(node_state.get("cloud_queue_length"), default=0.0) or 0.0

        transfer_ms = network_latency + (
            data_size_kb * 8.0 / 1024.0 / max(bandwidth_mbps, 0.1) * 1000.0
        )
        edge_pressure = 1.0 + 0.35 * edge_cpu + 0.20 * edge_memory
        packet_loss_penalty = 1.0 + min(packet_loss, 0.5)
        cloud_compute_ms = 45.0 + cloud_queue * 8.0

        return {
            "edge": round(edge_latency * edge_pressure, 3),
            "cloud": round(edge_latency + transfer_ms * packet_loss_penalty + cloud_compute_ms, 3),
            "deadline": round(deadline, 3),
        }

    def _actor_scores(
        self,
        task: Mapping[str, Any],
        edge_result: Mapping[str, Any],
        network_state: Mapping[str, Any],
        node_state: Mapping[str, Any],
        estimates: Mapping[str, float],
    ) -> dict[str, float]:
        confidence = self._float(edge_result.get("confidence"), default=0.0) or 0.0
        need_cloud = bool(edge_result.get("need_cloud", False))
        priority = self._float(task.get("priority"), default=0.0) or 0.0
        data_size_kb = self._float(task.get("data_size_kb"), default=0.0) or 0.0
        packet_loss = self._float(network_state.get("packet_loss"), default=0.0) or 0.0
        bandwidth_mbps = self._float(network_state.get("bandwidth_mbps"), default=1.0) or 1.0
        edge_cpu = self._float(node_state.get("edge_cpu_usage"), default=0.0) or 0.0
        edge_memory = self._float(node_state.get("edge_memory_usage"), default=0.0) or 0.0

        confidence_gap = max(0.0, self.confidence_threshold - confidence) / self.confidence_threshold
        edge_pressure = min((edge_cpu + edge_memory) / 2.0, 1.0)
        data_pressure = min(data_size_kb / 1024.0, 1.0)
        bandwidth_score = min(bandwidth_mbps / 20.0, 1.0)
        deadline_pressure = 1.0 if estimates["edge"] > estimates["deadline"] else 0.0

        cloud_score = (
            0.26 * confidence_gap
            + 0.20 * float(need_cloud)
            + 0.16 * priority
            + 0.14 * edge_pressure
            + 0.10 * bandwidth_score
            + 0.08 * deadline_pressure
            - 0.08 * data_pressure
            - 0.12 * min(packet_loss / self.max_packet_loss, 1.0)
        )
        cloud_score = min(max(cloud_score, 0.0), 1.0)
        return {"edge": round(1.0 - cloud_score, 4), "cloud": round(cloud_score, 4)}

    def _cloud_decision(
        self,
        task: Mapping[str, Any],
        *,
        reason: str,
        estimated_total_latency_ms: float,
        policy_score: dict[str, float],
    ) -> ScheduleDecision:
        return self._decision(
            task,
            route="cloud",
            target_node="cloud_1",
            reason=reason,
            estimated_total_latency_ms=estimated_total_latency_ms,
            upload_required=True,
            policy_score=policy_score,
        )

    def _edge_decision(
        self,
        task: Mapping[str, Any],
        *,
        route: str,
        reason: str,
        estimated_total_latency_ms: float,
        policy_score: dict[str, float],
    ) -> ScheduleDecision:
        return self._decision(
            task,
            route=route,
            target_node=str(task.get("source_node") or "edge_1"),
            reason=reason,
            estimated_total_latency_ms=estimated_total_latency_ms,
            upload_required=False,
            policy_score=policy_score,
        )

    def _decision(
        self,
        task: Mapping[str, Any],
        *,
        route: str,
        target_node: str,
        reason: str,
        estimated_total_latency_ms: float,
        upload_required: bool,
        policy_score: dict[str, float],
    ) -> ScheduleDecision:
        return ScheduleDecision(
            task_id=str(task.get("task_id", "")),
            route=route,
            target_node=target_node,
            reason=reason,
            estimated_total_latency_ms=round(estimated_total_latency_ms, 3),
            upload_required=upload_required,
            policy_score=policy_score,
        )

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _float(value: Any, *, default: float | None) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


def decide_schedule(request: Mapping[str, Any]) -> dict[str, Any]:
    """Run the complete legacy deterministic PreDDPG behavior."""

    return PreDDPGScheduler().decide(request).to_dict()


def decide_schedule_v01(request: Mapping[str, Any]) -> dict[str, Any]:
    """Apply only the three V0.1 rules and return exactly six public fields.

    实验挂钩：SCHEDULER_ROUTING_POLICY=p1 时先尝试 P1 LinUCB 策略；
    P1 返回 None（r0 模式 / 模型不可用 / 异常）时完全回退到固定规则。
    """

    task = _mapping(request.get("task"))
    edge_result = _mapping(request.get("edge_result"))
    network_state = _mapping(request.get("network_state"))
    node_state = _mapping(request.get("node_state"))
    task_id = str(task.get("task_id", ""))
    source_node = str(task.get("source_node") or "edge_1")
    edge_latency_ms = _number(edge_result.get("edge_latency_ms"), 0.0)

    # ---- P1 实验挂钩（默认 r0 模式下直接返回 None，零开销）----
    p1_choice = _p1_hook(task, edge_result, network_state, node_state)
    if p1_choice is not None:
        return _p1_v01_decision(task, edge_result, network_state, p1_choice)

    if not network_state.get("cloud_available"):
        return _v01_decision(
            task_id,
            "fallback_edge",
            source_node,
            "cloud_available is false; use edge fallback",
            edge_latency_ms,
            False,
        )
    if _number(edge_result.get("confidence"), 0.0) < 0.80:
        bandwidth = max(_number(network_state.get("bandwidth_mbps"), 0.0), 0.1)
        transfer_ms = _number(task.get("data_size_kb"), 0.0) * 8.0 / 1024.0 / bandwidth * 1000.0
        cloud_latency = round(
            edge_latency_ms + _number(network_state.get("latency_ms"), 0.0) + transfer_ms + 45.0,
            3,
        )
        return _v01_decision(
            task_id,
            "cloud",
            "cloud_1",
            "edge confidence is below 0.80",
            cloud_latency,
            True,
        )
    return _v01_decision(
        task_id,
        "edge",
        source_node,
        "edge confidence is at least 0.80",
        edge_latency_ms,
        False,
    )


def _v01_decision(
    task_id: str,
    route: str,
    target_node: str,
    reason: str,
    estimated_total_latency_ms: float,
    upload_required: bool,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "route": route,
        "target_node": target_node,
        "reason": reason,
        "estimated_total_latency_ms": estimated_total_latency_ms,
        "upload_required": upload_required,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---- P1 实验挂钩辅助函数 ------------------------------------------------


def _p1_hook(
    task: Mapping[str, Any],
    edge_result: Mapping[str, Any],
    network_state: Mapping[str, Any],
    node_state: Mapping[str, Any],
):
    """调用 P1 适配器；任何异常返回 None（回退固定规则）。"""
    try:
        from .p1_policy_adapter import maybe_choose_v01_route
    except ImportError:
        try:
            from p1_policy_adapter import maybe_choose_v01_route
        except ImportError:
            return None
    try:
        return maybe_choose_v01_route(
            task=task,
            edge_result=edge_result,
            network_state=network_state,
            node_state=node_state,
        )
    except Exception:
        return None


def _p1_v01_decision(
    task: Mapping[str, Any],
    edge_result: Mapping[str, Any],
    network_state: Mapping[str, Any],
    choice,
) -> dict[str, Any]:
    """把 P1 选择转换为 v0.1 六字段决策（与固定规则输出格式一致）。"""
    task_id = str(task.get("task_id", ""))
    source_node = str(task.get("source_node") or "edge_1")
    edge_latency_ms = _number(edge_result.get("edge_latency_ms"), 0.0)
    route = choice.route

    if route == "cloud":
        bandwidth = max(_number(network_state.get("bandwidth_mbps"), 0.0), 0.1)
        transfer_ms = _number(task.get("data_size_kb"), 0.0) * 8.0 / 1024.0 / bandwidth * 1000.0
        latency = round(
            edge_latency_ms + _number(network_state.get("latency_ms"), 0.0) + transfer_ms + 45.0,
            3,
        )
        target_node = "cloud_1"
        upload_required = True
    else:
        latency = edge_latency_ms
        target_node = source_node
        upload_required = False

    reason = "p1:" + ",".join(choice.reason_codes)
    return _v01_decision(task_id, route, target_node, reason, latency, upload_required)
