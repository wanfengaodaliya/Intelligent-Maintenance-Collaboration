"""Minimal V0.1 conflict detection and resolution rules."""

from __future__ import annotations

import math
from typing import Any, Mapping


class ConsistencyValidationError(ValueError):
    """Raised when a consistency request does not satisfy the V0.1 contract."""


_DECISION_FIELDS = (
    "task_id",
    "source_node",
    "target_device",
    "action",
    "power_kw",
    "confidence",
    "priority",
    "timestamp",
)


def resolve_decisions(request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve V0.1 energy decisions with deterministic priority rules."""

    validated = _validate_request(request)
    decisions = validated["decisions"]
    constraints = validated["global_constraints"]
    grouped = _group_by_target_device(decisions)

    has_opposite_action = any(
        not constraints["allow_charge_and_discharge_same_time"]
        and {decision["action"] for decision in group} == {"charge", "discharge"}
        for group in grouped.values()
    )
    has_power_overload = any(
        sum(decision["power_kw"] for decision in group)
        > constraints[f"{target_device}_max_power_kw"]
        for target_device, group in grouped.items()
    )
    selected = max(decisions, key=lambda decision: (decision["priority"], decision["confidence"]))

    if has_opposite_action:
        conflict_type: str | None = "opposite_action"
    elif has_power_overload:
        conflict_type = "power_overload"
    else:
        conflict_type = None

    return {
        "decision_id": validated["decision_id"],
        "has_conflict": conflict_type is not None,
        "conflict_type": conflict_type,
        "final_action": selected["action"],
        "selected_source_node": selected["source_node"],
        "reason": f"{selected['action']} decision has higher priority and confidence",
        "resolved": True,
    }


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ConsistencyValidationError("request must be an object")
    decision_id = _non_empty_string(request.get("decision_id"), "decision_id")
    scenario = _non_empty_string(request.get("scenario"), "scenario")
    raw_decisions = request.get("decisions")
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise ConsistencyValidationError("decisions must be a non-empty array")
    decisions = [_validate_decision(decision, index) for index, decision in enumerate(raw_decisions)]
    constraints = _validate_constraints(request.get("global_constraints"), decisions)
    return {
        "decision_id": decision_id,
        "scenario": scenario,
        "decisions": decisions,
        "global_constraints": constraints,
    }


def _validate_decision(decision: Any, index: int) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        raise ConsistencyValidationError(f"decisions[{index}] must be an object")
    missing = [field for field in _DECISION_FIELDS if field not in decision]
    if missing:
        raise ConsistencyValidationError(f"decisions[{index}] missing field: {missing[0]}")
    action = decision["action"]
    if action not in {"charge", "discharge"}:
        raise ConsistencyValidationError(f"decisions[{index}].action must be charge or discharge")
    power_kw = _number(decision["power_kw"], f"decisions[{index}].power_kw")
    if power_kw < 0:
        raise ConsistencyValidationError(f"decisions[{index}].power_kw must be non-negative")
    confidence = _probability(decision["confidence"], f"decisions[{index}].confidence")
    priority = _probability(decision["priority"], f"decisions[{index}].priority")
    return {
        "task_id": _non_empty_string(decision["task_id"], f"decisions[{index}].task_id"),
        "source_node": _non_empty_string(decision["source_node"], f"decisions[{index}].source_node"),
        "target_device": _non_empty_string(decision["target_device"], f"decisions[{index}].target_device"),
        "action": action,
        "power_kw": power_kw,
        "confidence": confidence,
        "priority": priority,
        "timestamp": _non_empty_string(decision["timestamp"], f"decisions[{index}].timestamp"),
    }


def _validate_constraints(value: Any, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConsistencyValidationError("global_constraints must be an object")
    allow_same_time = value.get("allow_charge_and_discharge_same_time")
    if not isinstance(allow_same_time, bool):
        raise ConsistencyValidationError("allow_charge_and_discharge_same_time must be boolean")
    constraints: dict[str, Any] = {"allow_charge_and_discharge_same_time": allow_same_time}
    for target_device in {decision["target_device"] for decision in decisions}:
        field = f"{target_device}_max_power_kw"
        if field not in value:
            raise ConsistencyValidationError(f"global_constraints missing field: {field}")
        max_power_kw = _number(value[field], f"global_constraints.{field}")
        if max_power_kw < 0:
            raise ConsistencyValidationError(f"global_constraints.{field} must be non-negative")
        constraints[field] = max_power_kw
    return constraints


def _group_by_target_device(decisions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        grouped.setdefault(decision["target_device"], []).append(decision)
    return grouped


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConsistencyValidationError(f"{field} must be a non-empty string")
    return value


def _number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ConsistencyValidationError(f"{field} must be a finite number")
    return float(value)


def _probability(value: Any, field: str) -> float:
    probability = _number(value, field)
    if not 0 <= probability <= 1:
        raise ConsistencyValidationError(f"{field} must be between 0 and 1")
    return probability
