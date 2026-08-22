"""P1: constrained LinUCB contextual bandit (packet/device models kept separate)."""

# P1 只对业务掩码允许的动作评分；单一合法动作直接选择不探索。
# 回报：永久失败/过期记 -1，成功为归一化截止余量 clip 到 [-1, 1]；
# 仅在任务获得最终结果或永久失败后 observe，不把临时 PROVISIONAL 当成功。
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..contracts import (
    DecisionLevel,
    DecisionOutcome,
    PolicyDecision,
    RouteAction,
    SchedulerContext,
    TerminalState,
)
from .baseline import decide_r0
from .predictive import Normalizer, extract_features

P1_POLICY_ID = "P1"
P1_POLICY_VERSION = "1.0.0"

_TIE_ORDER = {
    RouteAction.LOCAL_FINAL: 0,
    RouteAction.CLOUD_NOW: 1,
    RouteAction.PROVISIONAL_DEFER: 2,
}


@dataclass(frozen=True)
class _ArmStats:
    """单个 (层级, 动作) 臂的 LinUCB 充分统计量：A 与 b。"""

    matrix: np.ndarray
    vector: np.ndarray


class LinUCBCheckpoint:
    """包级与设备级各自维护每动作的 A/b 统计量，互不共享参数。"""

    def __init__(self) -> None:
        self.dim: int = 15
        self.alpha: float = 0.5
        self.min_samples: int = 200
        self.stats: dict[str, dict[str, _ArmStats]] = {}
        self.sample_counts: dict[str, dict[str, int]] = {}
        self.normalizers: dict[str, Normalizer] = {}
        self.trained_on: dict[str, Any] = {}

    # -- 初始化与训练 -------------------------------------------------------
    def init_level(self, level: DecisionLevel, normalizer: Normalizer) -> None:
        key = level.value
        self.normalizers[key] = normalizer
        self.stats.setdefault(key, {})
        self.sample_counts.setdefault(key, {})
        for action in RouteAction:
            self.stats[key].setdefault(
                action.value,
                _ArmStats(matrix=np.eye(self.dim), vector=np.zeros(self.dim)),
            )
            self.sample_counts[key].setdefault(action.value, 0)

    def feature(self, level: DecisionLevel, context: SchedulerContext) -> np.ndarray:
        normalizer = self.normalizers[level.value]
        return normalizer.full_vector(context)

    def update(
        self, level: DecisionLevel, action: RouteAction, vector: np.ndarray, reward: float
    ) -> None:
        key = level.value
        arm = self.stats[key][action.value]
        self.stats[key][action.value] = _ArmStats(
            matrix=arm.matrix + np.outer(vector, vector),
            vector=arm.vector + reward * vector,
        )
        self.sample_counts[key][action.value] += 1

    def ready(self, level: DecisionLevel) -> bool:
        counts = self.sample_counts.get(level.value, {})
        return (
            level.value in self.normalizers
            and sum(counts.values()) >= self.min_samples
            and len(self.stats.get(level.value, {})) == len(RouteAction)
        )

    def score(
        self, level: DecisionLevel, vector: np.ndarray
    ) -> dict[str, float]:
        key = level.value
        scores: dict[str, float] = {}
        for action in RouteAction:
            arm = self.stats[key][action.value]
            a_inv = np.linalg.inv(arm.matrix)
            theta = a_inv @ arm.vector
            mean = float(theta @ vector)
            radius = float(np.sqrt(max(0.0, vector @ a_inv @ vector)))
            scores[action.value] = mean + self.alpha * radius
        return scores

    def total_samples(self, level: DecisionLevel) -> int:
        return sum(self.sample_counts.get(level.value, {}).values())

    # -- 持久化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": P1_POLICY_VERSION,
            "dim": self.dim,
            "alpha": self.alpha,
            "min_samples": self.min_samples,
            "trained_on": self.trained_on,
            "sample_counts": self.sample_counts,
            "normalizers": {k: v.to_dict() for k, v in self.normalizers.items()},
            "stats": {
                level: {
                    action: {"matrix": arm.matrix.tolist(), "vector": arm.vector.tolist()}
                    for action, arm in arms.items()
                }
                for level, arms in self.stats.items()
            },
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "LinUCBCheckpoint":
        checkpoint = LinUCBCheckpoint()
        checkpoint.dim = int(data["dim"])
        checkpoint.alpha = float(data["alpha"])
        checkpoint.min_samples = int(data.get("min_samples", 200))
        checkpoint.trained_on = data.get("trained_on", {})
        checkpoint.sample_counts = data.get("sample_counts", {})
        checkpoint.normalizers = {
            key: Normalizer.from_dict(value) for key, value in data["normalizers"].items()
        }
        checkpoint.stats = {
            level: {
                action: _ArmStats(
                    matrix=np.asarray(arm["matrix"], dtype=np.float64),
                    vector=np.asarray(arm["vector"], dtype=np.float64),
                )
                for action, arm in arms.items()
            }
            for level, arms in data["stats"].items()
        }
        return checkpoint

    def save(self, path: Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        target.write_text(canonical, encoding="utf-8")
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def load(path: Path) -> "LinUCBCheckpoint":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return LinUCBCheckpoint.from_dict(data)


def outcome_reward(outcome: DecisionOutcome) -> float:
    """统一回报定义：非成功记 -1；成功为归一化截止余量 clip 到 [-1, 1]。"""
    if not outcome.success or outcome.final_at_ns is None:
        return -1.0
    span = outcome.deadline_ns - outcome.created_at_ns
    if span <= 0:
        return -1.0
    raw = (outcome.deadline_ns - outcome.final_at_ns) / span
    return max(-1.0, min(1.0, raw))


class LinUCBPolicy:
    """P1：受约束 LinUCB。training=True 为训练期在线模式：归一化参数就绪即评分并即时更新；
    training=False 为冻结推理，需达到 min_samples 才启用模型。

    模型不可用时回退 R0（记 PREDICTOR_FALLBACK），保持安全兜底。
"""

# 实现受业务约束的 P1 LinUCB 上下文 bandit 调度算法。

    policy_version = P1_POLICY_VERSION

    def __init__(
        self,
        checkpoint: LinUCBCheckpoint | None = None,
        *,
        online: bool = False,
        training: bool = False,
        policy_id: str = P1_POLICY_ID,
    ) -> None:
        self.checkpoint = checkpoint
        self.online = online
        self.training = training
        self.policy_id = policy_id
        self._seed = 0
        self._pending: dict[str, dict[str, Any]] = {}
        self.fallback_events: list[dict[str, Any]] = []

    def reset(self, seed: int) -> None:
        self._seed = seed
        self._pending = {}
        self.fallback_events = []

    def decide(
        self,
        context: SchedulerContext,
        allowed_actions: frozenset[RouteAction],
    ) -> PolicyDecision:
        start = time.perf_counter_ns()
        ordered = sorted(allowed_actions, key=lambda item: _TIE_ORDER[item])

        if len(ordered) == 1:
            # 单一合法动作：直接选择不探索，仍记录待结算（observe 不更新模型）。
            action = ordered[0]
            return self._decision(action, ("SINGLE_LEGAL_ACTION",), {}, start, fallback=False)

        if self.checkpoint is None or not self._usable(context.decision_level):
            return self._fallback(context, allowed_actions, "INSUFFICIENT_SAMPLES", start)

        vector = self.checkpoint.feature(context.decision_level, context)
        if self.checkpoint.normalizers[context.decision_level.value].is_out_of_distribution(
            vector[:12], 0.1
        ):
            return self._fallback(context, allowed_actions, "OUT_OF_DISTRIBUTION", start)

        all_scores = self.checkpoint.score(context.decision_level, vector)
        scores = {
            action.value: {"ucb": all_scores[action.value]} for action in ordered
        }
        best = max(ordered, key=lambda a: (all_scores[a.value], -_TIE_ORDER[a]))
        if self.online:
            self._pending[context.decision_id] = {
                "level": context.decision_level,
                "action": best,
                "vector": vector,
            }
        return self._decision(best, ("LINUCB_MAX_UCB",), scores, start, fallback=False)

    def _usable(self, level: DecisionLevel) -> bool:
        """训练期在线模式：归一化参数就绪即可评分；冻结推理需达到最小样本数。"""
        if self.checkpoint is None:
            return False
        if self.training:
            return level.value in self.checkpoint.normalizers
        return self.checkpoint.ready(level)

    def _fallback(
        self,
        context: SchedulerContext,
        allowed_actions: frozenset[RouteAction],
        reason: str,
        start: int,
    ) -> PolicyDecision:
        r0_decision = decide_r0(context, allowed_actions)
        self.fallback_events.append(
            {
                "decision_id": context.decision_id,
                "reason": reason,
                "action": r0_decision.action.value,
            }
        )
        return PolicyDecision(
            action=r0_decision.action,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            reason_codes=("PREDICTOR_FALLBACK", reason) + r0_decision.reason_codes,
            scores=r0_decision.scores,
            selection_probability=1.0,
            decision_duration_ns=time.perf_counter_ns() - start,
            fallback=True,
        )

    def observe(self, outcome: DecisionOutcome) -> None:
        if not self.online:
            return
        pending = self._pending.pop(outcome.decision_id, None)
        if pending is None or self.checkpoint is None:
            return
        if outcome.terminal_state not in (
            TerminalState.SUCCEEDED,
            TerminalState.PERMANENT_FAILED,
            TerminalState.EXPIRED,
        ):
            return
        reward = outcome_reward(outcome)
        self.checkpoint.update(pending["level"], pending["action"], pending["vector"], reward)

    def _decision(
        self,
        action: RouteAction,
        reason_codes: tuple[str, ...],
        scores: Mapping[str, Mapping[str, float]],
        start_ns: int,
        *,
        fallback: bool,
    ) -> PolicyDecision:
        return PolicyDecision(
            action=action,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            reason_codes=reason_codes,
            scores=scores,
            selection_probability=1.0,
            decision_duration_ns=time.perf_counter_ns() - start_ns,
            fallback=fallback,
        )


def fit_normalizers(samples_by_level: dict[str, Sequence[np.ndarray]]) -> dict[str, Normalizer]:
    """用训练样本的原始特征向量拟合每层级的 min-max 归一化参数。"""
    return {
        level: Normalizer.fit(list(vectors))
        for level, vectors in samples_by_level.items()
        if vectors
    }
