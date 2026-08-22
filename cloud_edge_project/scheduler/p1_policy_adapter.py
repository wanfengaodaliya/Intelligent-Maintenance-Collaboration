"""Switchable P1 LinUCB routing adapter (experiment copy only).

对照组实验专用：包级路由默认执行冻结 P1 LinUCB；设置环境变量
``SCHEDULER_ROUTING_POLICY=r0`` 时显式回退固定规则。冻结模型目录默认
``<cloud_edge_project>/models/p1``，可用 ``SCHEDULER_P1_MODEL_DIR`` 覆盖。
v2 放宽掩码：confidence >= 0.65 的放宽带内 LOCAL_FINAL 进入合法集；
模型不可用 / 分布外 / 单一合法动作时自动回退 R0 行为。
"""

# P1 适配器负责在 LinUCB 策略与 R0 回退规则之间选择路由。

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

ENV_POLICY = "SCHEDULER_ROUTING_POLICY"
ENV_MODEL_DIR = "SCHEDULER_P1_MODEL_DIR"
# 默认冻结模型目录：<cloud_edge_project>/models/p1（未设置 SCHEDULER_P1_MODEL_DIR 时使用）。
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "p1"
# 生产包级接口不携带截止时间；预测特征用固定剩余时间假设（在训练分布内）。
DEFAULT_REMAINING_MS = 3_000.0
# 训练包络中云状态年龄恒为 400ms（仿真器按上报节拍刷新）；生产接口的实时
# 状态年龄不在训练分布内，按同一训练假设固定化，避免无意义的分布外回退。
DEFAULT_STATUS_AGE_MS = 400.0
# v2 放宽掩码：本地定稿放宽置信下界（严格阈值 0.80 之下），与实验框架同步。
LOCAL_RELAXED_THRESHOLD = 0.65
COMPLEXITY_LIMIT = 0.5

# P1 动作 → v0.1 路由映射
_ACTION_TO_ROUTE = {
    "LOCAL_FINAL": "edge",
    "CLOUD_NOW": "cloud",
    "PROVISIONAL_DEFER": "fallback_edge",
}

_lock = threading.RLock()
_state: dict[str, Any] = {"checkpoint": None, "error": None, "model_dir": None}


@dataclass(frozen=True)
class P1PacketChoice:
    """P1 在合法动作集内的选择结果（v2 放宽带含 LOCAL_FINAL）。"""

    route: str
    reason_codes: tuple[str, ...]
    fallback: bool
    scores: dict[str, Any] = field(default_factory=dict)
    decision_duration_ns: int = 0


def routing_policy_mode() -> str:
    mode = os.getenv(ENV_POLICY, "p1").strip().lower()
    return mode if mode in ("r0", "p1") else "r0"


def _configured_model_dir() -> str:
    """解析模型目录：优先 SCHEDULER_P1_MODEL_DIR，否则用项目内冻结模型。"""
    return os.getenv(ENV_MODEL_DIR, str(DEFAULT_MODEL_DIR)).strip()


def policy_status() -> dict[str, Any]:
    """供 /scheduler/routing-policy 端点输出的可观测状态。"""
    with _lock:
        return {
            "mode": routing_policy_mode(),
            "model_dir": _state["model_dir"],
            "checkpoint_loaded": _state["checkpoint"] is not None,
            "load_error": _state["error"],
            "default_remaining_ms": DEFAULT_REMAINING_MS,
        }


def _load_checkpoint(model_dir: str):
    from pathlib import Path

    from experiments.scheduler_comparison.policies.linucb import LinUCBCheckpoint

    checkpoint_path = Path(model_dir) / "checkpoint.json"
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"frozen checkpoint not found: {checkpoint_path}")
    return LinUCBCheckpoint.load(checkpoint_path)


def _get_checkpoint():
    model_dir = _configured_model_dir()
    with _lock:
        if not model_dir:
            _state["error"] = f"env {ENV_MODEL_DIR} not set"
            _state["checkpoint"] = None
            _state["model_dir"] = None
            return None
        if _state["checkpoint"] is not None and _state["model_dir"] == model_dir:
            return _state["checkpoint"]
        try:
            checkpoint = _load_checkpoint(model_dir)
            _state.update(checkpoint=checkpoint, error=None, model_dir=model_dir)
            return checkpoint
        except Exception as error:  # 加载失败必须安全回退 R0
            _state.update(checkpoint=None, error=str(error), model_dir=model_dir)
            return None


def local_relaxed_ok(confidence: float | None) -> bool:
    """v2 放宽条件：confidence >= 0.65 且 task_complexity < 0.5。"""
    if confidence is None:
        return False
    return confidence >= LOCAL_RELAXED_THRESHOLD and (1.0 - confidence) < COMPLEXITY_LIMIT


def maybe_choose_v01_route(
    *,
    task: Mapping[str, Any],
    edge_result: Mapping[str, Any],
    network_state: Mapping[str, Any],
    node_state: Mapping[str, Any],
) -> P1PacketChoice | None:
    """给出 P1 选择；任何异常/不可用返回 None（回退 R0）。

    云条件合格时合法集 {CLOUD_NOW, PROVISIONAL_DEFER}（放宽带加 LOCAL_FINAL）；
    云条件不合格时仅放宽带可进入，合法集 {LOCAL_FINAL, PROVISIONAL_DEFER}，
    否则返回 None 维持原规则。
    """
    if routing_policy_mode() != "p1":
        return None

    confidence = _float(edge_result.get("confidence"))
    cloud_available = bool(network_state.get("cloud_available", True))
    bandwidth_mbps = _float(network_state.get("bandwidth_mbps"), default=0.0) or 0.0
    latency_ms = _float(network_state.get("latency_ms"), default=0.0) or 0.0
    packet_loss = _float(network_state.get("packet_loss"), default=0.0) or 0.0
    cloud_queue = int(_float(node_state.get("cloud_queue_length"), default=0.0) or 0)

    # 云条件判定（与 business_rules.cloud_condition_reasons_raw 语义一致）
    cloud_conditions_ok = (
        cloud_available
        and bandwidth_mbps >= 2.0
        and latency_ms <= 100.0
        and packet_loss <= 0.10
        and cloud_queue <= 5
    )

    relaxed_ok = local_relaxed_ok(confidence)
    strict_local = confidence is not None and confidence >= 0.80

    # 构建合法动作集（与 business_rules.legal_actions 语义一致）
    if strict_local:
        allowed_actions: set[str] = {"LOCAL_FINAL"}
    else:
        allowed_actions = set()
        if cloud_conditions_ok:
            allowed_actions.add("CLOUD_NOW")
        allowed_actions.add("PROVISIONAL_DEFER")
        if relaxed_ok:
            allowed_actions.add("LOCAL_FINAL")

    # 单一合法动作无需模型：直接映射，避免加载开销
    if len(allowed_actions) == 1:
        action = next(iter(allowed_actions))
        return P1PacketChoice(
            route=_ACTION_TO_ROUTE[action],
            reason_codes=("SINGLE_LEGAL_ACTION", action),
            fallback=False,
        )

    checkpoint = _get_checkpoint()
    if checkpoint is None:
        return None  # 冻结模型不可用且存在选择空间：保持原固定规则行为

    try:
        from experiments.scheduler_comparison.contracts import (
            DecisionLevel,
            RouteAction,
            SchedulerContext,
        )
        from experiments.scheduler_comparison.policies.linucb import LinUCBPolicy

        now_ns = time.time_ns()
        context = SchedulerContext(
            decision_id=f"decision_v01_live_{task.get('task_id', 'unknown')}",
            device_id=str(task.get("source_node") or "edge_01"),
            bearing_id=None,
            packet_id=str(task.get("task_id", "")),
            decision_level=DecisionLevel.PACKET,
            confidence=confidence,
            aggregate_confidence=None,
            task_complexity=None,
            conflict=False,
            queue_length=cloud_queue,
            deferred_queue_length=0,
            retry_count=0,
            uplink_mbps=bandwidth_mbps,
            rtt_p95_ms=latency_ms,
            loss_rate=packet_loss,
            cloud_online=cloud_available,
            cloud_model_loaded=True,
            cloud_status_age_ms=DEFAULT_STATUS_AGE_MS,
            created_at_ns=now_ns,
            deadline_ns=now_ns + int(DEFAULT_REMAINING_MS * 1_000_000),
            now_ns=now_ns,
        )

        # 字符串动作集 → RouteAction 枚举集
        action_map = {a.value: a for a in RouteAction}
        allowed_frozen = frozenset(action_map[name] for name in allowed_actions)

        policy = LinUCBPolicy(checkpoint, online=False)
        policy.reset(0)
        decision = policy.decide(context, allowed_frozen)

        if decision.action not in allowed_frozen:
            return None  # 安全兜底：理论上不会发生

        route = _ACTION_TO_ROUTE[decision.action.value]
        reason_codes = tuple(decision.reason_codes) + (
            ("P1_FALLBACK_R0",) if decision.fallback else ("P1_LINUCB",)
        )
        if (
            decision.action is RouteAction.LOCAL_FINAL
            and relaxed_ok
            and confidence is not None
            and confidence <= 0.80
        ):
            reason_codes = reason_codes + ("P1_RELAXED_LOCAL_FINAL",)

        return P1PacketChoice(
            route=route,
            reason_codes=reason_codes,
            fallback=decision.fallback,
            scores=dict(decision.scores),
            decision_duration_ns=decision.decision_duration_ns,
        )
    except Exception:
        return None


def _float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
