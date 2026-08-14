"""The runtime bridge from V1.2 bearing lifecycle to device decision rounds."""

from __future__ import annotations

from typing import Callable

from core.diagnosis_contracts import (
    CloudBearingResult,
    DeviceDecisionResult,
    RoundClosureReason,
    EdgeBearingResult,
)
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
        on_device_result: Callable[[DeviceDecisionResult], None] | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.device_rounds = device_rounds
        self._on_device_result = on_device_result or (lambda _: None)
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
        return bearing, self._close_if_complete(
            edge_result.device_id, edge_result.task_id, edge_result.decision_round_id, accepted_at_ns
        )

    def apply_cloud_result(
        self, cloud_result: CloudBearingResult, *, accepted_at_ns: int
    ):
        round_state = self.device_rounds.get_round(
            cloud_result.device_id, cloud_result.task_id, cloud_result.decision_round_id
        )
        bearing = self.lifecycle.apply_cloud_result(
            cloud_result,
            accepted_at_ns=accepted_at_ns,
            round_open=round_state is not None and round_state["state"] == "OPEN",
        )
        device = self._close_if_complete(
            cloud_result.device_id, cloud_result.task_id, cloud_result.decision_round_id, accepted_at_ns
        )
        if device is None:
            device = self._revisions.correct_closed_round(
                device_id=cloud_result.device_id,
                task_id=cloud_result.task_id,
                decision_round_id=cloud_result.decision_round_id,
                now_ns=accepted_at_ns,
            )
            if device is not None:
                self._on_device_result(device)
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
        self, *, now_ns: int, round_timeout_ns: int
    ) -> tuple[DeviceDecisionResult, ...]:
        """Use database CAS to seal every due open round exactly once."""
        closed: list[DeviceDecisionResult] = []
        for round_state in self.device_rounds.list_open_due(
            now_ns=now_ns, round_timeout_ns=round_timeout_ns
        ):
            if not self.device_rounds.close_round(
                device_id=round_state["device_id"],
                task_id=round_state["task_id"],
                decision_round_id=round_state["decision_round_id"],
                expected_version=round_state["version"],
                closure_reason=RoundClosureReason.ROUND_TIMEOUT,
                closed_at_ns=now_ns,
            ):
                continue
            bearings = self.lifecycle.repository.list_current_round(
                round_state["device_id"], round_state["task_id"], round_state["decision_round_id"]
            )
            if not bearings:
                continue
            result = self.device_rounds.save_revision(
                aggregate_device_round(
                    bearings,
                    expected_bearing_ids=round_state["expected_bearing_ids"],
                    closure_reason=RoundClosureReason.ROUND_TIMEOUT,
                    closed_at_ns=now_ns,
                )
            )
            self._on_device_result(result)
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
        )

    def _close_if_complete(
        self, device_id: str, task_id: str, decision_round_id: str, now_ns: int
    ) -> DeviceDecisionResult | None:
        round_state = self.device_rounds.get_round(device_id, task_id, decision_round_id)
        if round_state is None or round_state["state"] != "OPEN":
            return None
        bearings = self.lifecycle.repository.list_current_round(
            device_id, task_id, decision_round_id
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
        if not self.device_rounds.close_round(
            device_id=device_id,
            task_id=task_id,
            decision_round_id=decision_round_id,
            expected_version=round_state["version"],
            closure_reason=closure_reason,
            closed_at_ns=now_ns,
        ):
            return None
        result = self.device_rounds.save_revision(
            aggregate_device_round(
                bearings,
                expected_bearing_ids=round_state["expected_bearing_ids"],
                closure_reason=closure_reason,
                closed_at_ns=now_ns,
            )
        )
        self._on_device_result(result)
        return result
