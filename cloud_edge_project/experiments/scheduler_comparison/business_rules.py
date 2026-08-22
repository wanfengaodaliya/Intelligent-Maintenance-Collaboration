"""Non-bypassable business masks shared by every policy (R0 and P2)."""

# 该模块实现业务规则优先的合法动作掩码：两组策略共用，不参与学习。
# 定义 P1 调度的业务合法动作与云端条件规则。

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    DecisionLevel,
    IllegalPolicyAction,
    PolicyDecision,
    RouteAction,
    SchedulerContext,
)

# 冻结阈值：与 configs/local.yaml 当前固定规则完全一致。
CONFIDENCE_THRESHOLD = 0.80
COMPLEXITY_LIMIT = 0.5  # task_complexity = 1 - confidence，边界值不算「低于」
# v2 放宽掩码：本地定稿的放宽置信下界（严格阈值 0.80 之下），
# 放宽带内 LOCAL_FINAL 进入合法集，但需满足放宽条件，否则记安全违规。
LOCAL_RELAXED_THRESHOLD = 0.65
MAX_CLOUD_QUEUE_LENGTH = 5
MIN_UPLINK_MBPS = 2.0
MAX_RTT_P95_MS = 100.0
MAX_LOSS_RATE = 0.10
STATUS_TTL_MS = 5_000.0

LOCAL_FINAL = RouteAction.LOCAL_FINAL
CLOUD_NOW = RouteAction.CLOUD_NOW
PROVISIONAL_DEFER = RouteAction.PROVISIONAL_DEFER


@dataclass(frozen=True)
class MaskResult:
    allowed: frozenset[RouteAction]
    local_reasons: tuple[str, ...]
    condition_reasons: tuple[str, ...]


def local_final_allowed_packet(confidence: float | None) -> tuple[bool, tuple[str, ...]]:
    """包级本地直接定稿条件：confidence > 0.8 且 (1-confidence) < 0.5。"""
    if confidence is None:
        return False, ("EDGE_OUTPUT_MISSING",)
    reasons: list[str] = []
    if not confidence > CONFIDENCE_THRESHOLD:
        reasons.append("LOW_CONFIDENCE")
    if not (1.0 - confidence) < COMPLEXITY_LIMIT:
        reasons.append("HIGH_COMPLEXITY")
    return not reasons, tuple(reasons)


def local_final_allowed_device(
    conflict: bool,
    aggregate_confidence: float | None,
) -> tuple[bool, tuple[str, ...]]:
    """设备级本地定稿条件：无冲突且 aggregate_confidence > 0.8 且复杂度 < 0.5。"""
    if aggregate_confidence is None:
        return False, ("AGGREGATE_CONFIDENCE_MISSING",)
    reasons: list[str] = []
    if conflict:
        reasons.append("RESULT_CONFLICT")
    if not aggregate_confidence > CONFIDENCE_THRESHOLD:
        reasons.append("LOW_AGGREGATE_CONFIDENCE")
    if not (1.0 - aggregate_confidence) < COMPLEXITY_LIMIT:
        reasons.append("HIGH_COMPLEXITY")
    return not reasons, tuple(reasons)


def local_final_relaxed_packet(confidence: float | None) -> tuple[bool, tuple[str, ...]]:
    """v2 包级放宽条件：confidence ≥ 0.65 且 (1-confidence) < 0.5（含严格条件情形）。"""
    if confidence is None:
        return False, ("EDGE_OUTPUT_MISSING",)
    reasons: list[str] = []
    if not confidence >= LOCAL_RELAXED_THRESHOLD:
        reasons.append("BELOW_RELAXED_THRESHOLD")
    if not (1.0 - confidence) < COMPLEXITY_LIMIT:
        reasons.append("HIGH_COMPLEXITY")
    return not reasons, tuple(reasons)


def local_final_relaxed_device(
    conflict: bool,
    aggregate_confidence: float | None,
) -> tuple[bool, tuple[str, ...]]:
    """v2 设备级放宽条件：无冲突且 aggregate_confidence ≥ 0.65 且复杂度 < 0.5。"""
    if aggregate_confidence is None:
        return False, ("AGGREGATE_CONFIDENCE_MISSING",)
    reasons: list[str] = []
    if conflict:
        reasons.append("RESULT_CONFLICT")
    if not aggregate_confidence >= LOCAL_RELAXED_THRESHOLD:
        reasons.append("BELOW_RELAXED_THRESHOLD")
    if not (1.0 - aggregate_confidence) < COMPLEXITY_LIMIT:
        reasons.append("HIGH_COMPLEXITY")
    return not reasons, tuple(reasons)


def cloud_condition_reasons_raw(
    *,
    cloud_online: bool,
    cloud_status_age_ms: float,
    queue_length: int,
    cloud_model_loaded: bool,
    uplink_mbps: float,
    rtt_p95_ms: float,
    loss_rate: float,
) -> tuple[str, ...]:
    """立即上云的可用性判定，语义与现有路由器 _condition_reasons 一致。"""
    reasons: list[str] = []
    if not cloud_online:
        reasons.append("CLOUD_OFFLINE")
    else:
        if cloud_status_age_ms > STATUS_TTL_MS:
            reasons.append("STATUS_STALE")
        if queue_length > MAX_CLOUD_QUEUE_LENGTH:
            reasons.append("CLOUD_OVERLOADED")
        if not cloud_model_loaded:
            reasons.append("MODEL_NOT_READY")
    if (
        uplink_mbps < MIN_UPLINK_MBPS
        or rtt_p95_ms > MAX_RTT_P95_MS
        or loss_rate > MAX_LOSS_RATE
    ):
        reasons.append("NETWORK_POOR")
    return tuple(reasons)


def cloud_condition_reasons(context: SchedulerContext) -> tuple[str, ...]:
    """基于决策上下文的可用性判定（委托原始参数版本）。"""
    return cloud_condition_reasons_raw(
        cloud_online=context.cloud_online,
        cloud_status_age_ms=context.cloud_status_age_ms,
        queue_length=context.queue_length,
        cloud_model_loaded=context.cloud_model_loaded,
        uplink_mbps=context.uplink_mbps,
        rtt_p95_ms=context.rtt_p95_ms,
        loss_rate=context.loss_rate,
    )


def legal_actions(context: SchedulerContext) -> MaskResult:
    """业务掩码（v2 放宽版）：严格本地定稿 → 放宽带 → 上云资格，逐档判定。

    - confidence > 0.8：{LOCAL_FINAL}（与 v1 相同）；
    - 0.65 ≤ confidence ≤ 0.8：LOCAL_FINAL 进入合法集，云可用时加入 CLOUD_NOW；
    - confidence < 0.65：与 v1 相同，LOCAL_FINAL 保持禁止。
    """
    if context.decision_level is DecisionLevel.PACKET:
        strict_ok, local_reasons = local_final_allowed_packet(context.confidence)
        relaxed_ok, _ = local_final_relaxed_packet(context.confidence)
    else:
        strict_ok, local_reasons = local_final_allowed_device(
            context.conflict, context.aggregate_confidence
        )
        relaxed_ok, _ = local_final_relaxed_device(
            context.conflict, context.aggregate_confidence
        )
    if strict_ok:
        return MaskResult(frozenset({LOCAL_FINAL}), (), ())
    condition_reasons = cloud_condition_reasons(context)
    if relaxed_ok:
        actions = {LOCAL_FINAL, PROVISIONAL_DEFER}
        if not condition_reasons:
            actions.add(CLOUD_NOW)
        return MaskResult(frozenset(actions), local_reasons, condition_reasons)
    if condition_reasons:
        return MaskResult(frozenset({PROVISIONAL_DEFER}), local_reasons, condition_reasons)
    return MaskResult(
        frozenset({CLOUD_NOW, PROVISIONAL_DEFER}), local_reasons, ()
    )


def validate_decision(
    context: SchedulerContext,
    allowed: frozenset[RouteAction],
    decision: PolicyDecision,
) -> None:
    """策略传回非法动作时必须抛出 IllegalPolicyAction，不能静默接受。"""
    if decision.action not in allowed:
        raise IllegalPolicyAction(
            f"policy {decision.policy_id} returned illegal action "
            f"{decision.action.value} for {context.decision_id}",
            context_id=context.decision_id,
            action=decision.action.value,
        )
    if decision.action is LOCAL_FINAL:
        # v2：LOCAL_FINAL 合法性按放宽条件校验；放宽阈值之下执行即 UNSAFE_LOCAL_FINAL。
        if context.decision_level is DecisionLevel.PACKET:
            ok, _ = local_final_relaxed_packet(context.confidence)
        else:
            ok, _ = local_final_relaxed_device(
                context.conflict, context.aggregate_confidence
            )
        if not ok:
            raise IllegalPolicyAction(
                f"UNSAFE_LOCAL_FINAL: LOCAL_FINAL violates relaxed business rules "
                f"for {context.decision_id}",
                context_id=context.decision_id,
                action=decision.action.value,
            )
    if decision.action is CLOUD_NOW and cloud_condition_reasons(context):
        raise IllegalPolicyAction(
            f"CLOUD_NOW violates availability rules for {context.decision_id}",
            context_id=context.decision_id,
            action=decision.action.value,
        )
