"""The runtime bridge from V1.2 bearing lifecycle to device decision rounds."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from core.diagnosis_contracts import (
    BearingLifecycleStatus,
    CloudBearingResult,
    DeviceDecisionStatus,
    DeviceDecisionResult,
    RoundClosureReason,
    EdgeBearingResult,
)
from core.bearing_actions import ACTION_TO_STATE, grade_for_action
from dataclasses import replace
from device_decision import (
    DeviceDecisionRevisionService,
    DeviceDecisionRoundRepository,
    aggregate_device_round,
)
from result_lifecycle import BearingResultLifecycleManager


class V12DecisionFlow:
    def __init__(
        self,
        lifecycle: BearingResultLifecycleManager,
        device_rounds: DeviceDecisionRoundRepository,
        *,
        round_timeout_ns: int = 3_500_000_000,
        late_correction_retention_ns: int | None = None,
        on_bearing_result: Callable[[Any], None] | None = None,
        on_device_result: Callable[[DeviceDecisionResult], None] | None = None,
        on_device_conflict: Callable[[dict[str, Any]], None] | None = None,
        on_manual_review: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if round_timeout_ns <= 0:
            raise ValueError("round_timeout_ns must be positive")
        self.lifecycle = lifecycle
        self.device_rounds = device_rounds
        self.round_timeout_ns = round_timeout_ns
        self.late_correction_retention_ns = late_correction_retention_ns
        self._on_device_result = on_device_result or (lambda _: None)
        self._on_bearing_result = on_bearing_result or (lambda _: None)
        self._on_device_conflict = on_device_conflict or (lambda _: None)
        self._on_manual_review = on_manual_review or (lambda _: None)
        self._revisions = DeviceDecisionRevisionService(
            device_rounds, lifecycle.repository
        )

    def apply_edge_result(
        self,
        edge_result: EdgeBearingResult,
        route_decision: dict,
        *,
        expected_bearing_ids: tuple[str, ...],
        accepted_at_ns: int,
    ):
        self._ensure_round(edge_result, expected_bearing_ids, accepted_at_ns)
        bearing = self.lifecycle.apply_route(
            edge_result, route_decision, accepted_at_ns=accepted_at_ns
        )
        self._emit_bearing_result(bearing)
        return bearing, self._close_if_complete(
            edge_result.device_id, edge_result.task_id, edge_result.decision_round_id, accepted_at_ns
        )

    def apply_cloud_result(
        self, cloud_result: CloudBearingResult, *, accepted_at_ns: int
    ):
        timeout_result = None
        with self.device_rounds.transaction() as connection:
            round_state = self.device_rounds.get_round(
                cloud_result.device_id,
                cloud_result.task_id,
                cloud_result.decision_round_id,
                connection=connection,
            )
            if round_state is None:
                raise ValueError("cloud result has no device round")
            round_open = round_state["state"] == "OPEN"
            accepted_before_deadline = accepted_at_ns < round_state["deadline_at_ns"]
            if round_open and not accepted_before_deadline:
                timeout_result = self._close_timeout(
                    round_state, now_ns=accepted_at_ns, connection=connection
                )
            bearing = self.lifecycle.apply_cloud_result(
                cloud_result,
                accepted_at_ns=accepted_at_ns,
                round_open=round_open and accepted_before_deadline,
                connection=connection,
            )
            device = timeout_result or self._close_if_complete(
                cloud_result.device_id,
                cloud_result.task_id,
                cloud_result.decision_round_id,
                accepted_at_ns,
                connection=connection,
            )
        if device is not None:
            self._emit_device_result(device)
        else:
            device = self._revisions.correct_closed_round(
                device_id=cloud_result.device_id,
                task_id=cloud_result.task_id,
                decision_round_id=cloud_result.decision_round_id,
                now_ns=accepted_at_ns,
            )
            if device is not None:
                self._emit_device_result(device)
        if bearing.lifecycle_state is not BearingLifecycleStatus.LATE_CLOUD_CONFIRMED:
            # 迟到且结论一致的确认不向下游重复通知。
            self._emit_bearing_result(bearing)
        return bearing, device

    def promote_cloud_now_timeouts(
        self, *, now_ns: int, cloud_now_timeout_ns: int
    ) -> tuple[DeviceDecisionResult, ...]:
        closed: list[DeviceDecisionResult] = []
        for waiting in self.lifecycle.repository.list_waiting_cloud_due(
            now_ns=now_ns, cloud_now_timeout_ns=cloud_now_timeout_ns
        ):
            provisional = self.lifecycle.promote_timed_out_cloud_now(waiting)
            device = self._close_if_complete(
                provisional.device_id,
                provisional.task_id,
                provisional.decision_round_id,
                now_ns,
            )
            if device is not None:
                closed.append(device)
        return tuple(closed)

    def finalize_timeouts(
        self, *, now_ns: int, round_timeout_ns: int | None = None
    ) -> tuple[DeviceDecisionResult, ...]:
        """Use database CAS to seal every due open round exactly once."""
        if round_timeout_ns is not None and round_timeout_ns != self.round_timeout_ns:
            raise ValueError("round_timeout_ns differs from the persisted round policy")
        closed: list[DeviceDecisionResult] = []
        for round_state in self.device_rounds.list_open_due(now_ns=now_ns):
            with self.device_rounds.transaction() as connection:
                current_round = self.device_rounds.get_round(
                    round_state["device_id"],
                    round_state["task_id"],
                    round_state["decision_round_id"],
                    connection=connection,
                )
                if current_round is None or current_round["state"] != "OPEN":
                    continue
                result = self._close_timeout(
                    current_round, now_ns=now_ns, connection=connection
                )
            if result is None:
                continue
            self._emit_device_result(result)
            closed.append(result)
        return tuple(closed)

    def _ensure_round(
        self,
        result: EdgeBearingResult,
        expected_bearing_ids: tuple[str, ...],
        opened_at_ns: int,
    ) -> None:
        self.device_rounds.register_round(
            device_id=result.device_id,
            task_id=result.task_id,
            decision_round_id=result.decision_round_id,
            expected_bearing_ids=expected_bearing_ids,
            opened_at_ns=opened_at_ns,
            deadline_at_ns=opened_at_ns + self.round_timeout_ns,
        )

    def _close_timeout(
        self,
        round_state: dict,
        *,
        now_ns: int,
        connection: sqlite3.Connection,
    ) -> DeviceDecisionResult | None:
        bearings = self.lifecycle.repository.list_current_round(
            round_state["device_id"],
            round_state["task_id"],
            round_state["decision_round_id"],
            connection=connection,
        )
        if not bearings:
            return None
        return self.device_rounds.close_round_and_save_initial_result(
            aggregate_device_round(
                bearings,
                expected_bearing_ids=round_state["expected_bearing_ids"],
                closure_reason=RoundClosureReason.ROUND_TIMEOUT,
                closed_at_ns=now_ns,
            ),
            expected_version=round_state["version"],
            connection=connection,
        )

    def _close_if_complete(
        self,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        now_ns: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> DeviceDecisionResult | None:
        round_state = self.device_rounds.get_round(
            device_id, task_id, decision_round_id, connection=connection
        )
        if round_state is None or round_state["state"] != "OPEN":
            return None
        bearings = self.lifecycle.repository.list_current_round(
            device_id, task_id, decision_round_id, connection=connection
        )
        if {item.bearing_id for item in bearings} != set(round_state["expected_bearing_ids"]):
            return None
        if any(item.lifecycle_state.value == "WAITING_CLOUD" for item in bearings):
            return None
        closure_reason = (
            RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL
            if any(item.lifecycle_state.value == "PROVISIONAL" for item in bearings)
            else RoundClosureReason.ALL_BEARINGS_FINAL
        )
        result = self.device_rounds.close_round_and_save_initial_result(
            aggregate_device_round(
                bearings,
                expected_bearing_ids=round_state["expected_bearing_ids"],
                closure_reason=closure_reason,
                closed_at_ns=now_ns,
            ),
            expected_version=round_state["version"],
            connection=connection,
        )
        if result is None:
            return None
        if connection is None:
            self._emit_device_result(result)
        return result

    def _emit_device_result(self, result: DeviceDecisionResult) -> None:
        self._on_device_result(result)
        if result.has_conflict and result.arbitration_id is None:
            self._on_device_conflict(self._arbitration_request(result))

    def _emit_bearing_result(self, result: Any) -> None:
        try:
            self._on_bearing_result(result)
        except Exception:
            pass

    def apply_cloud_arbitration_result(
        self, payload: dict[str, Any], *, accepted_at_ns: int
    ) -> DeviceDecisionResult | None:
        """Persist a cloud-arbitrated device revision without reopening a round."""

        device_id = _required_text(payload, "device_id")
        task_id = _required_text(payload, "task_id")
        decision_round_id = _required_text(payload, "decision_round_id")
        revision = _required_positive_int(payload, "device_result_revision")
        arbitration_id = _required_text(payload, "arbitration_id")
        receipt = self.device_rounds.get_arbitration_receipt(arbitration_id)
        if receipt is not None:
            # 重复回调：直接返回已处理结果，不再修改状态也不再发布。
            return self.device_rounds.get_current_result(
                device_id, task_id, decision_round_id
            )
        current = self.device_rounds.get_current_result(
            device_id, task_id, decision_round_id
        )
        round_state = self.device_rounds.get_round(device_id, task_id, decision_round_id)
        if current is None or round_state is None:
            raise ValueError("cloud arbitration result has no device round")
        if current.revision != revision:
            raise ValueError("cloud arbitration result is stale")
        if round_state["closure_reason"] == RoundClosureReason.ROUND_TIMEOUT.value:
            return None
        closed_at_ns = round_state.get("closed_at_ns")
        if (
            self.late_correction_retention_ns is not None
            and isinstance(closed_at_ns, int)
            and accepted_at_ns - closed_at_ns > self.late_correction_retention_ns
        ):
            self._on_manual_review(
                {
                    "stage": "late_correction_retention",
                    "device_id": device_id,
                    "task_id": task_id,
                    "decision_round_id": decision_round_id,
                    "arbitration_id": arbitration_id,
                    "error_code": "LATE_CORRECTION_BEYOND_RETENTION",
                    "action": "manual_review_required",
                }
            )
            raise ValueError("cloud arbitration arrived beyond the retention window")
        action = _required_text(payload, "final_action")
        action_grade = grade_for_action(action)
        confidence = _score(payload.get("confidence"), "confidence")
        status = (
            DeviceDecisionStatus.CORRECTED
            if round_state["closure_reason"]
            == RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL.value
            else DeviceDecisionStatus.FINAL
        )
        revised = replace(
            current,
            status=status,
            final_state=ACTION_TO_STATE[action],
            final_action_grade=action_grade,
            final_action=action,
            confidence=confidence,
            decision_source="CLOUD_ARBITRATION",
            degraded=False,
            affects_realtime_action=status is not DeviceDecisionStatus.CORRECTED,
            arbitration_id=arbitration_id,
            created_at_ns=accepted_at_ns,
            closed_at_ns=accepted_at_ns,
        )
        saved = self.device_rounds.save_revision(revised)
        self.device_rounds.save_arbitration_receipt(
            arbitration_id=arbitration_id,
            device_id=device_id,
            task_id=task_id,
            decision_round_id=decision_round_id,
            result_id=saved.result_id,
            processed_at_ns=accepted_at_ns,
        )
        self._emit_device_result(saved)
        return saved

    def _arbitration_request(self, result: DeviceDecisionResult) -> dict[str, Any]:
        bearings = self.lifecycle.repository.list_current_round(
            result.device_id, result.task_id, result.decision_round_id
        )
        by_bearing = {item.bearing_id: item for item in bearings}
        ordered = [by_bearing[bearing_id] for bearing_id in result.expected_bearing_ids]
        if tuple(item.result_id for item in ordered) != result.bearing_result_ids:
            raise ValueError("current bearing revisions do not match device result")
        action_levels = [item.action_grade for item in ordered]
        confidences = [item.confidence for item in ordered]
        provisional_count = sum(
            item.lifecycle_state.value == "PROVISIONAL" for item in ordered
        )
        return {
            "device_id": result.device_id,
            "task_id": result.task_id,
            "decision_round_id": result.decision_round_id,
            "device_result_revision": result.revision,
            "bearing_result_ids": list(result.bearing_result_ids),
            "expected_bearing_count": len(result.expected_bearing_ids),
            "received_bearing_count": len(ordered),
            "bearing_results": [
                {
                    "bearing_id": item.bearing_id,
                    "bearing_result_id": item.result_id,
                    "result": item.bearing_state,
                    "confidence": item.confidence,
                    "risk_level": item.risk_level,
                    "action_level": item.action_grade,
                    "result_status": (
                        "PROVISIONAL"
                        if item.lifecycle_state.value == "PROVISIONAL"
                        else "FINAL"
                    ),
                }
                for item in ordered
            ],
            "comparison": {
                "conflict": result.has_conflict,
                "conflict_type": "ACTION_SPAN" if result.has_conflict else None,
                "action_level_min": min(action_levels),
                "action_level_max": max(action_levels),
                "action_level_span": max(action_levels) - min(action_levels),
                "aggregate_confidence": min(confidences),
                "low_confidence_bearing_count": sum(value < 0.8 for value in confidences),
                "provisional_bearing_count": provisional_count,
                "data_complete": len(ordered) == len(result.expected_bearing_ids),
            },
            "task_complexity": round(1.0 - min(confidences), 6),
            "local_arbitration_supported": True,
        }


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _required_positive_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be in [0, 1]")
    return result
