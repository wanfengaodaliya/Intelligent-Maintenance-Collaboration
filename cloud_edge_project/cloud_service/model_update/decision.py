"""Decision gate over a problem already identified by global analysis."""

from __future__ import annotations

from typing import Any

from cloud_service.model_update.contracts import ModelUpdateConfig


def decide_update(
    problem_candidate: dict[str, Any], config: ModelUpdateConfig
) -> str:
    """Return ``create_update`` only for a persistent supported weakness."""

    if not isinstance(problem_candidate, dict):
        return "observe"
    if problem_candidate.get("problem_layer") != "packet_diagnosis":
        return "observe"
    if problem_candidate.get("suggested_action") != "model_update":
        return "observe"
    if problem_candidate.get("persistence") != "persistent":
        return "observe"
    evidence = problem_candidate.get("evidence")
    sample_count = evidence.get("sample_count") if isinstance(evidence, dict) else None
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        return "observe"
    if sample_count < config.min_update_evidence_count:
        return "observe"
    return "create_update"
