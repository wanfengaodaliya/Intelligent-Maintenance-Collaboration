"""Scenario-neutral contracts shared by platform orchestration code."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")


def _freeze(value: Any) -> Any:
    if isinstance(value, MappingABC):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ScenarioInferenceRequest:
    """One scenario-neutral request for an inference capability."""

    scenario_id: str
    task_id: str
    unit_id: str
    device_id: str
    capability: str
    observation_window_id: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "scenario_id",
            "task_id",
            "unit_id",
            "device_id",
            "capability",
            "observation_window_id",
        ):
            _require_identifier(name, getattr(self, name))
        object.__setattr__(self, "evidence", _freeze(self.evidence))


@dataclass(frozen=True)
class ScenarioDiagnosis:
    """Normalized diagnosis returned by an edge or cloud provider."""

    scenario_id: str
    task_id: str
    unit_id: str
    state: str
    confidence: float
    risk_level: str
    action_level: int
    model_id: str
    model_version: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "scenario_id",
            "task_id",
            "unit_id",
            "state",
            "risk_level",
            "model_id",
            "model_version",
        ):
            _require_identifier(name, getattr(self, name))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.action_level < 0:
            raise ValueError("action_level must not be negative")
        object.__setattr__(self, "evidence", _freeze(self.evidence))


@dataclass(frozen=True)
class ScenarioDecision:
    """Scenario-neutral final decision produced by a policy provider."""

    scenario_id: str
    task_id: str
    unit_id: str
    state: str
    confidence: float
    risk_level: str
    action_level: int
    decision: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "scenario_id",
            "task_id",
            "unit_id",
            "state",
            "risk_level",
            "decision",
        ):
            _require_identifier(name, getattr(self, name))
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.action_level < 0:
            raise ValueError("action_level must not be negative")
        object.__setattr__(self, "evidence", _freeze(self.evidence))
