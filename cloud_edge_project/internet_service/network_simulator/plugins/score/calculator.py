"""Pure, explainable V3 link reliability score calculation."""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import math

from controller.config_loader import ScoreConfig
from domain.enums import NetworkState
from domain.models import NetworkParameters, SCORE_COMPONENT_NAMES, ScoreResult


COMPONENT_NAMES = SCORE_COMPONENT_NAMES


class LinkReliabilityCalculator:
    def __init__(self, config: ScoreConfig) -> None:
        self._config = config
        self._weights = {
            "latency": config.weights.latency,
            "jitter": config.weights.jitter,
            "bandwidth": config.weights.bandwidth,
            "packet_loss": config.weights.packet_loss,
            "state_prior": config.weights.state_prior,
        }

    def calculate(
        self,
        state: NetworkState,
        parameters: NetworkParameters | None,
        last_apply_success: bool,
        consecutive_apply_failures: int,
    ) -> ScoreResult:
        if consecutive_apply_failures < 0:
            raise ValueError("consecutive_apply_failures must be non-negative")
        try:
            resolved_state = NetworkState(state)
        except ValueError as exc:
            raise ValueError(f"unknown network state: {state}") from exc

        if parameters is None:
            return self._forced_zero("no_applied_parameters")
        if parameters.state is not resolved_state:
            raise ValueError("state must match applied parameters state")
        if resolved_state is NetworkState.DISCONNECTED:
            return self._forced_zero(
                "disconnected",
                packet_loss_applied=parameters.packet_loss_applied,
            )

        latency = self._finite_required(parameters.latency_ms, "latency_ms")
        jitter = self._finite_required(parameters.jitter_ms, "jitter_ms")
        bandwidth = self._finite_required(
            parameters.bandwidth_kbps, "bandwidth_kbps"
        )
        packet_loss = self._finite_required(
            parameters.packet_loss_percent, "packet_loss_percent"
        )
        normalization = self._config.normalization
        components = {
            "latency": self._lower_is_better(
                latency,
                normalization.latency_best_ms,
                normalization.latency_worst_ms,
            ),
            "jitter": self._lower_is_better(
                jitter,
                normalization.jitter_best_ms,
                normalization.jitter_worst_ms,
            ),
            "bandwidth": self._higher_is_better(
                bandwidth,
                normalization.bandwidth_worst_kbps,
                normalization.bandwidth_best_kbps,
            ),
            "packet_loss": self._lower_is_better(
                packet_loss,
                normalization.packet_loss_best_percent,
                normalization.packet_loss_worst_percent,
            ),
            "state_prior": float(self._config.state_prior[resolved_state]),
        }
        score = sum(
            components[name] * self._weights[name] for name in COMPONENT_NAMES
        )
        threshold = (
            self._config.failure_policy.unavailable_after_consecutive_failures
        )
        if consecutive_apply_failures >= threshold:
            score = self._round_capped_score(
                score,
                self._config.failure_policy.max_score_when_apply_failed,
            )
            available = False
            reason = "failure_threshold_reached"
        else:
            score = self._round_score(score)
            available = True
            reason = (
                "ok"
                if last_apply_success
                else "using_last_successful_apply_after_failure"
            )
        return ScoreResult(
            score=score,
            available=available,
            components=components,
            weights=self._weights,
            reason=reason,
            packet_loss_applied=parameters.packet_loss_applied,
        )

    def _forced_zero(
        self,
        reason: str,
        *,
        packet_loss_applied: bool = False,
    ) -> ScoreResult:
        return ScoreResult(
            score=0.0,
            available=False,
            components={name: 0.0 for name in COMPONENT_NAMES},
            weights=self._weights,
            reason=reason,
            packet_loss_applied=packet_loss_applied,
        )

    @staticmethod
    def _finite_required(value: float | int | None, field_name: str) -> float:
        if value is None or not math.isfinite(value):
            raise ValueError(f"{field_name} must be a finite number")
        return float(value)

    @staticmethod
    def _lower_is_better(value: float, best: float, worst: float) -> float:
        return LinkReliabilityCalculator._clamp(
            100.0 * (worst - value) / (worst - best)
        )

    @staticmethod
    def _higher_is_better(value: float, worst: float, best: float) -> float:
        return LinkReliabilityCalculator._clamp(
            100.0 * (value - worst) / (best - worst)
        )

    @staticmethod
    def _clamp(value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("normalized score must be finite")
        return min(100.0, max(0.0, value))

    def _round_score(self, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("weighted score must be finite")
        quantum = Decimal(1).scaleb(-self._config.precision)
        rounded = Decimal(str(self._clamp(value))).quantize(
            quantum, rounding=ROUND_HALF_UP
        )
        return float(rounded)

    def _round_capped_score(self, value: float, cap: float) -> float:
        rounded = self._round_score(value)
        if rounded <= cap:
            return rounded
        quantum = Decimal(1).scaleb(-self._config.precision)
        return float(Decimal(str(cap)).quantize(quantum, rounding=ROUND_FLOOR))
