"""Validate device summaries and choose the device-level cloud route."""
# 该模块校验设备汇总结果并选择设备级云端处理路径。

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Mapping

from compatibility.bearing_v12.scheduler_mapper import (
    device_request_to_domain,
    legacy_scheduler_error_message,
    uses_generic_scheduler_fields,
)

try:
    from .cloud_registry import (
        CloudNodeRegistry,
        CloudNodeSnapshot,
        EdgeCloudLinkSnapshot,
    )
except ImportError:
    from cloud_registry import CloudNodeRegistry, CloudNodeSnapshot, EdgeCloudLinkSnapshot


LOCAL_FINAL = "LOCAL_FINAL"
CLOUD_ARBITRATION_NOW = "CLOUD_ARBITRATION_NOW"
LOCAL_PROVISIONAL_AND_DEFER_CLOUD = "LOCAL_PROVISIONAL_AND_DEFER_CLOUD"


class DeviceArbitrationRouteError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class DeviceArbitrationRoutingConfig:
    confidence_threshold: float = 0.80
    max_cloud_queue_length: int = 5
    min_uplink_mbps: float = 2.0
    max_rtt_p95_ms: float = 100.0
    max_loss_rate: float = 0.10
    required_cloud_model: str | None = None
    default_cloud_node_id: str = "cloud_01"
    cloud_endpoint: str = "/cloud/device-arbitration"
    summary_module_id: str = "summary_01"


class DeviceArbitrationRouter:
    def __init__(
        self,
        *,
        cloud_registry: CloudNodeRegistry,
        config: DeviceArbitrationRoutingConfig | None = None,
        clock_ns: Any = time.time_ns,
    ) -> None:
        self.cloud_registry = cloud_registry
        self.config = config or DeviceArbitrationRoutingConfig()
        self.clock_ns = clock_ns

    def decide(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = _validate_device_request(payload, self.config.summary_module_id)
        now_ns = self.clock_ns()
        decision_id = _decision_id(request)
        comparison = request["comparison"]
        aggregate_confidence = comparison["aggregate_confidence"]
        complexity = request["task_complexity"]
        reasons = _business_reasons(request, self.config.confidence_threshold)

        if not reasons:
            return self._response(
                request,
                decision_id=decision_id,
                route=LOCAL_FINAL,
                reasons=["NO_CONFLICT", "HIGH_AGGREGATE_CONFIDENCE", "LOW_COMPLEXITY"],
                defer_reason=None,
                cloud=None,
                link=None,
                now_ns=now_ns,
            )

        cloud = self.cloud_registry.snapshot(
            self.config.default_cloud_node_id, now_ns=now_ns
        )
        link = self.cloud_registry.link_snapshot(
            request["summary_module_id"],
            self.config.default_cloud_node_id,
            now_ns=now_ns,
        )
        condition_reasons = self._condition_reasons(cloud, link)
        if condition_reasons:
            return self._response(
                request,
                decision_id=decision_id,
                route=LOCAL_PROVISIONAL_AND_DEFER_CLOUD,
                reasons=reasons + condition_reasons,
                defer_reason=condition_reasons[0],
                cloud=cloud,
                link=link,
                now_ns=now_ns,
            )
        return self._response(
            request,
            decision_id=decision_id,
            route=CLOUD_ARBITRATION_NOW,
            reasons=reasons,
            defer_reason=None,
            cloud=cloud,
            link=link,
            now_ns=now_ns,
        )

    def cloud_delivery_eligibility(
        self,
        task: Mapping[str, Any],
        now_ns: int,
    ) -> tuple[bool, str | None]:
        summary_module_id = str(task["summary_module_id"])
        cloud_node_id = str(task["cloud_node_id"])
        cloud = self.cloud_registry.snapshot(cloud_node_id, now_ns=now_ns)
        link = self.cloud_registry.link_snapshot(
            summary_module_id,
            cloud_node_id,
            now_ns=now_ns,
        )
        reasons = self._condition_reasons(cloud, link)
        if reasons:
            return False, reasons[0]
        return True, None

    def _condition_reasons(
        self,
        cloud: CloudNodeSnapshot | None,
        link: EdgeCloudLinkSnapshot | None,
    ) -> list[str]:
        reasons: list[str] = []
        if cloud is None:
            reasons.append("CLOUD_OFFLINE")
        else:
            if not cloud.is_fresh:
                reasons.append("STATUS_STALE")
            elif cloud.health_status != "ONLINE":
                reasons.append("CLOUD_OFFLINE")
            if cloud.queue_length > self.config.max_cloud_queue_length:
                reasons.append("CLOUD_OVERLOADED")
            if not cloud.model_loaded(self.config.required_cloud_model):
                reasons.append("MODEL_NOT_READY")

        if (
            link is None
            or link.measurement_status != "AVAILABLE"
            or not link.connected
        ):
            reasons.append("NETWORK_UNAVAILABLE")
        elif (
            link.goodput_mbps is None
            or link.rtt_ms_p95 is None
            or link.loss_rate is None
            or link.goodput_mbps < self.config.min_uplink_mbps
            or link.rtt_ms_p95 > self.config.max_rtt_p95_ms
            or link.loss_rate > self.config.max_loss_rate
        ):
            reasons.append("NETWORK_POOR")
        return reasons

    def _response(
        self,
        request: Mapping[str, Any],
        *,
        decision_id: str,
        route: str,
        reasons: list[str],
        defer_reason: str | None,
        cloud: CloudNodeSnapshot | None,
        link: EdgeCloudLinkSnapshot | None,
        now_ns: int,
    ) -> dict[str, Any]:
        local_final = route == LOCAL_FINAL
        deferred = route == LOCAL_PROVISIONAL_AND_DEFER_CLOUD
        needs_cloud = not local_final
        response = {
            "decision_id": decision_id,
            "device_id": request["device_id"],
            "task_id": request["task_id"],
            "route": route,
            "needs_cloud_arbitration": needs_cloud,
            "deferred_cloud_arbitration": deferred,
            "reason_codes": reasons,
            "defer_reason": defer_reason,
            "source": {
                "holder_id": request["summary_module_id"],
                "unit_results_ref": request["source_refs"]["unit_results_ref"],
                "provisional_result_ref": request["source_refs"]["provisional_result_ref"],
            },
            "input_snapshot": {
                "conflict": request["comparison"]["conflict"],
                "aggregate_confidence": request["comparison"]["aggregate_confidence"],
                "task_complexity": request["task_complexity"],
                "network_snapshot_id": link.link_id if link is not None else None,
                "cloud_status_message_id": cloud.status_message_id if cloud is not None else None,
            },
            "local_instruction": {
                "execute_local_arbitration": route != CLOUD_ARBITRATION_NOW,
                "result_status": "FINAL" if local_final else "PROVISIONAL",
                "decision_mode": "LOCAL_ARBITRATION" if local_final else "LOCAL_FALLBACK",
                "use_conservative_action": deferred,
            },
            "target": {
                "summary_module_id": request["summary_module_id"],
                "cloud_node_id": self.config.default_cloud_node_id if needs_cloud else None,
                "endpoint": self.config.cloud_endpoint if needs_cloud else None,
            },
            "callback": {
                "edge_node_id": request["edge_node_id"] if needs_cloud else None,
                "endpoint": (
                    "/edge/device-arbitration-results"
                    if needs_cloud and request["edge_node_id"]
                    else None
                ),
            },
            "retry_required": deferred,
            "created_at_ns": now_ns,
        }
        if request["v12_identity"] is not None:
            identity = request["v12_identity"]
            response.update(
                {
                    "conflict_id": _conflict_id(request),
                    "decision_round_id": identity["decision_round_id"],
                    "device_result_revision": identity["device_result_revision"],
                    "unit_result_ids": list(identity["unit_result_ids"]),
                    "unit_results": request["unit_results"],
                    "comparison": request["comparison"],
                    "local_arbitration_supported": request[
                        "local_arbitration_supported"
                    ],
                }
            )
        return response


def cloud_device_task_id(decision_id: str) -> str:
    suffix = decision_id.removeprefix("decision_device_")
    return f"cloud_device_task_{suffix}"


def _validate_device_request(
    payload: Mapping[str, Any],
    default_summary_module_id: str,
) -> dict[str, Any]:
    legacy_vocabulary = not uses_generic_scheduler_fields(payload)
    try:
        item = device_request_to_domain(
            _mapping(payload, "device arbitration request")
        )
        comparison = _mapping(item.get("comparison"), "comparison")
        aggregate_confidence = _bounded_float(
            comparison.get("aggregate_confidence"),
            "aggregate_confidence",
            0.0,
            1.0,
        )
        complexity = _bounded_float(item.get("task_complexity"), "task_complexity", 0.0, 1.0)
        if abs(complexity - (1.0 - aggregate_confidence)) > 1e-6:
            raise DeviceArbitrationRouteError(
                "TASK_COMPLEXITY_MISMATCH",
                "task_complexity must equal 1 - aggregate_confidence",
            )
        expected = _positive_int(item.get("expected_unit_count"), "expected_unit_count")
        received = _non_negative_int(item.get("received_unit_count"), "received_unit_count")
        results = item.get("unit_results")
        if not isinstance(results, list):
            raise ValueError("unit_results must be an array")
        units = [_validate_unit_result(value) for value in results]
        unit_ids = [value["unit_id"] for value in units]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("unit_id values must be unique")
        v12_identity = _validate_v12_identity(item, units)
        source_refs = item.get("source_refs")
        if source_refs is None:
            task_id = _text(item.get("task_id"), "task_id")
            source_refs = {
                "unit_results_ref": f"summary-store://{task_id}/units",
                "provisional_result_ref": f"summary-store://{task_id}/device-result-v1",
            }
        else:
            source_refs = _mapping(source_refs, "source_refs")
        return {
            "device_id": _text(item.get("device_id"), "device_id"),
            "task_id": _text(item.get("task_id"), "task_id"),
            "summary_module_id": _text(
                item.get("summary_module_id", default_summary_module_id),
                "summary_module_id",
            ),
            "edge_node_id": _optional_text(item.get("edge_node_id"), "edge_node_id"),
            "expected_unit_count": expected,
            "received_unit_count": received,
            "unit_results": units,
            "comparison": {
                "conflict": _bool(comparison.get("conflict"), "conflict"),
                "conflict_type": (
                    None
                    if comparison.get("conflict_type") is None
                    else _text(comparison.get("conflict_type"), "conflict_type")
                ),
                "action_level_min": _non_negative_int(
                    comparison.get("action_level_min"),
                    "action_level_min",
                ),
                "action_level_max": _non_negative_int(
                    comparison.get("action_level_max"),
                    "action_level_max",
                ),
                "action_level_span": _non_negative_int(
                    comparison.get("action_level_span"),
                    "action_level_span",
                ),
                "aggregate_confidence": aggregate_confidence,
                "low_confidence_unit_count": _non_negative_int(
                    comparison.get("low_confidence_unit_count"),
                    "low_confidence_unit_count",
                ),
                "provisional_unit_count": _non_negative_int(
                    comparison.get("provisional_unit_count"),
                    "provisional_unit_count",
                ),
                "data_complete": _bool(comparison.get("data_complete"), "data_complete"),
            },
            "task_complexity": complexity,
            "local_arbitration_supported": _bool(
                item.get("local_arbitration_supported"),
                "local_arbitration_supported",
            ),
            "source_refs": {
                "unit_results_ref": _text(
                    source_refs.get("unit_results_ref"),
                    "unit_results_ref",
                ),
                "provisional_result_ref": _text(
                    source_refs.get("provisional_result_ref"),
                    "provisional_result_ref",
                ),
            },
            "v12_identity": v12_identity,
        }
    except DeviceArbitrationRouteError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        message = str(error)
        if legacy_vocabulary:
            message = legacy_scheduler_error_message(message)
        raise DeviceArbitrationRouteError(
            "INVALID_DEVICE_ARBITRATION_REQUEST",
            message,
        ) from error


def _validate_unit_result(value: Any) -> dict[str, Any]:
    item = _mapping(value, "unit result")
    return {
        "unit_id": _text(item.get("unit_id"), "unit_id"),
        "unit_result_id": _text(item.get("unit_result_id"), "unit_result_id"),
        "result": _text(item.get("result"), "result"),
        "confidence": _bounded_float(item.get("confidence"), "confidence", 0.0, 1.0),
        "risk_level": _text(item.get("risk_level"), "risk_level"),
        "action_level": _non_negative_int(item.get("action_level"), "action_level"),
        "result_status": _enum(
            item.get("result_status"),
            "result_status",
            {"FINAL", "PROVISIONAL", "FAILED"},
        ),
    }


def _business_reasons(
    request: Mapping[str, Any],
    confidence_threshold: float,
) -> list[str]:
    comparison = request["comparison"]
    reasons: list[str] = []
    if comparison["conflict"]:
        reasons.append("RESULT_CONFLICT")
    if comparison["aggregate_confidence"] < confidence_threshold:
        reasons.append("LOW_AGGREGATE_CONFIDENCE")
    if request["task_complexity"] > 1.0 - confidence_threshold:
        reasons.append("HIGH_COMPLEXITY")
    if (
        not comparison["data_complete"]
        or request["received_unit_count"] != request["expected_unit_count"]
    ):
        reasons.append("INCOMPLETE_UNIT_RESULTS")
    if not request["local_arbitration_supported"]:
        reasons.append("LOCAL_ARBITRATION_UNSUPPORTED")
    if comparison["provisional_unit_count"] > 0:
        reasons.append("HAS_PROVISIONAL_UNIT_RESULT")
    return reasons


def _decision_id(request: Mapping[str, Any]) -> str:
    identity = {
        "device_id": request["device_id"],
        "task_id": request["task_id"],
        "summary_module_id": request["summary_module_id"],
    }
    if request["v12_identity"] is not None:
        identity["decision_round_id"] = request["v12_identity"]["decision_round_id"]
        identity["device_result_revision"] = request["v12_identity"]["device_result_revision"]
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"decision_device_{digest}"


def _conflict_id(request: Mapping[str, Any]) -> str:
    identity = request["v12_identity"]
    if identity is None:
        raise ValueError("V1.2 identity is required for conflict_id")
    canonical = {
        "device_id": request["device_id"],
        "task_id": request["task_id"],
        "decision_round_id": identity["decision_round_id"],
        "device_result_revision": identity["device_result_revision"],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"conflict_device_{digest}"


def _validate_v12_identity(
    item: Mapping[str, Any],
    units: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    fields = ("decision_round_id", "device_result_revision", "unit_result_ids")
    present = [field in item for field in fields]
    if not any(present):
        return None
    if not all(present):
        raise ValueError("V1.2 device arbitration identity fields must be provided together")
    result_ids = _string_list(item.get("unit_result_ids"), "unit_result_ids")
    if len(set(result_ids)) != len(result_ids):
        raise ValueError("unit_result_ids must be unique")
    if result_ids != [unit["unit_result_id"] for unit in units]:
        raise ValueError("unit_result_ids must match unit_results in order")
    return {
        "decision_round_id": _text(item.get("decision_round_id"), "decision_round_id"),
        "device_result_revision": _positive_int(
            item.get("device_result_revision"), "device_result_revision"
        ),
        "unit_result_ids": result_ids,
    }


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    normalized = _text(value, field).upper()
    if normalized not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return normalized


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    return [_text(item, field) for item in value]


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result
