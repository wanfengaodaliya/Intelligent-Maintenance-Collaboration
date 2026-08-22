"""P2: Ridge outcome prediction + legal-action enumeration + R0 fallback."""

# P2 不直接学「选哪个动作」，而是对每个合法动作分别预测归一化最终时延余量
# slack 与永久失败概率 p_fail，再在合法动作集合内做枚举式选择。
# 提取并归一化 P1 LinUCB 的上下文特征。

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
)
from .baseline import decide_r0

P2_POLICY_ID = "P2"
P2_NO_FALLBACK_POLICY_ID = "P2_NO_FALLBACK"
P2_POLICY_VERSION = "1.0.0"

# 特征顺序冻结：连续特征 -> 交叉特征；task_complexity 与 confidence 同源，不并存。
FEATURE_NAMES = (
    "confidence",
    "output_missing",
    "conflict",
    "queue_length",
    "deferred_queue_length",
    "retry_count",
    "uplink_mbps",
    "rtt_p95_ms",
    "loss_rate",
    "cloud_ready",
    "status_age_ms",
    "remaining_ms",
    "x_uplink_queue",
    "x_rtt_queue",
    "x_remaining_deferred",
)
STATUS_AGE_CAP_MS = 10_000.0
_TIE_ORDER = {
    RouteAction.LOCAL_FINAL: 0,
    RouteAction.CLOUD_NOW: 1,
    RouteAction.PROVISIONAL_DEFER: 2,
}


def extract_features(context: SchedulerContext) -> np.ndarray:
    """从决策上下文提取原始特征（未归一化）。"""
    confidence = context.primary_confidence
    output_missing = 0.0 if confidence is not None else 1.0
    conf = float(confidence) if confidence is not None else 0.0
    cloud_ready = (
        1.0
        if context.cloud_online
        and context.cloud_status_age_ms <= 5_000.0
        and context.cloud_model_loaded
        else 0.0
    )
    remaining_ms = context.remaining_ns / 1_000_000
    base = np.array(
        [
            conf,
            output_missing,
            1.0 if context.conflict else 0.0,
            float(context.queue_length),
            float(context.deferred_queue_length),
            float(context.retry_count),
            context.uplink_mbps,
            context.rtt_p95_ms,
            context.loss_rate,
            cloud_ready,
            min(context.cloud_status_age_ms, STATUS_AGE_CAP_MS),
            remaining_ms,
        ],
        dtype=np.float64,
    )
    return base


@dataclass(frozen=True)
class Normalizer:
    """冻结的 min-max 归一化参数，只允许由训练集统计量确定。"""

    minimums: np.ndarray
    ranges: np.ndarray

    def transform(self, raw: np.ndarray) -> np.ndarray:
        safe_range = np.where(self.ranges > 1e-12, self.ranges, 1.0)
        normalized = (raw - self.minimums) / safe_range
        return np.where(self.ranges > 1e-12, normalized, 0.5)

    def cross_terms(self, normalized: np.ndarray) -> np.ndarray:
        idx = {name: i for i, name in enumerate(FEATURE_NAMES[:12])}
        return np.array(
            [
                normalized[idx["uplink_mbps"]] * normalized[idx["queue_length"]],
                normalized[idx["rtt_p95_ms"]] * normalized[idx["queue_length"]],
                normalized[idx["remaining_ms"]] * normalized[idx["deferred_queue_length"]],
            ],
            dtype=np.float64,
        )

    def full_vector(self, context: SchedulerContext) -> np.ndarray:
        raw = extract_features(context)
        normalized = self.transform(raw)
        cross = self.cross_terms(normalized)
        return np.concatenate([normalized, cross])

    def is_out_of_distribution(self, vector: np.ndarray, margin: float) -> bool:
        return bool(np.any(vector < -margin) or np.any(vector > 1.0 + margin))

    @staticmethod
    def fit(vectors: Sequence[np.ndarray]) -> "Normalizer":
        matrix = np.stack(vectors)
        minimums = matrix.min(axis=0)
        maximums = matrix.max(axis=0)
        return Normalizer(minimums=minimums, ranges=maximums - minimums)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimums": self.minimums.tolist(),
            "ranges": self.ranges.tolist(),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Normalizer":
        return Normalizer(
            minimums=np.asarray(data["minimums"], dtype=np.float64),
            ranges=np.asarray(data["ranges"], dtype=np.float64),
        )


def _ridge_fit(features: np.ndarray, targets: np.ndarray, alpha: float) -> np.ndarray:
    """Ridge 回归闭式解：(X'X + aI) w = X'y，带常数项。"""
    design = np.concatenate([features, np.ones((features.shape[0], 1))], axis=1)
    penalty = alpha * np.eye(design.shape[1])
    penalty[-1, -1] = 0.0  # 不惩罚偏置项
    lhs = design.T @ design + penalty
    rhs = design.T @ targets
    weights = np.linalg.solve(lhs, rhs)
    return weights


def _ridge_predict(weights: np.ndarray, vector: np.ndarray) -> float:
    design = np.concatenate([vector, np.ones(1)])
    return float(design @ weights)


class RidgeCheckpoint:
    """每个 (决策层级, 动作) 各自维护 slack / fail 两组 Ridge 系数。"""

    def __init__(self) -> None:
        self.normalizers: dict[str, Normalizer] = {}
        self.weights: dict[str, dict[str, np.ndarray]] = {}
        self.sample_counts: dict[str, dict[str, int]] = {}
        self.alpha_slack: float | None = None
        self.alpha_fail: float | None = None
        self.fail_threshold: float | None = None
        self.ood_margin: float | None = None
        self.min_samples: int = 200
        self.trained_on: dict[str, Any] = {}

    # -- 训练 -------------------------------------------------------------
    def fit(
        self,
        level: DecisionLevel,
        samples: Sequence[tuple[np.ndarray, str, float, float]],
        alpha_slack: float,
        alpha_fail: float,
    ) -> None:
        """samples: (原始12维特征向量, 动作名, slack标签, fail标签)。"""
        key = level.value
        if not samples:
            raise ValueError(f"no training samples for level {key}")
        vectors = [item[0] for item in samples]
        self.normalizers[key] = Normalizer.fit(vectors)
        normalizer = self.normalizers[key]
        self.weights[key] = {}
        self.sample_counts[key] = {}
        for action in RouteAction:
            rows = [
                item
                for item in samples
                if item[1] == action.value
            ]
            self.sample_counts[key][action.value] = len(rows)
            if len(rows) < 2:
                continue
            feature_rows = np.stack(
                [np.concatenate([normalizer.transform(r[0]), normalizer.cross_terms(normalizer.transform(r[0]))]) for r in rows]
            )
            slack_targets = np.array([r[2] for r in rows], dtype=np.float64)
            fail_targets = np.array([r[3] for r in rows], dtype=np.float64)
            self.weights[key][f"{action.value}:slack"] = _ridge_fit(feature_rows, slack_targets, alpha_slack)
            self.weights[key][f"{action.value}:fail"] = _ridge_fit(feature_rows, fail_targets, alpha_fail)
        self.alpha_slack = alpha_slack
        self.alpha_fail = alpha_fail

    # -- 推理 -------------------------------------------------------------
    def ready(self, level: DecisionLevel) -> bool:
        key = level.value
        counts = self.sample_counts.get(key, {})
        return sum(counts.values()) >= self.min_samples and key in self.weights

    def predict(
        self, level: DecisionLevel, context: SchedulerContext
    ) -> tuple[dict[str, dict[str, float]], bool]:
        """返回每个动作的 {slack, p_fail} 预测；特征越界时第二项为 True。"""
        key = level.value
        normalizer = self.normalizers[key]
        vector = normalizer.full_vector(context)
        ood = normalizer.is_out_of_distribution(vector[:12], self.ood_margin or 0.1)
        scores: dict[str, dict[str, float]] = {}
        for action in RouteAction:
            slack_w = self.weights[key].get(f"{action.value}:slack")
            fail_w = self.weights[key].get(f"{action.value}:fail")
            if slack_w is None or fail_w is None:
                continue
            slack = _ridge_predict(slack_w, vector)
            p_fail = min(1.0, max(0.0, _ridge_predict(fail_w, vector)))
            scores[action.value] = {"slack": round(slack, 6), "p_fail": round(p_fail, 6)}
        return scores, ood

    def total_samples(self, level: DecisionLevel) -> int:
        return sum(self.sample_counts.get(level.value, {}).values())

    # -- 持久化 -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": P2_POLICY_VERSION,
            "alpha_slack": self.alpha_slack,
            "alpha_fail": self.alpha_fail,
            "fail_threshold": self.fail_threshold,
            "ood_margin": self.ood_margin,
            "min_samples": self.min_samples,
            "sample_counts": self.sample_counts,
            "trained_on": self.trained_on,
            "normalizers": {k: v.to_dict() for k, v in self.normalizers.items()},
            "weights": {
                level: {name: weights.tolist() for name, weights in table.items()}
                for level, table in self.weights.items()
            },
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "RidgeCheckpoint":
        checkpoint = RidgeCheckpoint()
        checkpoint.alpha_slack = data["alpha_slack"]
        checkpoint.alpha_fail = data["alpha_fail"]
        checkpoint.fail_threshold = data["fail_threshold"]
        checkpoint.ood_margin = data["ood_margin"]
        checkpoint.min_samples = int(data.get("min_samples", 200))
        checkpoint.sample_counts = data.get("sample_counts", {})
        checkpoint.trained_on = data.get("trained_on", {})
        checkpoint.normalizers = {
            key: Normalizer.from_dict(value) for key, value in data["normalizers"].items()
        }
        checkpoint.weights = {
            level: {name: np.asarray(weights, dtype=np.float64) for name, weights in table.items()}
            for level, table in data["weights"].items()
        }
        return checkpoint

    def save(self, path: Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        target.write_text(canonical, encoding="utf-8")
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def load(path: Path) -> "RidgeCheckpoint":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return RidgeCheckpoint.from_dict(data)


class PredictivePolicy:
    """组 B/C：Ridge 结果预测 + 合法动作枚举。

    fallback_enabled=True 为组 B（正式对照）；False 为组 C（消融，仅风险分析）。
    """

    policy_version = P2_POLICY_VERSION

    def __init__(
        self,
        checkpoint: RidgeCheckpoint | None = None,
        *,
        fallback_enabled: bool = True,
        policy_id: str | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self.fallback_enabled = fallback_enabled
        self.policy_id = policy_id or (
            P2_POLICY_ID if fallback_enabled else P2_NO_FALLBACK_POLICY_ID
        )
        self.fallback_events: list[dict[str, Any]] = []
        self._seed = 0

    def reset(self, seed: int) -> None:
        self._seed = seed
        self.fallback_events = []

    def decide(
        self,
        context: SchedulerContext,
        allowed_actions: frozenset[RouteAction],
    ) -> PolicyDecision:
        start = time.perf_counter_ns()
        ordered = sorted(allowed_actions, key=lambda item: _TIE_ORDER[item])

        if len(ordered) == 1:
            action = ordered[0]
            return self._decision(action, ("SINGLE_LEGAL_ACTION",), {}, start, fallback=False)

        fallback_reason = None
        if self.checkpoint is None or not self.checkpoint.ready(context.decision_level):
            fallback_reason = "INSUFFICIENT_SAMPLES"
            scores: dict[str, dict[str, float]] = {}
        else:
            scores, ood = self.checkpoint.predict(context.decision_level, context)
            if ood:
                fallback_reason = "OUT_OF_DISTRIBUTION"

        if fallback_reason is None and self.checkpoint is not None:
            threshold = self.checkpoint.fail_threshold or 0.2
            candidates = [
                action
                for action in ordered
                if action.value in scores and scores[action.value]["p_fail"] <= threshold
            ]
            if not candidates:
                fallback_reason = "EMPTY_CANDIDATES"
            else:
                best = max(
                    candidates,
                    key=lambda action: (scores[action.value]["slack"], -_TIE_ORDER[action]),
                )
                return self._decision(best, ("PREDICTED_SLACK_MAX",), scores, start, fallback=False)

        # 回退或消融兜底
        if self.fallback_enabled or fallback_reason in {"INSUFFICIENT_SAMPLES"}:
            r0_decision = decide_r0(context, allowed_actions)
            duration = time.perf_counter_ns() - start
            if self.fallback_enabled:
                self.fallback_events.append(
                    {
                        "decision_id": context.decision_id,
                        "reason": fallback_reason or "EMPTY_CANDIDATES",
                        "action": r0_decision.action.value,
                    }
                )
                return PolicyDecision(
                    action=r0_decision.action,
                    policy_id=self.policy_id,
                    policy_version=self.policy_version,
                    reason_codes=("PREDICTOR_FALLBACK", fallback_reason or "EMPTY_CANDIDATES")
                    + r0_decision.reason_codes,
                    scores=r0_decision.scores,
                    selection_probability=1.0,
                    decision_duration_ns=duration,
                    fallback=True,
                )
        # 消融组 C：强制按预测选择（仅保留业务掩码），不做安全回退。
        if scores:
            best = max(ordered, key=lambda a: (scores.get(a.value, {}).get("slack", -2.0), -_TIE_ORDER[a]))
        else:
            best = RouteAction.CLOUD_NOW if RouteAction.CLOUD_NOW in allowed_actions else RouteAction.PROVISIONAL_DEFER
        return self._decision(
            best,
            ("NO_FALLBACK_FORCED",) + ((fallback_reason,) if fallback_reason else ()),
            scores,
            start,
            fallback=False,
        )

    def observe(self, outcome: DecisionOutcome) -> None:
        # 冻结推理模式下不在线更新系数，仅记录。
        return None

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
