# -*- coding: utf-8 -*-
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from edge_model.contracts import EdgeResult, PacketExecutionCompleted


DIRECT_FINAL_TO_SUMMARY = "DIRECT_FINAL_TO_SUMMARY"
CLOUD_REVIEW_NOW = "CLOUD_REVIEW_NOW"
EDGE_PROVISIONAL_AND_DEFER_CLOUD = "EDGE_PROVISIONAL_AND_DEFER_CLOUD"
ROUTES = {
    DIRECT_FINAL_TO_SUMMARY,
    CLOUD_REVIEW_NOW,
    EDGE_PROVISIONAL_AND_DEFER_CLOUD,
}

RESULT_FINAL = "FINAL"
RESULT_PROVISIONAL = "PROVISIONAL"

_RESULT_SCORE = {"normal": 0, "warning": 1, "fault": 2}
_RISK_SCORE = {"low": 0, "medium": 1, "high": 2}


def action_level_for(edge_result: str, risk_level: str) -> int:
    try:
        return min(4, _RESULT_SCORE[edge_result] + _RISK_SCORE[risk_level])
    except KeyError as exc:
        raise ValueError("edge_result 或 risk_level 不受支持") from exc


def task_complexity_for(confidence: float) -> float:
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
            or not math.isfinite(float(confidence)) or not 0 <= confidence <= 1:
        raise ValueError("confidence 必须是0到1的有限数")
    return 1.0 - float(confidence)


@dataclass(frozen=True)
class ExternalError:
    code: str
    stage: str
    retryable: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "retryable": self.retryable,
            "message": self.message,
        }


@dataclass(frozen=True)
class PacketAnalysisReport:
    dispatch_id: str
    edge_node_id: str
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    packet_id: str
    sequence_number: int
    status: str
    started_at_ns: int
    finished_at_ns: int
    raw_data_ref: Optional[str]
    context_ref: Optional[str]
    output: Optional[dict[str, Any]]
    error: Optional[ExternalError]

    @classmethod
    def from_completion(
        cls,
        completion: PacketExecutionCompleted,
        *,
        dispatch_id: str,
        edge_node_id: str,
        raw_data_ref: Optional[str],
        context_ref: Optional[str],
    ) -> "PacketAnalysisReport":
        output = None
        error = None
        if completion.edge is not None:
            complexity = task_complexity_for(completion.edge.confidence)
            output = {
                **completion.edge.as_dict(),
                "task_complexity": complexity,
                "action_level": action_level_for(
                    completion.edge.edge_result, completion.edge.edge_risk_level
                ),
            }
        else:
            error = ExternalError(
                code=completion.error_code or "EDGE_PROCESSING_FAILED",
                stage="EDGE_PROCESSING",
                retryable=False,
                message="edge packet processing did not produce a result",
            )
        return cls(
            dispatch_id=dispatch_id,
            edge_node_id=edge_node_id,
            device_id=completion.device_id,
            sender_id=completion.sender_id,
            task_id=completion.task_id,
            bearing_id=completion.bearing_id,
            packet_id=completion.packet_id,
            sequence_number=completion.sequence_number,
            status=completion.status,
            started_at_ns=completion.started_at_ns,
            finished_at_ns=completion.finished_at_ns,
            raw_data_ref=raw_data_ref,
            context_ref=context_ref,
            output=output,
            error=error,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "edge_node_id": self.edge_node_id,
            "device_id": self.device_id,
            "sender_id": self.sender_id,
            "task_id": self.task_id,
            "bearing_id": self.bearing_id,
            "packet_id": self.packet_id,
            "sequence_number": self.sequence_number,
            "status": self.status,
            "started_at_ns": self.started_at_ns,
            "finished_at_ns": self.finished_at_ns,
            "raw_data_ref": self.raw_data_ref,
            "context_ref": self.context_ref,
            "output": self.output,
            "error": self.error.as_dict() if self.error else None,
        }


@dataclass(frozen=True)
class PacketRouteDecision:
    decision_id: str
    dispatch_id: str
    target_edge_node_id: str
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    packet_id: str
    sequence_number: int
    route: str
    reason_codes: tuple[str, ...]
    created_at_ns: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PacketRouteDecision":
        required_strings = (
            "decision_id", "dispatch_id", "target_edge_node_id", "device_id",
            "sender_id", "task_id", "bearing_id", "packet_id", "route",
        )
        values: dict[str, str] = {}
        for field in required_strings:
            item = value.get(field)
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{field} 必须是非空字符串")
            values[field] = item
        if values["route"] not in ROUTES:
            raise ValueError("route 不受支持")
        sequence = value.get("sequence_number")
        created = value.get("created_at_ns")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("sequence_number 必须是正整数")
        if isinstance(created, bool) or not isinstance(created, int) or created < 1:
            raise ValueError("created_at_ns 必须是正整数")
        reasons = value.get("reason_codes", [])
        if not isinstance(reasons, list) or any(
            not isinstance(item, str) or not item for item in reasons
        ):
            raise ValueError("reason_codes 必须是字符串数组")
        return cls(
            **values,
            sequence_number=sequence,
            reason_codes=tuple(reasons),
            created_at_ns=created,
        )


@dataclass(frozen=True)
class CloudPacketReviewInstruction:
    decision_id: str
    cloud_task_id: str
    dispatch_id: str
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    packet_id: str
    raw_data_ref: str
    context_ref: Optional[str]
    cloud_node_id: str
    endpoint: str
    attempt: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CloudPacketReviewInstruction":
        source = value.get("source")
        target = value.get("target")
        if not isinstance(source, Mapping) or not isinstance(target, Mapping):
            raise ValueError("source 和 target 必须是对象")
        flattened = {
            "decision_id": value.get("decision_id"),
            "cloud_task_id": value.get("cloud_task_id"),
            "dispatch_id": value.get("dispatch_id"),
            "device_id": value.get("device_id"),
            "sender_id": value.get("sender_id"),
            "task_id": value.get("task_id"),
            "bearing_id": value.get("bearing_id"),
            "packet_id": value.get("packet_id"),
            "raw_data_ref": source.get("raw_data_ref"),
            "context_ref": source.get("context_ref"),
            "cloud_node_id": target.get("cloud_node_id"),
            "endpoint": target.get("endpoint"),
        }
        for field, item in flattened.items():
            if field == "context_ref" and item is None:
                continue
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{field} 必须是非空字符串")
        attempt = value.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt 必须是正整数")
        return cls(**flattened, attempt=attempt)


@dataclass(frozen=True)
class SummaryPacketResult:
    result_id: str
    result_version: int
    supersedes_result_id: Optional[str]
    dispatch_id: str
    decision_id: str
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    packet_id: str
    sequence_number: int
    result_status: str
    decision_source: str
    review_status: str
    edge: EdgeResult
    action_level: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "result_version": self.result_version,
            "supersedes_result_id": self.supersedes_result_id,
            "dispatch_id": self.dispatch_id,
            "decision_id": self.decision_id,
            "device_id": self.device_id,
            "sender_id": self.sender_id,
            "task_id": self.task_id,
            "bearing_id": self.bearing_id,
            "packet_id": self.packet_id,
            "sequence_number": self.sequence_number,
            "result_status": self.result_status,
            "decision_source": self.decision_source,
            "review_status": self.review_status,
            "result": self.edge.edge_result,
            "confidence": self.edge.confidence,
            "risk_level": self.edge.edge_risk_level.upper(),
            "action_level": self.action_level,
            "model_version": self.edge.model_version,
        }
