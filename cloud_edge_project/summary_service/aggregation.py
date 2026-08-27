from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from typing import Any

from .contracts import ACTION_BY_GRADE, EXPECTED_BEARING_IDS, group_key, stable_id


def build_window_result(
    results: Iterable[Mapping[str, Any]],
    *,
    closed_at_ns: int,
    expected_bearing_ids: Sequence[str] = EXPECTED_BEARING_IDS,
) -> dict[str, Any]:
    source_results = sorted(
        (dict(result) for result in results), key=lambda item: str(item["bearing_id"])
    )
    expected = tuple(expected_bearing_ids)
    actual = tuple(result["bearing_id"] for result in source_results)
    if actual != tuple(sorted(expected)):
        raise ValueError(f"expected one result for each bearing {expected}, got {actual}")

    keys = {group_key(result) for result in source_results}
    if len(keys) != 1:
        raise ValueError("all bearing results must belong to the same device window")
    device_id, window_start, window_end = keys.pop()

    edge_ids = sorted({str(result["edge_node_id"]) for result in source_results})
    comparisons: list[dict[str, Any]] = []
    for left, right in combinations(source_results, 2):
        if left["edge_node_id"] == right["edge_node_id"]:
            continue
        grade_gap = abs(int(left["action_grade"]) - int(right["action_grade"]))
        comparisons.append(
            {
                "left_bearing_id": left["bearing_id"],
                "left_edge_node_id": left["edge_node_id"],
                "left_action_grade": int(left["action_grade"]),
                "right_bearing_id": right["bearing_id"],
                "right_edge_node_id": right["edge_node_id"],
                "right_action_grade": int(right["action_grade"]),
                "grade_gap": grade_gap,
                "is_conflict": grade_gap >= 2,
            }
        )

    sufficient_edge_diversity = len(edge_ids) >= 2
    has_conflict = sufficient_edge_diversity and any(
        comparison["is_conflict"] for comparison in comparisons
    )
    final_grade = max(int(result["action_grade"]) for result in source_results)
    summary_result_id = stable_id("summary", device_id, window_start, window_end)
    conflict_id = (
        stable_id("conflict", device_id, window_start, window_end) if has_conflict else None
    )

    action_grades_by_edge: dict[str, list[int]] = {}
    for result in source_results:
        action_grades_by_edge.setdefault(str(result["edge_node_id"]), []).append(
            int(result["action_grade"])
        )

    return {
        "summary_result_id": summary_result_id,
        "conflict_id": conflict_id,
        "device_id": device_id,
        "window_start_sequence": window_start,
        "window_end_sequence": window_end,
        "result_status": (
            "INCOMPLETE"
            if not sufficient_edge_diversity
            else "PENDING_ARBITRATION"
            if has_conflict
            else "FINAL"
        ),
        "excluded_from_formal_metrics": not sufficient_edge_diversity,
        "incomplete_reason": None if sufficient_edge_diversity else "INSUFFICIENT_EDGE_DIVERSITY",
        "has_conflict": has_conflict,
        "cross_edge_pair_count": len(comparisons),
        "conflict_pair_count": sum(
            1 for comparison in comparisons if comparison["is_conflict"]
        ),
        "max_grade_gap": max(
            (comparison["grade_gap"] for comparison in comparisons), default=0
        ),
        "edge_node_ids": edge_ids,
        "action_grades_by_edge": action_grades_by_edge,
        "comparisons": comparisons,
        "final_action_grade": final_grade,
        "recommended_action": ACTION_BY_GRADE[final_grade],
        "confidence": min(float(result["confidence"]) for result in source_results),
        "data_quality_score": min(
            float(result["data_quality_score"]) for result in source_results
        ),
        "source_results": source_results,
        "closed_at_ns": int(closed_at_ns),
    }


def build_arbitration_request(window_result: Mapping[str, Any]) -> dict[str, Any]:
    if not window_result.get("has_conflict"):
        raise ValueError("only conflicted windows may be submitted for arbitration")
    return {
        "conflict_id": window_result["conflict_id"],
        "summary_result_id": window_result["summary_result_id"],
        "device_id": window_result["device_id"],
        "window_start_sequence": int(window_result["window_start_sequence"]),
        "window_end_sequence": int(window_result["window_end_sequence"]),
        "comparison": {
            "max_cross_edge_grade_gap": int(window_result["max_grade_gap"]),
            "conflicting_pair_count": int(window_result["conflict_pair_count"]),
        },
        "bearing_results": list(window_result["source_results"]),
    }


def build_incomplete_window_result(
    results: Iterable[Mapping[str, Any]],
    *,
    closed_at_ns: int,
    reason: str = "WINDOW_TIMEOUT",
    expected_bearing_ids: Sequence[str] = EXPECTED_BEARING_IDS,
) -> dict[str, Any]:
    source_results = sorted(
        (dict(result) for result in results), key=lambda item: str(item["bearing_id"])
    )
    if not source_results:
        raise ValueError("an incomplete window must contain at least one result")
    keys = {group_key(result) for result in source_results}
    if len(keys) != 1:
        raise ValueError("all bearing results must belong to the same device window")
    device_id, window_start, window_end = keys.pop()
    actual = {str(result["bearing_id"]) for result in source_results}
    missing = sorted(set(expected_bearing_ids) - actual)
    if not missing:
        raise ValueError("use build_window_result when all expected bearings are present")
    final_grade = max(int(result["action_grade"]) for result in source_results)
    edge_ids = sorted({str(result["edge_node_id"]) for result in source_results})
    return {
        "summary_result_id": stable_id("summary", device_id, window_start, window_end),
        "conflict_id": None,
        "device_id": device_id,
        "window_start_sequence": window_start,
        "window_end_sequence": window_end,
        "result_status": "INCOMPLETE",
        "excluded_from_formal_metrics": True,
        "incomplete_reason": reason,
        "missing_bearing_ids": missing,
        "has_conflict": False,
        "cross_edge_pair_count": 0,
        "conflict_pair_count": 0,
        "max_grade_gap": 0,
        "edge_node_ids": edge_ids,
        "action_grades_by_edge": {
            edge_id: [
                int(result["action_grade"])
                for result in source_results
                if result["edge_node_id"] == edge_id
            ]
            for edge_id in edge_ids
        },
        "comparisons": [],
        "final_action_grade": final_grade,
        "recommended_action": ACTION_BY_GRADE[final_grade],
        "confidence": min(float(result["confidence"]) for result in source_results),
        "data_quality_score": min(
            float(result["data_quality_score"]) for result in source_results
        ),
        "source_results": source_results,
        "closed_at_ns": int(closed_at_ns),
    }
