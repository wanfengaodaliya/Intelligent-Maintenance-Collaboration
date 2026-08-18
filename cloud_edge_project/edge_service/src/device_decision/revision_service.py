"""Persist historical device corrections without reopening a timeout-sealed round."""

from __future__ import annotations

from dataclasses import replace

from core.diagnosis_contracts import DeviceDecisionStatus, RoundClosureReason
from result_lifecycle import BearingResultRepository

from .aggregator import aggregate_device_round
from .repository import DeviceDecisionRoundRepository


class DeviceDecisionRevisionService:
    def __init__(
        self,
        round_repository: DeviceDecisionRoundRepository,
        bearing_repository: BearingResultRepository,
    ) -> None:
        self._rounds = round_repository
        self._bearings = bearing_repository

    def correct_closed_round(
        self, *, device_id: str, task_id: str, decision_round_id: str, now_ns: int
    ):
        round_state = self._rounds.get_round(device_id, task_id, decision_round_id)
        if round_state is None or round_state["state"] != "CLOSED":
            return None
        if round_state["closure_reason"] == RoundClosureReason.ROUND_TIMEOUT.value:
            return None
        bearings = self._bearings.list_current_round(device_id, task_id, decision_round_id)
        if {item.bearing_id for item in bearings} != set(round_state["expected_bearing_ids"]):
            return None
        aggregate = aggregate_device_round(
            bearings,
            expected_bearing_ids=round_state["expected_bearing_ids"],
            closure_reason=RoundClosureReason(round_state["closure_reason"]),
            closed_at_ns=now_ns,
        )
        current = self._rounds.get_current_result(device_id, task_id, decision_round_id)
        if (
            current is not None
            and current.status is DeviceDecisionStatus.CORRECTED
            and _conclusion_unchanged(current, aggregate)
        ):
            # 重复的迟到结果只是确认了已有修正结论，不产生新的修正版本，也不重复发布。
            return None
        corrected = replace(
            aggregate,
            status=DeviceDecisionStatus.CORRECTED,
            decision_source="HISTORICAL_CORRECTION",
            affects_realtime_action=False,
            created_at_ns=now_ns,
        )
        return self._rounds.save_revision(corrected)


def _conclusion_unchanged(current, candidate) -> bool:
    """比较两个设备级结果的结论字段是否实质一致。"""
    return (
        current.final_state == candidate.final_state
        and current.final_action_grade == candidate.final_action_grade
        and current.final_action == candidate.final_action
        and current.has_conflict == candidate.has_conflict
    )
