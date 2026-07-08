#最核心的文件，真正决定任务走 edge、cloud 还是 fallback_edge
"""Minimal PER-DDPG-style rule scheduler for edge/cloud task decisions.

The paper's PER-DDPG scheduler learns an Actor policy from task, network, and
node state. This first runnable version keeps that state shape but replaces the
trained Actor with deterministic rules and scores. Fog routing is intentionally
disabled for the current edge/cloud-only system.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ScheduleDecision:
    """Public scheduling result matching the project interface document."""

    task_id: str
    route: str          #调度路径
    target_node: str    #目标节点
    reason: str         #为什么这么调度
    estimated_total_latency_ms: float   #预计总时延
    upload_required: bool   #是否需要上传
    scheduler: str = "PER-DDPG-rule-minimal"
    policy_score: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreDDPGScheduler:
    """Decide whether a task stays on edge or is uploaded to cloud."""

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.80,     #边缘置信度低于 0.8，倾向上云
        max_packet_loss: float = 0.10,          #丢包率高于 0.10，不上云
        min_bandwidth_mbps: float = 2.0,        #带宽低于 2 Mbps，不上云
        cloud_score_threshold: float = 0.50,    #云端规则分数高于 0.50，且云端更快，可以上云
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.max_packet_loss = max_packet_loss
        self.min_bandwidth_mbps = min_bandwidth_mbps
        self.cloud_score_threshold = cloud_score_threshold

    # 主决策函数
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

        #cloud_available = false → fallback_edge
        if not cloud_available:
            return self._edge_decision(
                task,
                route="fallback_edge",
                reason="cloud_available is false; use edge fallback",
                estimated_total_latency_ms=estimates["edge"],
                policy_score=policy_score,
            )

        #packet_loss 太高 → fallback_edge
        if packet_loss > self.max_packet_loss:
            return self._edge_decision(
                task,
                route="fallback_edge",
                reason=f"packet_loss {packet_loss:.2f} is too high for cloud upload",
                estimated_total_latency_ms=estimates["edge"],
                policy_score=policy_score,
            )

        #bandwidth 太低 → fallback_edge
        if bandwidth_mbps < self.min_bandwidth_mbps:
            return self._edge_decision(
                task,
                route="fallback_edge",
                reason=f"bandwidth {bandwidth_mbps:.2f} Mbps is below upload threshold",
                estimated_total_latency_ms=estimates["edge"],
                policy_score=policy_score,
            )

        #confidence < 0.8 → cloud
        if confidence < self.confidence_threshold:
            return self._cloud_decision(
                task,
                reason=f"edge confidence {confidence:.2f} is below {self.confidence_threshold:.2f}",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )

        #need_cloud = true → cloud
        if need_cloud:
            return self._cloud_decision(
                task,
                reason="edge model set need_cloud to true",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )

        #规则分数支持 cloud 且 cloud 更快 → cloud
        if policy_score["cloud"] >= self.cloud_score_threshold and estimates["cloud"] <= estimates["edge"]:
            return self._cloud_decision(
                task,
                reason=f"PER-DDPG rule score favors cloud ({policy_score['cloud']:.2f})",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )

        #边缘预计超 deadline 且 cloud 更快 → cloud
        if estimates["edge"] > estimates["deadline"] and estimates["cloud"] < estimates["edge"]:
            return self._cloud_decision(
                task,
                reason="estimated edge latency exceeds deadline and cloud is faster",
                estimated_total_latency_ms=estimates["cloud"],
                policy_score=policy_score,
            )

        #否则 → edge
        return self._edge_decision(
            task,
            route="edge",
            reason="edge confidence and latency satisfy scheduler policy",
            estimated_total_latency_ms=estimates["edge"],
            policy_score=policy_score,
        )

    # 估算边缘和云端的预计总时延
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

        transfer_ms = network_latency + (data_size_kb * 8.0 / 1024.0 / max(bandwidth_mbps, 0.1) * 1000.0)
        edge_pressure = 1.0 + 0.35 * edge_cpu + 0.20 * edge_memory
        packet_loss_penalty = 1.0 + min(packet_loss, 0.5)
        cloud_compute_ms = 45.0 + cloud_queue * 8.0

        return {
            #edge_latency_ms × 节点压力系数
            "edge": round(edge_latency * edge_pressure, 3),
            #边缘已耗时 + 网络传输时延 + 云端计算/排队时延
            "cloud": round(edge_latency + transfer_ms * packet_loss_penalty + cloud_compute_ms, 3),
            "deadline": round(deadline, 3),
        }

    #模拟 PER-DDPG Actor 网络输出的地方
    def _actor_scores(
        self,
        task: Mapping[str, Any],
        edge_result: Mapping[str, Any],
        network_state: Mapping[str, Any],
        node_state: Mapping[str, Any],
        estimates: Mapping[str, float],
    ) -> dict[str, float]:
        """Deterministic stand-in for the PER-DDPG Actor output."""

        #confidence是否低
        #need_cloud是否为true
        #priority是否高
        #边缘 / 内存压力
        #带宽是否好
        #是否超过deadline
        #数据量是否太大
        #丢包率是否高
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
        if isinstance(value, Mapping):
            return value
        return {}

    @staticmethod
    def _float(value: Any, *, default: float | None) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


def decide_schedule(request: Mapping[str, Any]) -> dict[str, Any]:
    """Convenience function used by tests or non-HTTP callers."""

    return PreDDPGScheduler().decide(request).to_dict()
