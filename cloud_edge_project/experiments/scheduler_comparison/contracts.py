"""Unified contracts shared by every scheduling policy in the comparison."""

# 该模块定义统一动作空间、决策上下文、策略决策与结算结果契约。
# 定义 P1 调度策略使用的任务上下文、动作和结果契约。

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class RouteAction(str, Enum):
    """三个统一路由动作，包级与设备级共用。"""

    LOCAL_FINAL = "LOCAL_FINAL"
    CLOUD_NOW = "CLOUD_NOW"
    PROVISIONAL_DEFER = "PROVISIONAL_DEFER"


class DecisionLevel(str, Enum):
    PACKET = "PACKET"
    DEVICE = "DEVICE"


class TerminalState(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PERMANENT_FAILED = "PERMANENT_FAILED"
    EXPIRED = "EXPIRED"


class IllegalPolicyAction(RuntimeError):
    """策略返回了业务掩码之外的动作，或掩码被非法放宽。"""

    def __init__(self, message: str, *, context_id: str = "", action: str = "") -> None:
        super().__init__(message)
        self.context_id = context_id
        self.action = action


@dataclass(frozen=True)
class SchedulerContext:
    """策略可见的全部决策时刻状态（不得包含未来或反事实信息）。"""

    decision_id: str
    device_id: str
    bearing_id: str | None
    packet_id: str | None
    decision_level: DecisionLevel
    confidence: float | None
    aggregate_confidence: float | None
    task_complexity: float | None
    conflict: bool
    queue_length: int
    deferred_queue_length: int
    retry_count: int
    uplink_mbps: float
    rtt_p95_ms: float
    loss_rate: float
    cloud_online: bool
    cloud_model_loaded: bool
    cloud_status_age_ms: float
    created_at_ns: int
    deadline_ns: int
    now_ns: int

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("decision_id is required")
        if not self.device_id:
            raise ValueError("device_id is required")
        if self.decision_level is DecisionLevel.PACKET and not self.packet_id:
            raise ValueError("packet_id is required for packet-level decisions")
        if self.decision_level is DecisionLevel.DEVICE and self.bearing_id is not None:
            raise ValueError("device-level decisions must not bind a single bearing")
        if self.deadline_ns <= self.created_at_ns:
            raise ValueError("deadline_ns must be greater than created_at_ns")
        if self.now_ns < self.created_at_ns:
            raise ValueError("now_ns cannot precede created_at_ns")
        if self.queue_length < 0 or self.deferred_queue_length < 0 or self.retry_count < 0:
            raise ValueError("queue and retry counters cannot be negative")
        for name, value in (
            ("uplink_mbps", self.uplink_mbps),
            ("rtt_p95_ms", self.rtt_p95_ms),
            ("loss_rate", self.loss_rate),
            ("cloud_status_age_ms", self.cloud_status_age_ms),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.loss_rate > 1.0:
            raise ValueError("loss_rate must be within [0, 1]")

    @property
    def remaining_ns(self) -> int:
        return max(0, self.deadline_ns - self.now_ns)

    @property
    def primary_confidence(self) -> float | None:
        if self.decision_level is DecisionLevel.PACKET:
            return self.confidence
        return self.aggregate_confidence


@dataclass(frozen=True)
class PolicyDecision:
    """策略输出：动作、理由码、各合法动作评分与决策耗时。"""

    action: RouteAction
    policy_id: str
    policy_version: str
    reason_codes: tuple[str, ...]
    scores: Mapping[str, Mapping[str, float]]
    selection_probability: float = 1.0
    decision_duration_ns: int = 0
    fallback: bool = False

    def __post_init__(self) -> None:
        if not self.policy_id:
            raise ValueError("policy_id is required")
        if not (0.0 <= self.selection_probability <= 1.0):
            raise ValueError("selection_probability must be within [0, 1]")
        if self.decision_duration_ns < 0:
            raise ValueError("decision_duration_ns cannot be negative")


@dataclass(frozen=True)
class DecisionOutcome:
    """任务终态结算结果，仅在最终结果或永久失败后通过 observe 反馈。"""

    decision_id: str
    level: DecisionLevel
    action: RouteAction
    terminal_state: TerminalState
    created_at_ns: int
    deadline_ns: int
    final_at_ns: int | None
    provisional_at_ns: int | None = None
    attempt_count: int = 1
    fallback: bool = False

    @property
    def success(self) -> bool:
        return self.terminal_state is TerminalState.SUCCEEDED

    @property
    def permanent_failure(self) -> bool:
        return self.terminal_state is TerminalState.PERMANENT_FAILED

    @property
    def on_time(self) -> bool:
        return self.success and self.final_at_ns is not None and self.final_at_ns <= self.deadline_ns

    @property
    def latency_ns(self) -> int | None:
        if not self.success or self.final_at_ns is None:
            return None
        return self.final_at_ns - self.created_at_ns

    def slack_label(self) -> float:
        """时延余量标签：成功为归一化余量，永久失败/过期记为 -1。"""
        span = self.deadline_ns - self.created_at_ns
        if span <= 0:
            return -1.0
        if not self.success or self.final_at_ns is None:
            return -1.0
        raw = (self.deadline_ns - self.final_at_ns) / span
        return max(-1.0, min(1.0, raw))

    def failure_label(self) -> float:
        return 1.0 if self.permanent_failure else 0.0


@dataclass(frozen=True)
class TrainingSample:
    """训练样本：上下文特征向量来源 + 每个合法动作的后果标签。"""

    decision_id: str
    level: DecisionLevel
    context: SchedulerContext
    labels: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
