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


def resolve_decisions(request: Any) -> dict[str, Any]:
    """Resolve V0.1 energy decisions with deterministic priority rules."""

    validated = _validate_request(request)
    decisions = validated["decisions"]
    constraints = validated["global_constraints"]
    grouped = _group_by_target_device(decisions)

    conflict_type, candidates = _conflict_candidates(grouped, constraints)
    safe_candidates = [
        decision
        for decision in candidates
        if decision["power_kw"] <= constraints[f"{decision['target_device']}_max_power_kw"]
    ]
    if not safe_candidates:
        return {
            "decision_id": validated["decision_id"],
            "has_conflict": conflict_type is not None,
            "conflict_type": conflict_type,
            "final_action": None,
            "selected_source_node": None,
            "reason": "no individually safe decision satisfies the target power limit",
            "resolved": False,
        }
    selected = max(safe_candidates, key=lambda decision: (decision["priority"], decision["confidence"]))

    return {
        "decision_id": validated["decision_id"],
        "has_conflict": conflict_type is not None,
        "conflict_type": conflict_type,
        "final_action": selected["action"],
        "selected_source_node": selected["source_node"],
        "reason": _selection_reason(safe_candidates, selected),
        "resolved": True,
    }


def _validate_request(request: Any) -> dict[str, Any]:
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
    if not isinstance(action, str) or action not in {"charge", "discharge"}:
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


def _conflict_candidates(
    grouped: dict[str, list[dict[str, Any]]], constraints: dict[str, Any]
) -> tuple[str | None, list[dict[str, Any]]]:
    if not constraints["allow_charge_and_discharge_same_time"]:
        for decisions in grouped.values():
            if {decision["action"] for decision in decisions} == {"charge", "discharge"}:
                return "opposite_action", decisions

    for target_device, decisions in grouped.items():
        by_action: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            by_action.setdefault(decision["action"], []).append(decision)
        max_power_kw = constraints[f"{target_device}_max_power_kw"]
        for action_decisions in by_action.values():
            if sum(decision["power_kw"] for decision in action_decisions) > max_power_kw:
                return "power_overload", action_decisions

    return None, [decision for decisions in grouped.values() for decision in decisions]


def _selection_reason(candidates: list[dict[str, Any]], selected: dict[str, Any]) -> str:
    other_decisions = [decision for decision in candidates if decision is not selected]
    if not other_decisions:
        return "only applicable decision was selected"
    if all(
        selected["priority"] > decision["priority"]
        and selected["confidence"] > decision["confidence"]
        for decision in other_decisions
    ):
        return f"{selected['action']} decision has higher priority and confidence"
    if all(selected["priority"] > decision["priority"] for decision in other_decisions):
        return f"{selected['action']} decision has highest priority; confidence is used only to break priority ties"
    return f"{selected['action']} decision has highest priority and confidence among tied priorities"


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
