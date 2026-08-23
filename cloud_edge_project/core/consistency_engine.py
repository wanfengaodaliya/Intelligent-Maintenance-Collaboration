"""Scenario-neutral orchestration for one consistency decision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ConsistencyUnit:
    unit_id: str
    lifecycle_status: str
    confidence: float
    data_quality_score: float
    action_level: int
    scenario_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConsistencyRequest:
    units: tuple[ConsistencyUnit, ...]
    expected_unit_ids: tuple[str, ...]
    closure_reason: str
    closed_at_ns: int


@dataclass(frozen=True)
class ConsistencyDecision:
    status: str
    received_unit_ids: tuple[str, ...]
    missing_unit_ids: tuple[str, ...]
    final_state: str
    final_action_level: int
    final_action: str
    confidence: float
    data_quality_score: float
    has_conflict: bool
    conflict_reasons: tuple[str, ...]
    degraded: bool
    decision_source: str = "EDGE"
    affects_realtime_action: bool = True


class ConsistencyPolicy(Protocol):
    def evaluate(self, request: ConsistencyRequest) -> ConsistencyDecision: ...


class ConsistencyEngine:
    def __init__(self, policy: ConsistencyPolicy) -> None:
        self._policy = policy

    def evaluate(self, request: ConsistencyRequest) -> ConsistencyDecision:
        return self._policy.evaluate(request)
