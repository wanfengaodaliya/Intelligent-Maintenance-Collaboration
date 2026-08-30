"""API contract validation helpers.

The public JSON shapes are defined by docs/api.md. This module keeps validation
small and explicit so the core demo can run without web-framework dependencies.

Bearing-specific signal contracts (``data_type == "bearing_timeseries"``,
``vibration`` / ``bearing_id`` / operating-context field validation, and the
legacy sensor-packet envelopes) have been moved to
``scenarios.bearing.cloud.context.signal_contracts``. They are re-exported at
the bottom of this module so existing ``from common.schemas import ...`` sites
and the documented error codes / JSON shapes stay unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


ROUTES = {"edge", "cloud", "fallback_edge"}
V01_ROUTES = {"edge", "fog", "cloud", "fallback_edge", "edge_cloud"}
LEGACY_ENVELOPE_FIELDS = {"packet_id", "packet", "cloud_raw_packet"}
LABELS = {"normal", "fault"}
RISK_LEVELS = {"low", "medium", "high"}


class ContractError(ValueError):
    """Raised when an API payload does not match docs/api.md."""

    def __init__(self, code: str, message: str, packet_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.packet_id = packet_id


def error_response(error: ContractError) -> dict[str, Any]:
    return {
        "success": False,
        "packet_id": error.packet_id,
        "error_code": error.code,
        "message": error.message,
    }


def require_mapping(value: Any, name: str, packet_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("INVALID_JSON", f"{name} must be an object", packet_id)
    return value


def require_field(payload: dict[str, Any], field: str, packet_id: str | None = None) -> Any:
    if field not in payload:
        raise ContractError("MISSING_FIELD", f"missing field: {field}", packet_id)
    return payload[field]


def require_number(value: Any, field: str, packet_id: str | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError("INVALID_PACKET", f"{field} must be a number", packet_id)
    number = float(value)
    if not math.isfinite(number):
        raise ContractError("INVALID_PACKET", f"{field} must be a finite number", packet_id)
    return number


def require_int(value: Any, field: str, packet_id: str | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError("INVALID_PACKET", f"{field} must be an integer", packet_id)
    return value


def require_non_empty_string(value: Any, field: str, packet_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("INVALID_PACKET", f"{field} must be a non-empty string", packet_id)
    return value


def require_confidence(value: Any, field: str, packet_id: str | None = None) -> float:
    confidence = require_number(value, field, packet_id)
    if not 0 <= confidence <= 1:
        raise ContractError("INVALID_PACKET", f"{field} must be between 0 and 1", packet_id)
    return confidence


def is_v01_task_request(payload: Any) -> bool:
    """Recognize V0.1 edge intent without requiring a complete request."""

    return (
        isinstance(payload, dict)
        and "task_id" in payload
        and not LEGACY_ENVELOPE_FIELDS.intersection(payload)
    )


def is_v01_cloud_request(payload: Any) -> bool:
    """Recognize V0.1 cloud intent without requiring a complete request."""

    return (
        isinstance(payload, dict)
        and "task_id" in payload
        and not LEGACY_ENVELOPE_FIELDS.intersection(payload)
    )


def is_v01_schedule_request(payload: Any) -> bool:
    """Recognize V0.1 scheduling intent from any documented nested key."""

    return isinstance(payload, dict) and any(
        field in payload for field in ("task", "edge_result", "network_state", "node_state")
    )


def validate_task_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the V0.1 TaskRequest contract."""

    task = require_mapping(payload, "TaskRequest")
    task_id = require_non_empty_string(require_field(task, "task_id"), "task_id")
    scenario = require_field(task, "scenario", task_id)
    if scenario not in {"industrial", "energy"}:
        raise ContractError("INVALID_PACKET", "scenario must be industrial or energy", task_id)
    for field in ("source_node", "task_type", "timestamp"):
        require_non_empty_string(require_field(task, field, task_id), field, task_id)
    if require_int(require_field(task, "deadline_ms", task_id), "deadline_ms", task_id) <= 0:
        raise ContractError("INVALID_PACKET", "deadline_ms must be greater than 0", task_id)
    require_confidence(require_field(task, "priority", task_id), "priority", task_id)
    if require_number(require_field(task, "data_size_kb", task_id), "data_size_kb", task_id) <= 0:
        raise ContractError("INVALID_PACKET", "data_size_kb must be greater than 0", task_id)
    data = require_mapping(require_field(task, "data", task_id), "data", task_id)
    if not data:
        raise ContractError("INVALID_PACKET", "data must be a non-empty object", task_id)
    return task


def validate_task_log(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the V0.1 TaskLog contract."""

    log = require_mapping(payload, "TaskLog")
    task_id = require_non_empty_string(require_field(log, "task_id"), "task_id")
    scenario = require_field(log, "scenario", task_id)
    if scenario not in {"industrial", "energy"}:
        raise ContractError("INVALID_PACKET", "scenario must be industrial or energy", task_id)
    for field in ("source_node", "timestamp"):
        require_non_empty_string(require_field(log, field, task_id), field, task_id)
    route = require_field(log, "route", task_id)
    if route not in V01_ROUTES:
        raise ContractError("INVALID_PACKET", "route is not a documented V0.1 route", task_id)
    for field in ("edge_latency_ms", "network_latency_ms", "total_latency_ms"):
        if require_number(require_field(log, field, task_id), field, task_id) <= 0:
            raise ContractError("INVALID_PACKET", f"{field} must be greater than 0", task_id)
    cloud_latency = require_field(log, "cloud_latency_ms", task_id)
    if cloud_latency is not None and require_number(cloud_latency, "cloud_latency_ms", task_id) <= 0:
        raise ContractError("INVALID_PACKET", "cloud_latency_ms must be greater than 0", task_id)
    require_confidence(require_field(log, "edge_confidence", task_id), "edge_confidence", task_id)
    cloud_confidence = require_field(log, "cloud_confidence", task_id)
    if cloud_confidence is not None:
        require_confidence(cloud_confidence, "cloud_confidence", task_id)
    if route in {"cloud", "edge_cloud"} and (
        cloud_latency is None or cloud_confidence is None
    ):
        raise ContractError(
            "INVALID_PACKET",
            "cloud and edge_cloud routes require cloud latency and confidence",
            task_id,
        )
    if route in {"edge", "fallback_edge"} and (
        cloud_latency is not None or cloud_confidence is not None
    ):
        raise ContractError(
            "INVALID_PACKET",
            "edge and fallback_edge routes reject cloud latency and confidence",
            task_id,
        )
    final_label = log.get("final_label")
    if final_label is not None and final_label not in LABELS:
        raise ContractError("INVALID_PACKET", "final_label must be normal or fault", task_id)
    final_confidence = log.get("final_confidence")
    if final_confidence is not None:
        require_confidence(final_confidence, "final_confidence", task_id)
    for field in ("success", "has_conflict"):
        if not isinstance(require_field(log, field, task_id), bool):
            raise ContractError("INVALID_PACKET", f"{field} must be boolean", task_id)
    conflict_resolved = require_field(log, "conflict_resolved", task_id)
    if log["has_conflict"]:
        if not isinstance(conflict_resolved, bool):
            raise ContractError("INVALID_PACKET", "conflict_resolved must be boolean when has_conflict is true", task_id)
    elif conflict_resolved is not None:
        raise ContractError("INVALID_PACKET", "conflict_resolved must be null when has_conflict is false", task_id)
    return log


def validate_schedule_request_v01(payload: Any) -> dict[str, Any]:
    """Validate all four nested objects in the documented V0.1 schedule request."""

    request = require_mapping(payload, "ScheduleRequest")
    task = require_mapping(require_field(request, "task"), "task")
    task_id = require_non_empty_string(require_field(task, "task_id"), "task.task_id")
    scenario = require_field(task, "scenario", task_id)
    if scenario not in {"industrial", "energy"}:
        raise ContractError("INVALID_PACKET", "task.scenario must be industrial or energy", task_id)
    require_non_empty_string(require_field(task, "task_type", task_id), "task.task_type", task_id)
    deadline = require_int(require_field(task, "deadline_ms", task_id), "task.deadline_ms", task_id)
    if deadline <= 0:
        raise ContractError("INVALID_PACKET", "task.deadline_ms must be greater than 0", task_id)
    require_confidence(require_field(task, "priority", task_id), "task.priority", task_id)
    data_size = require_number(require_field(task, "data_size_kb", task_id), "task.data_size_kb", task_id)
    if data_size < 0:
        raise ContractError("INVALID_PACKET", "task.data_size_kb must be non-negative", task_id)
    if "source_node" in task:
        require_non_empty_string(task["source_node"], "task.source_node", task_id)

    edge_result = require_mapping(require_field(request, "edge_result", task_id), "edge_result", task_id)
    edge_task_id = edge_result.get("task_id")
    if edge_task_id is not None:
        require_non_empty_string(edge_task_id, "edge_result.task_id", task_id)
        if edge_task_id != task_id:
            raise ContractError("INVALID_PACKET", "edge_result.task_id must match task.task_id", task_id)
    if require_field(edge_result, "label", task_id) not in LABELS:
        raise ContractError("INVALID_PACKET", "edge_result.label must be normal or fault", task_id)
    require_confidence(require_field(edge_result, "confidence", task_id), "edge_result.confidence", task_id)
    latency = require_number(
        require_field(edge_result, "edge_latency_ms", task_id), "edge_result.edge_latency_ms", task_id
    )
    if latency < 0:
        raise ContractError("INVALID_PACKET", "edge_result.edge_latency_ms must be non-negative", task_id)
    if not isinstance(require_field(edge_result, "need_cloud", task_id), bool):
        raise ContractError("INVALID_PACKET", "edge_result.need_cloud must be boolean", task_id)

    network = require_mapping(require_field(request, "network_state", task_id), "network_state", task_id)
    for field in ("latency_ms", "bandwidth_mbps"):
        value = require_number(require_field(network, field, task_id), f"network_state.{field}", task_id)
        if value < 0:
            raise ContractError("INVALID_PACKET", f"network_state.{field} must be non-negative", task_id)
    require_confidence(require_field(network, "packet_loss", task_id), "network_state.packet_loss", task_id)
    if not isinstance(require_field(network, "cloud_available", task_id), bool):
        raise ContractError("INVALID_PACKET", "network_state.cloud_available must be boolean", task_id)

    node = require_mapping(require_field(request, "node_state", task_id), "node_state", task_id)
    for field in ("edge_cpu_usage", "edge_memory_usage"):
        require_confidence(require_field(node, field, task_id), f"node_state.{field}", task_id)
    queue = require_int(
        require_field(node, "cloud_queue_length", task_id), "node_state.cloud_queue_length", task_id
    )
    if queue < 0:
        raise ContractError("INVALID_PACKET", "node_state.cloud_queue_length must be non-negative", task_id)
    if not isinstance(require_field(node, "fog_available", task_id), bool):
        raise ContractError("INVALID_PACKET", "node_state.fog_available must be boolean", task_id)
    return request


def validate_edge_result(payload: dict[str, Any], packet_id: str | None = None) -> dict[str, Any]:
    result = require_mapping(payload, "EdgeResult", packet_id)
    result_packet_id = require_non_empty_string(require_field(result, "packet_id", packet_id), "packet_id", packet_id)
    if packet_id is not None and result_packet_id != packet_id:
        raise ContractError("INVALID_PACKET", "edge result packet_id does not match request", packet_id)
    require_non_empty_string(require_field(result, "device_id", result_packet_id), "device_id", result_packet_id)
    require_non_empty_string(require_field(result, "edge_node_id", result_packet_id), "edge_node_id", result_packet_id)
    require_non_empty_string(require_field(result, "model_name", result_packet_id), "model_name", result_packet_id)
    label = require_field(result, "label", result_packet_id)
    if label not in LABELS:
        raise ContractError("INVALID_PACKET", "label must be normal or fault", result_packet_id)
    require_confidence(require_field(result, "confidence", result_packet_id), "confidence", result_packet_id)
    if require_field(result, "risk_level", result_packet_id) not in RISK_LEVELS:
        raise ContractError("INVALID_PACKET", "risk_level must be low, medium, or high", result_packet_id)
    if not isinstance(require_field(result, "need_cloud", result_packet_id), bool):
        raise ContractError("INVALID_PACKET", "need_cloud must be boolean", result_packet_id)
    if require_number(require_field(result, "edge_latency_ms", result_packet_id), "edge_latency_ms", result_packet_id) < 0:
        raise ContractError("INVALID_PACKET", "edge_latency_ms must be greater than or equal to 0", result_packet_id)
    return result


def validate_schedule_decision(payload: dict[str, Any], packet_id: str | None = None) -> dict[str, Any]:
    decision = require_mapping(payload, "ScheduleDecision", packet_id)
    result_packet_id = require_non_empty_string(require_field(decision, "packet_id", packet_id), "packet_id", packet_id)
    if packet_id is not None and result_packet_id != packet_id:
        raise ContractError("INVALID_PACKET", "schedule decision packet_id does not match request", packet_id)
    route = require_field(decision, "route", result_packet_id)
    if route not in ROUTES:
        raise ContractError("INVALID_PACKET", "route must be edge, cloud, or fallback_edge", result_packet_id)
    require_non_empty_string(require_field(decision, "target_node", result_packet_id), "target_node", result_packet_id)
    if not isinstance(require_field(decision, "upload_required", result_packet_id), bool):
        raise ContractError("INVALID_PACKET", "upload_required must be boolean", result_packet_id)
    require_non_empty_string(require_field(decision, "reason", result_packet_id), "reason", result_packet_id)
    if require_number(require_field(decision, "estimated_total_latency_ms", result_packet_id), "estimated_total_latency_ms", result_packet_id) < 0:
        raise ContractError("INVALID_PACKET", "estimated_total_latency_ms must be greater than or equal to 0", result_packet_id)
    return decision


def validate_cloud_review_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the cloud perception review request without accepting legacy fields."""

    from cloud_service.perception.validator import validate_cloud_review_request as validate_request

    return validate_request(payload)


def canonical_summary_sha256(summary: dict[str, Any]) -> str:
    """Return the documented SHA-256 for one summary, excluding its envelope."""

    serialized = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Compatibility shim: bearing-specific signal contracts were moved behind
# compatibility.bearing_v12. Re-export them here so
# existing ``from common.schemas import ...`` sites and their JSON shapes /
# error codes / error messages remain unchanged.
# ---------------------------------------------------------------------------

from compatibility.bearing_v12.legacy_exports import (  # noqa: E402, F401
    DATA_TYPE,
    DURATION_MS,
    EDGE_RESULTS,
    PERCEPTION_ERROR_CODES,
    PERCEPTION_QUALITY_STATUSES,
    PROCESSING_STATUSES,
    QUALITY_FLAGS,
    SAMPLE_COUNT,
    SAMPLE_RATE_HZ,
    compact_packet_for_scheduler,
    validate_cloud_request,
    validate_edge_feature_summary,
    validate_edge_feature_summary_batch,
    validate_edge_feature_summary_envelope,
    validate_schedule_request,
    validate_sensor_packet,
    validate_task_trace,
)
