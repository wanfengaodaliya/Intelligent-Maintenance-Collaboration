from __future__ import annotations

import copy
import math
import threading
from typing import Any, Mapping


NETWORK_MEASUREMENT_STATUSES = {"OK", "STALE", "DISCONNECTED", "FAILED"}
# AUD-03/04/11: 采集状态语义与 Edge 契约保持一致；字段可选，缺失时按旧报告处理。
CPU_MEASUREMENT_STATUSES = {"OK", "DEGRADED", "FAILED"}
ACCELERATOR_MEASUREMENT_STATUSES = {"OK", "STALE", "FAILED"}
QUEUE_MEASUREMENT_STATUSES = {"OK", "STALE", "FAILED"}


class EdgeStatusValidationError(ValueError):
    pass


class EdgeStatusRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[str, dict[str, Any]] = {}

    def update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        report = _validate_report(payload)
        edge_node_id = report["edge_node_id"]
        with self._lock:
            current = self._reports.get(edge_node_id)
            if current is not None and report["reported_at_ns"] <= current["reported_at_ns"]:
                return {
                    "edge_node_id": edge_node_id,
                    "accepted": False,
                    "reported_at_ns": report["reported_at_ns"],
                    "reason_code": "STALE_STATUS_REPORT",
                }
            self._reports[edge_node_id] = copy.deepcopy(report)
        return {
            "edge_node_id": edge_node_id,
            "accepted": True,
            "reported_at_ns": report["reported_at_ns"],
        }

    def get(self, edge_node_id: str) -> dict[str, Any] | None:
        node_id = _non_empty_text(edge_node_id, "edge_node_id")
        with self._lock:
            report = self._reports.get(node_id)
            return copy.deepcopy(report) if report is not None else None


def _validate_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    report = _mapping(payload, "status report")
    resources = _mapping(report.get("resources"), "resources")
    models = report.get("models")
    if not isinstance(models, list):
        raise EdgeStatusValidationError("models must be an array")
    validated_models = []
    for model in models:
        item = _mapping(model, "model")
        validated_models.append(
            {
                "model_version": _non_empty_text(item.get("model_version"), "model_version"),
                "load_status": _non_empty_text(item.get("load_status"), "load_status").upper(),
            }
        )
    validated = {
        "edge_node_id": _non_empty_text(report.get("edge_node_id"), "edge_node_id"),
        "reported_at_ns": _positive_int(report.get("reported_at_ns"), "reported_at_ns"),
        "resources": {
            "logical_cpu_count": _positive_int(resources.get("logical_cpu_count"), "logical_cpu_count"),
            "cpu_utilization_percent": _bounded_float(
                resources.get("cpu_utilization_percent"),
                "cpu_utilization_percent",
                0.0,
                100.0,
            ),
            "memory_available_mb": _non_negative_float(
                resources.get("memory_available_mb"), "memory_available_mb"
            ),
            "gpu_available": _boolean(resources.get("gpu_available"), "gpu_available"),
            "npu_available": _boolean(resources.get("npu_available"), "npu_available"),
            "queue_length": _non_negative_int(resources.get("queue_length"), "queue_length"),
        },
        "models": validated_models,
        "last_task_activity_ns": _non_negative_int(
            report.get("last_task_activity_ns"), "last_task_activity_ns"
        ),
    }
    _attach_resource_measurement_status(resources, validated["resources"])
    if report.get("network_to_scheduler") is not None:
        network = _mapping(report.get("network_to_scheduler"), "network_to_scheduler")
        validated_network = {
            "measured_at_ns": _positive_int(
                network.get("measured_at_ns"), "measured_at_ns"
            ),
            "available_uplink_mbps_estimate": _non_negative_float(
                network.get("available_uplink_mbps_estimate"),
                "available_uplink_mbps_estimate",
            ),
            "rtt_ms_avg": _non_negative_float(
                network.get("rtt_ms_avg"), "rtt_ms_avg"
            ),
            "rtt_ms_p95": _non_negative_float(
                network.get("rtt_ms_p95"), "rtt_ms_p95"
            ),
            "loss_rate": _bounded_float(
                network.get("loss_rate"), "loss_rate", 0.0, 1.0
            ),
        }
        measurement_status = network.get("measurement_status")
        if measurement_status is not None:
            status_text = _non_empty_text(measurement_status, "measurement_status").upper()
            if status_text not in NETWORK_MEASUREMENT_STATUSES:
                raise EdgeStatusValidationError(
                    "measurement_status must be one of OK/STALE/DISCONNECTED/FAILED"
                )
            validated_network["measurement_status"] = status_text
        rtt_p95_is_estimate = network.get("rtt_p95_is_estimate")
        if rtt_p95_is_estimate is not None:
            validated_network["rtt_p95_is_estimate"] = _boolean(
                rtt_p95_is_estimate, "rtt_p95_is_estimate"
            )
        last_successful_measurement_ns = network.get("last_successful_measurement_ns")
        if last_successful_measurement_ns is not None:
            validated_network["last_successful_measurement_ns"] = _positive_int(
                last_successful_measurement_ns, "last_successful_measurement_ns"
            )
        validated["network_to_scheduler"] = validated_network
    return validated


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EdgeStatusValidationError(f"{field} must be an object")
    return value


def _optional_measurement_status(
    resources: Mapping[str, Any],
    field: str,
    allowed: set[str],
) -> str | None:
    value = resources.get(field)
    if value is None:
        return None
    status = _non_empty_text(value, field).upper()
    if status not in allowed:
        raise EdgeStatusValidationError(
            f"{field} must be one of {'/'.join(sorted(allowed))}"
        )
    return status


def _attach_resource_measurement_status(
    resources: Mapping[str, Any],
    validated_resources: dict[str, Any],
) -> None:
    """AUD-03/04/11: carry optional measurement-status fields into the stored report.

    Fields are additive and backward compatible: reports without them keep the
    legacy projection, and the Cloud must not silently reinterpret
    cpu=0 / queue=0 as "idle" when the Edge explicitly flags DEGRADED/FAILED.
    """

    cpu_status = _optional_measurement_status(
        resources, "cpu_measurement_status", CPU_MEASUREMENT_STATUSES
    )
    if cpu_status is not None:
        validated_resources["cpu_measurement_status"] = cpu_status
    # EDGE-3: 复用 CPU 的 OK/DEGRADED/FAILED whitelist；字段缺失保持旧报告兼容。
    memory_status = _optional_measurement_status(
        resources, "memory_measurement_status", CPU_MEASUREMENT_STATUSES
    )
    if memory_status is not None:
        validated_resources["memory_measurement_status"] = memory_status
    accelerator_status = _optional_measurement_status(
        resources, "accelerator_measurement_status", ACCELERATOR_MEASUREMENT_STATUSES
    )
    if accelerator_status is not None:
        validated_resources["accelerator_measurement_status"] = accelerator_status
    queue_status = _optional_measurement_status(
        resources, "queue_measurement_status", QUEUE_MEASUREMENT_STATUSES
    )
    if queue_status is not None:
        validated_resources["queue_measurement_status"] = queue_status
    breakdown = resources.get("queue_breakdown")
    if breakdown is not None:
        if not isinstance(breakdown, Mapping):
            raise EdgeStatusValidationError("queue_breakdown must be an object")
        validated_breakdown: dict[str, int] = {}
        for name, value in breakdown.items():
            validated_breakdown[_non_empty_text(name, "queue_breakdown key")] = (
                _non_negative_int(value, f"queue_breakdown.{name}")
            )
        validated_resources["queue_breakdown"] = validated_breakdown


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EdgeStatusValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EdgeStatusValidationError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EdgeStatusValidationError(f"{field} must be a non-negative integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise EdgeStatusValidationError(f"{field} must be a boolean")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EdgeStatusValidationError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise EdgeStatusValidationError(f"{field} must be finite")
    return number


def _non_negative_float(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise EdgeStatusValidationError(f"{field} cannot be negative")
    return number


def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    number = _number(value, field)
    if not minimum <= number <= maximum:
        raise EdgeStatusValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return number
