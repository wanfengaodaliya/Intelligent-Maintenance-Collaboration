"""R0 baseline: adapts the observable behaviour of the fixed-rule routers."""

# R0 不学习；适配现有 packet_router / device_router 的可观察行为：
# 满足本地定稿条件 → LOCAL_FINAL；云端条件满足 → CLOUD_NOW；否则 PROVISIONAL_DEFER。
# 实现 P1 LinUCB 使用的 R0 基线决策与回退策略。

from __future__ import annotations

import time

from ..business_rules import legal_actions
from ..contracts import (
    DecisionLevel,
    DecisionOutcome,
    PolicyDecision,
    RouteAction,
    SchedulerContext,
)

R0_POLICY_ID = "R0"
R0_POLICY_VERSION = "1.0.0"

_TIE_ORDER = {
    RouteAction.LOCAL_FINAL: 0,
    RouteAction.CLOUD_NOW: 1,
    RouteAction.PROVISIONAL_DEFER: 2,
}


def r0_reason_codes(
    context: SchedulerContext,
    allowed: frozenset[RouteAction],
    local_reasons: tuple[str, ...],
    condition_reasons: tuple[str, ...],
    action: RouteAction,
) -> tuple[str, ...]:
    """与现有路由器输出语义一致的理由码。"""
    if action is RouteAction.LOCAL_FINAL:
        if context.decision_level is DecisionLevel.PACKET:
            return ("HIGH_CONFIDENCE", "LOW_COMPLEXITY")
        return ("NO_CONFLICT", "HIGH_AGGREGATE_CONFIDENCE", "LOW_COMPLEXITY")
    trigger = tuple(local_reasons) or ("EDGE_OUTPUT_MISSING",)
    if action is RouteAction.CLOUD_NOW:
        return trigger
    return trigger + tuple(condition_reasons)


def decide_r0(
    context: SchedulerContext,
    allowed: frozenset[RouteAction],
) -> PolicyDecision:
    """纯函数形式的 R0 决策，供基线与 P2 回退路径共用，保证逐位一致。"""
    mask = legal_actions(context)
    if set(allowed) != set(mask.allowed):
        raise ValueError("R0 received a mask that differs from the business mask")
    start = time.perf_counter_ns()
    if len(allowed) == 1:
        action = next(iter(allowed))
    else:
        candidates = sorted(allowed, key=lambda item: _TIE_ORDER[item])
        # v2 放宽带会把 LOCAL_FINAL 加入合法集；固定规则从不在此带本地定稿，
        # 只有当它是唯一合法动作（严格条件，conf>0.8）时才选它，保证与 v1 逐位一致。
        if len(candidates) > 1:
            candidates = [item for item in candidates if item is not RouteAction.LOCAL_FINAL]
        # 固定规则：条件满足即立即上云，否则延后。
        action = RouteAction.CLOUD_NOW if RouteAction.CLOUD_NOW in candidates else candidates[0]
    reason_codes = r0_reason_codes(
        context, allowed, mask.local_reasons, mask.condition_reasons, action
    )
    scores = {
        item.value: {"rule_order": float(_TIE_ORDER[item])} for item in sorted(allowed, key=lambda i: _TIE_ORDER[i])
    }
    duration = time.perf_counter_ns() - start
    return PolicyDecision(
        action=action,
        policy_id=R0_POLICY_ID,
        policy_version=R0_POLICY_VERSION,
        reason_codes=reason_codes,
        scores=scores,
        selection_probability=1.0,
        decision_duration_ns=duration,
        fallback=False,
    )


class BaselinePolicy:
    """组 A：现有固定规则基线。"""

    policy_id = R0_POLICY_ID
    policy_version = R0_POLICY_VERSION

    def __init__(self) -> None:
        self._seed = 0

    def reset(self, seed: int) -> None:
        self._seed = seed

    def decide(
        self,
        context: SchedulerContext,
        allowed_actions: frozenset[RouteAction],
    ) -> PolicyDecision:
        return decide_r0(context, allowed_actions)

    def observe(self, outcome: DecisionOutcome) -> None:
        # R0 不学习，结算反馈仅用于统计。
        return None
