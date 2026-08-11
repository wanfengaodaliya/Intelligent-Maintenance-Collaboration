"""Build one bearing result from four windows and one device result from expected bearings."""

from __future__ import annotations

from collections import Counter
from statistics import mean

from core.bearing_workflow_contracts import BearingTaskResult, BearingWindowResult, DeviceTaskResult


class DeviceTaskAggregator:
    def __init__(self, *, conflict_grade_span: int = 2):
        self.conflict_grade_span = conflict_grade_span
        self._expected: dict[tuple[str, str], tuple[str, ...]] = {}
        self._bearings: dict[tuple[str, str], dict[str, BearingTaskResult]] = {}

    def register_task(self, device_id: str, task_id: str, expected_bearing_ids: tuple[str, ...]) -> None:
        if not expected_bearing_ids or len(set(expected_bearing_ids)) != len(expected_bearing_ids):
            raise ValueError("expected_bearing_ids must be unique and non-empty")
        key = (device_id, task_id)
        normalized = tuple(expected_bearing_ids)
        previous = self._expected.get(key)
        if previous is not None and previous != normalized:
            raise ValueError("EXPECTED_BEARING_IDS_CONFLICT")
        self._expected[key] = normalized
        self._bearings.setdefault(key, {})

    def add_bearing_result(self, result: BearingTaskResult) -> DeviceTaskResult:
        key = (result.device_id, result.task_id)
        expected = self._expected.get(key)
        if expected is None:
            raise ValueError("TASK_NOT_REGISTERED")
        if result.bearing_id not in expected:
            raise ValueError("UNEXPECTED_BEARING")
        current = self._bearings[key].get(result.bearing_id)
        if current is not None and current != result:
            raise ValueError("BEARING_RESULT_CONFLICT")
        self._bearings[key][result.bearing_id] = result
        return self.snapshot(result.device_id, result.task_id)

    def snapshot(self, device_id: str, task_id: str) -> DeviceTaskResult:
        key = (device_id, task_id)
        expected = self._expected[key]
        by_bearing = self._bearings[key]
        ordered = tuple(by_bearing[item] for item in expected if item in by_bearing)
        if len(ordered) != len(expected):
            return DeviceTaskResult(
                result_id="device_task_%s_%s" % (task_id, device_id),
                device_id=device_id,
                task_id=task_id,
                expected_bearing_ids=expected,
                bearing_results=ordered,
                status="WAITING",
                action_grade=None,
                conflict=False,
            )
        grades = [item.recommended_action_grade for item in ordered]
        reasons: list[str] = []
        if max(grades) - min(grades) >= self.conflict_grade_span:
            reasons.append("DEVICE_ACTION_GRADE_CONFLICT")
        if any("UNSTABLE_TREND" in item.rule_facts for item in ordered):
            reasons.append("UNSTABLE_BEARING_TREND")
        return DeviceTaskResult(
            result_id="device_task_%s_%s" % (task_id, device_id),
            device_id=device_id,
            task_id=task_id,
            expected_bearing_ids=expected,
            bearing_results=ordered,
            status="REVIEW_REQUIRED" if reasons else "READY",
            action_grade=max(grades),
            conflict=bool(reasons),
            conflict_reasons=tuple(reasons),
            decision_source="PENDING_CLOUD" if reasons else "EDGE",
        )


def build_bearing_task_result(windows: tuple[BearingWindowResult, ...]) -> BearingTaskResult:
    if len(windows) != 4:
        raise ValueError("four final windows are required")
    windows = tuple(sorted(windows, key=lambda item: item.window_index))
    grades = [item.action_grade for item in windows]
    counts = Counter(grades)
    persistent = max((grade for grade, count in counts.items() if count >= 2), default=max(grades))
    direction_changes = sum(
        1 for index in range(1, len(grades) - 1)
        if (grades[index] - grades[index - 1]) * (grades[index + 1] - grades[index]) < 0
    )
    facts: list[str] = []
    if grades == sorted(grades) and grades[0] != grades[-1]:
        trend = "WORSENING"
        recommended = grades[-1]
    elif direction_changes and max(grades) - min(grades) >= 2:
        trend = "UNSTABLE"
        recommended = max(grades)
        facts.append("UNSTABLE_TREND")
    elif counts[max(grades)] == 1 and max(grades) - persistent >= 2:
        trend = "TRANSIENT_HIGH_RISK"
        recommended = max(grades)
        facts.append("TRANSIENT_HIGH_RISK")
    else:
        trend = "STABLE"
        recommended = max(persistent, grades[-1])
    if any(item.result_source == "FINAL_CLOUD" for item in windows):
        facts.append("CLOUD_REVIEW_CONFIRMED")
    action = windows[-1]
    return BearingTaskResult(
        result_id="bearing_task_%s_%s" % (action.task_id, action.bearing_id),
        device_id=action.device_id,
        task_id=action.task_id,
        bearing_id=action.bearing_id,
        sender_id=action.sender_id,
        window_results=windows,
        latest_action_grade=grades[-1],
        max_action_grade=max(grades),
        persistent_action_grade=persistent,
        recommended_action_grade=recommended,
        confidence=round(mean(item.confidence for item in windows), 4),
        data_quality_score=round(mean(item.data_quality_score for item in windows), 4),
        trend=trend,
        rule_facts=tuple(facts),
    )
