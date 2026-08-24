"""Route decisions translated into the V1.2 bearing-result lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    CloudBearingResult,
    EdgeBearingResult,
    PacketRoute,
)

from .repository import BearingResultRepository


class BearingResultLifecycleManager:
    def __init__(self, repository: BearingResultRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> BearingResultRepository:
        return self._repository

    def apply_route(
        self, edge_result: EdgeBearingResult, route_decision: dict, *, accepted_at_ns: int
    ) -> BearingDecisionResult:
        self._validate_identity(edge_result, route_decision)
        try:
            route = PacketRoute(route_decision["route"])
        except (KeyError, ValueError) as exc:
            raise ValueError("unsupported route decision") from exc

        lifecycle_state, decision_source, review_status, degraded = _ROUTE_LIFECYCLE[route]
        instruction = route_decision.get("result_instruction")
        expected_instruction = {
            "result_status": _RESULT_STATUS[route],
            "review_status": review_status,
            "degraded": degraded,
        }
        # Scheduler 的指令包含额外字段（如 decision_source），逐字段比对语义即可。
        if not isinstance(instruction, dict) or any(
            instruction.get(field) != value
            for field, value in expected_instruction.items()
        ):
            raise ValueError("route decision result instruction is inconsistent")

        draft = BearingDecisionResult(
            result_id="pending",
            revision=1,
            replaces_result_id=None,
            device_id=edge_result.device_id,
            task_id=edge_result.task_id,
            bearing_id=edge_result.bearing_id,
            sender_id=edge_result.sender_id,
            decision_round_id=edge_result.decision_round_id,
            diagnosis_window_id=edge_result.diagnosis_window_id,
            lifecycle_state=lifecycle_state,
            bearing_state=edge_result.bearing_state,
            confidence=edge_result.confidence,
            data_quality_score=edge_result.data_quality_score,
            risk_level=edge_result.risk_level,
            action_grade=edge_result.action_grade,
            recommended_action=edge_result.recommended_action,
            decision_source=decision_source,
            review_status=review_status,
            degraded=degraded,
            edge_result_id=edge_result.result_id,
            cloud_result_id=None,
            model_version=edge_result.model_version,
            created_at_ns=edge_result.created_at_ns,
            edge_accepted_at_ns=accepted_at_ns,
        )
        return self._repository.save_revision(draft)

    def apply_failed_route(
        self,
        *,
        device_id: str,
        task_id: str,
        bearing_id: str,
        sender_id: str,
        decision_round_id: str,
        diagnosis_window_id: str,
        route_decision: dict,
        accepted_at_ns: int,
        window_end_ns: int,
        model_version: str = "unavailable",
    ) -> BearingDecisionResult:
        """Persist a placeholder bearing decision for a packet that failed at the edge.

        失败包没有真实边缘诊断；占位记录表明该轴承正等待云复核，
        使延迟的云复核结果能按既有 apply_cloud_result 路径闭环。
        """
        try:
            route = PacketRoute(route_decision["route"])
        except (KeyError, ValueError) as exc:
            raise ValueError("unsupported route decision") from exc
        if route not in {PacketRoute.CLOUD_NOW, PacketRoute.DEFER}:
            raise ValueError("failed packet route must require cloud review")
        lifecycle_state, decision_source, review_status, degraded = _ROUTE_LIFECYCLE[route]
        instruction = route_decision.get("result_instruction")
        expected_instruction = {
            "result_status": _RESULT_STATUS[route],
            "review_status": review_status,
            "degraded": degraded,
        }
        if not isinstance(instruction, dict) or any(
            instruction.get(field) != value
            for field, value in expected_instruction.items()
        ):
            raise ValueError("route decision result instruction is inconsistent")
        draft = BearingDecisionResult(
            result_id="pending",
            revision=1,
            replaces_result_id=None,
            device_id=device_id,
            task_id=task_id,
            bearing_id=bearing_id,
            sender_id=sender_id,
            decision_round_id=decision_round_id,
            diagnosis_window_id=diagnosis_window_id,
            lifecycle_state=lifecycle_state,
            bearing_state="unknown",
            confidence=0.0,
            data_quality_score=0.0,
            risk_level="unknown",
            action_grade=0,
            recommended_action="await_cloud_review",
            decision_source=decision_source,
            review_status=review_status,
            degraded=degraded,
            edge_result_id="edge_unavailable",
            cloud_result_id=None,
            model_version=model_version,
            created_at_ns=window_end_ns,
            edge_accepted_at_ns=accepted_at_ns,
        )
        return self._repository.save_revision(draft)

    def apply_cloud_result(
        self,
        cloud_result: CloudBearingResult,
        *,
        accepted_at_ns: int,
        round_open: bool | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> BearingDecisionResult:
        current = self._repository.get_current(
            cloud_result.device_id,
            cloud_result.task_id,
            cloud_result.decision_round_id,
            cloud_result.bearing_id,
            connection=connection,
        )
        if current is None:
            raise ValueError("cloud result has no matching edge bearing decision")
        for field in (
            "device_id",
            "task_id",
            "bearing_id",
            "sender_id",
            "decision_round_id",
            "diagnosis_window_id",
        ):
            if getattr(current, field) != getattr(cloud_result, field):
                raise ValueError(f"cloud result identity mismatch: {field}")
        if current.lifecycle_state not in {
            BearingLifecycleStatus.WAITING_CLOUD,
            BearingLifecycleStatus.PROVISIONAL,
        }:
            raise ValueError("cloud result cannot replace a final bearing decision")

        accepted_before_round_close = (
            current.lifecycle_state is BearingLifecycleStatus.WAITING_CLOUD
            if round_open is None
            else round_open
        )
        if accepted_before_round_close:
            lifecycle_state = BearingLifecycleStatus.FINAL_CLOUD
        elif _conclusion_matches(current, cloud_result):
            # 迟到结果与暂定结论一致：记录确认，不产生实质性修正。
            lifecycle_state = BearingLifecycleStatus.LATE_CLOUD_CONFIRMED
        else:
            lifecycle_state = BearingLifecycleStatus.LATE_CLOUD_CORRECTED
        draft = BearingDecisionResult(
            result_id="pending",
            revision=1,
            replaces_result_id=None,
            device_id=current.device_id,
            task_id=current.task_id,
            bearing_id=current.bearing_id,
            sender_id=current.sender_id,
            decision_round_id=current.decision_round_id,
            diagnosis_window_id=current.diagnosis_window_id,
            lifecycle_state=lifecycle_state,
            bearing_state=cloud_result.bearing_state,
            confidence=cloud_result.confidence,
            data_quality_score=cloud_result.data_quality_score,
            risk_level=cloud_result.risk_level,
            action_grade=cloud_result.action_grade,
            recommended_action=cloud_result.recommended_action,
            decision_source="CLOUD",
            review_status="REVIEWED",
            degraded=False,
            edge_result_id=current.edge_result_id,
            cloud_result_id=cloud_result.result_id,
            model_version=cloud_result.model_version,
            created_at_ns=cloud_result.created_at_ns,
            edge_accepted_at_ns=current.edge_accepted_at_ns,
        )
        return self._repository.save_revision(draft, connection=connection)

    def promote_timed_out_cloud_now(self, result: BearingDecisionResult) -> BearingDecisionResult:
        if result.lifecycle_state is not BearingLifecycleStatus.WAITING_CLOUD:
            raise ValueError("only WAITING_CLOUD may become provisional")
        current = self._repository.get_current(
            result.device_id, result.task_id, result.decision_round_id, result.bearing_id
        )
        if current != result:
            raise ValueError("waiting cloud result is no longer current")
        return self._repository.save_revision(
            replace(
                result,
                result_id="pending",
                revision=1,
                replaces_result_id=None,
                lifecycle_state=BearingLifecycleStatus.PROVISIONAL,
                decision_source="PROVISIONAL_EDGE",
                review_status="PENDING_CLOUD",
                degraded=True,
            )
        )

    @staticmethod
    def _validate_identity(edge_result: EdgeBearingResult, route_decision: dict) -> None:
        for field in (
            "device_id",
            "task_id",
            "bearing_id",
            "decision_round_id",
            "diagnosis_window_id",
        ):
            if route_decision.get(field) != getattr(edge_result, field):
                raise ValueError(f"route decision identity mismatch: {field}")


def _conclusion_matches(current: BearingDecisionResult, cloud_result: CloudBearingResult) -> bool:
    """判断迟到云端结论与当前暂定结论是否实质一致。"""
    return (
        current.bearing_state == cloud_result.bearing_state
        and current.risk_level == cloud_result.risk_level
        and current.action_grade == cloud_result.action_grade
        and current.recommended_action == cloud_result.recommended_action
    )


_ROUTE_LIFECYCLE = {
    PacketRoute.EDGE: (
        BearingLifecycleStatus.FINAL_EDGE,
        "FINAL_EDGE",
        "NOT_REQUIRED",
        False,
    ),
    PacketRoute.CLOUD_NOW: (
        BearingLifecycleStatus.WAITING_CLOUD,
        "EDGE",
        "PENDING_CLOUD",
        False,
    ),
    PacketRoute.DEFER: (
        BearingLifecycleStatus.PROVISIONAL,
        "PROVISIONAL_EDGE",
        "PENDING_CLOUD",
        True,
    ),
}

_RESULT_STATUS = {
    PacketRoute.EDGE: "FINAL",
    PacketRoute.CLOUD_NOW: "WAITING_CLOUD",
    PacketRoute.DEFER: "PROVISIONAL",
}
