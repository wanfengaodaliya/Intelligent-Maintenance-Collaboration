"""Validate edge packet results and choose the package-level cloud route."""

# 该模块校验边缘数据包结果并选择包级云端处理路径。
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core.diagnosis_contracts import PacketRoute
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from compatibility.bearing_v12.scheduler_mapper import (
    assignment_row_to_domain,
    packet_result_to_domain,
)

try:
    from .cloud_registry import CloudNodeRegistry, CloudNodeSnapshot, EdgeCloudLinkSnapshot
except ImportError:
    from cloud_registry import CloudNodeRegistry, CloudNodeSnapshot, EdgeCloudLinkSnapshot

try:
    from .p1_policy_adapter import maybe_choose_v01_route as _p1_choose_route
except ImportError:  # pragma: no cover - experimental policy unavailable
    _p1_choose_route = None


DIRECT_FINAL_TO_SUMMARY = "DIRECT_FINAL_TO_SUMMARY"
CLOUD_REVIEW_NOW = "CLOUD_REVIEW_NOW"
EDGE_PROVISIONAL_AND_DEFER_CLOUD = "EDGE_PROVISIONAL_AND_DEFER_CLOUD"

# P1 route action -> packet-level route in the V1.2 single-packet flow.
_P1_ROUTE_TO_PACKET_ROUTE = {
    "edge": DIRECT_FINAL_TO_SUMMARY,
    "cloud": CLOUD_REVIEW_NOW,
    "fallback_edge": EDGE_PROVISIONAL_AND_DEFER_CLOUD,
}


class PacketRouteError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PacketRoutingConfig:
    confidence_threshold: float = 0.80
    max_cloud_queue_length: int = 5
    min_uplink_mbps: float = 2.0
    max_rtt_p95_ms: float = 100.0
    max_loss_rate: float = 0.10
    required_cloud_model: str | None = None
    default_cloud_node_id: str = "cloud_01"
    cloud_endpoint: str = "/cloud/infer"
    summary_module_id: str = "summary_01"
    summary_topic: str = "summary/packet-results"


class PacketRouter:
    def __init__(
        self,
        *,
        assignment_lookup: Callable[[str], Mapping[str, Any] | None],
        cloud_registry: CloudNodeRegistry,
        config: PacketRoutingConfig | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.assignment_lookup = assignment_lookup
        self.cloud_registry = cloud_registry
        self.config = config or PacketRoutingConfig()
        self.clock_ns = clock_ns

    def decide(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _validate_packet_result(payload)
        assignment = self.assignment_lookup(result["task_id"])
        _validate_assignment_identity(result, assignment)
        now_ns = self.clock_ns()
        decision_id = _decision_id(result)

        output = result.get("output")
        confidence = output["confidence"] if output is not None else None
        complexity = output["task_complexity"] if output is not None else None
        p1_response = self._try_p1_route(
            result,
            output,
            confidence,
            complexity,
            decision_id=decision_id,
            now_ns=now_ns,
        )
        if p1_response is not None:
            return p1_response
        if output is not None and confidence >= self.config.confidence_threshold:
            return self._response(
                result,
                decision_id=decision_id,
                route=DIRECT_FINAL_TO_SUMMARY,
                reasons=["HIGH_CONFIDENCE", "LOW_COMPLEXITY"],
                defer_reason=None,
                confidence=confidence,
                complexity=complexity,
                cloud=None,
                link=None,
                now_ns=now_ns,
            )

        trigger_reasons = (
            ["LOW_CONFIDENCE", "HIGH_COMPLEXITY"]
            if output is not None
            else ["EDGE_TIMEOUT" if result["status"] == "TIMEOUT" else "EDGE_FAILED"]
        )
        cloud = self.cloud_registry.snapshot(
            self.config.default_cloud_node_id, now_ns=now_ns
        )
        link = self.cloud_registry.link_snapshot(
            result["edge_node_id"], self.config.default_cloud_node_id, now_ns=now_ns
        )
        condition_reasons = self._condition_reasons(cloud, link)
        if condition_reasons:
            return self._response(
                result,
                decision_id=decision_id,
                route=EDGE_PROVISIONAL_AND_DEFER_CLOUD,
                reasons=trigger_reasons + condition_reasons,
                defer_reason=condition_reasons[0],
                confidence=confidence,
                complexity=complexity,
                cloud=cloud,
                link=link,
                now_ns=now_ns,
            )
        return self._response(
            result,
            decision_id=decision_id,
            route=CLOUD_REVIEW_NOW,
            reasons=trigger_reasons,
            defer_reason=None,
            confidence=confidence,
            complexity=complexity,
            cloud=cloud,
            link=link,
            now_ns=now_ns,
        )

    def cloud_delivery_eligibility(
        self,
        task: Mapping[str, Any],
        now_ns: int,
    ) -> tuple[bool, str | None]:
        edge_node_id = str(task["edge_node_id"])
        cloud_node_id = str(task["cloud_node_id"])
        cloud = self.cloud_registry.snapshot(cloud_node_id, now_ns=now_ns)
        link = self.cloud_registry.link_snapshot(
            edge_node_id,
            cloud_node_id,
            now_ns=now_ns,
        )
        reasons = self._condition_reasons(cloud, link)
        if reasons:
            return False, reasons[0]
        return True, None


    def _try_p1_route(
        self,
        result: Mapping[str, Any],
        output: Mapping[str, Any] | None,
        confidence: float | None,
        complexity: float | None,
        *,
        decision_id: str,
        now_ns: int,
    ) -> dict[str, Any] | None:
        """Prefer the P1 packet-routing policy in the V1.2 single-packet flow.

        Returns None so the caller keeps the fixed rule whenever P1 is
        unavailable, the packet carries no edge output, or P1 falls back to R0.
        """
        if output is None or _p1_choose_route is None:
            return None
        cloud = self.cloud_registry.snapshot(
            self.config.default_cloud_node_id, now_ns=now_ns
        )
        link = self.cloud_registry.link_snapshot(
            result["edge_node_id"], self.config.default_cloud_node_id, now_ns=now_ns
        )
        task = {
            "task_id": result["task_id"],
            "source_node": result["device_id"],
            "unit_id": result["unit_id"],
        }
        edge_result = {"confidence": confidence}
        network_state = self._p1_network_state(cloud, link)
        node_state = {
            "cloud_queue_length": cloud.queue_length if cloud is not None else 0
        }
        choice = _p1_choose_route(
            task=task,
            edge_result=edge_result,
            network_state=network_state,
            node_state=node_state,
        )
        if choice is None:
            return None
        route = _P1_ROUTE_TO_PACKET_ROUTE.get(choice.route)
        if route is None:
            return None
        reasons = list(choice.reason_codes)
        defer_reason = (
            reasons[0] if route == EDGE_PROVISIONAL_AND_DEFER_CLOUD else None
        )
        return self._response(
            result,
            decision_id=decision_id,
            route=route,
            reasons=reasons,
            defer_reason=defer_reason,
            confidence=confidence,
            complexity=complexity,
            cloud=cloud,
            link=link,
            now_ns=now_ns,
        )

    def _p1_network_state(
        self,
        cloud: CloudNodeSnapshot | None,
        link: EdgeCloudLinkSnapshot | None,
    ) -> dict[str, Any]:
        link_usable = (
            link is not None
            and link.measurement_status == "AVAILABLE"
            and link.connected
        )
        return {
            "cloud_available": bool(
                cloud is not None
                and cloud.is_fresh
                and cloud.health_status == "ONLINE"
            ),
            "bandwidth_mbps": link.goodput_mbps if link_usable else 0.0,
            "latency_ms": link.rtt_ms_p95 if link_usable else None,
            "packet_loss": link.loss_rate if link_usable else None,
        }

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
        result: Mapping[str, Any],
        *,
        decision_id: str,
        route: str,
        reasons: list[str],
        defer_reason: str | None,
        confidence: float | None,
        complexity: float | None,
        cloud: CloudNodeSnapshot | None,
        link: EdgeCloudLinkSnapshot | None,
        now_ns: int,
    ) -> dict[str, Any]:
        input_ref = result["input_ref"]
        direct = route == DIRECT_FINAL_TO_SUMMARY
        deferred = route == EDGE_PROVISIONAL_AND_DEFER_CLOUD
        needs_cloud = not direct
        response = {
            "decision_id": decision_id,
            "device_id": result["device_id"],
            "task_id": result["task_id"],
            "unit_id": result["unit_id"],
            "packet_id": input_ref["packet_id"],
            "sequence_number": input_ref["sequence_number"],
            "route": _shared_route(route).value,
            "legacy_route": route,
            "needs_cloud_review": needs_cloud,
            "deferred_cloud_review": deferred,
            "result_instruction": {
                "result_status": (
                    "FINAL" if direct else "PROVISIONAL" if deferred else "WAITING_CLOUD"
                ),
                "decision_source": "EDGE",
                "review_status": "NOT_REQUIRED" if direct else "PENDING_CLOUD",
                "degraded": deferred,
            },
            "reason_codes": reasons,
            "defer_reason": defer_reason,
            "input_snapshot": {
                "confidence": confidence,
                "task_complexity": complexity,
                "network_snapshot_id": link.link_id if link is not None else None,
                "cloud_status_message_id": cloud.status_message_id if cloud is not None else None,
            },
            "target": {
                "summary_module_id": self.config.summary_module_id if direct or deferred else None,
                "target_topic": self.config.summary_topic if direct or deferred else None,
                "cloud_node_id": self.config.default_cloud_node_id if needs_cloud else None,
                "endpoint": self.config.cloud_endpoint if needs_cloud else None,
            },
            "created_at_ns": now_ns,
        }
        for field in (
            "decision_round_id",
            "diagnosis_window_id",
            "window_start_sequence",
            "window_end_sequence",
        ):
            if field in result:
                response[field] = result[field]
        return response


def cloud_task_id(decision_id: str) -> str:
    suffix = decision_id.removeprefix("decision_packet_")
    return f"cloud_packet_task_{suffix}"


def _validate_packet_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        item = packet_result_to_domain(_mapping(payload, "packet result"))
        required_top = {
            "device_id", "task_id", "unit_id", "edge_node_id", "error",
            "input_ref", "status", "started_at_ns", "finished_at_ns",
        }
        missing = required_top - set(item)
        if missing:
            raise ValueError("missing fields: " + ", ".join(sorted(missing)))
        input_ref = _mapping(item["input_ref"], "input_ref")
        if set(input_ref) != {"device_id", "unit_id", "sender_id", "packet_id", "sequence_number"}:
            raise ValueError("input_ref fields do not match the contract")
        result: dict[str, Any] = {
            "device_id": _text(item["device_id"], "device_id"),
            "task_id": _text(item["task_id"], "task_id"),
            "unit_id": _text(item["unit_id"], "unit_id"),
            "edge_node_id": _text(item["edge_node_id"], "edge_node_id"),
            "input_ref": {
                "device_id": _text(input_ref["device_id"], "input_ref.device_id"),
                "unit_id": _text(input_ref["unit_id"], "input_ref.unit_id"),
                "sender_id": _text(input_ref["sender_id"], "input_ref.sender_id"),
                "packet_id": _text(input_ref["packet_id"], "input_ref.packet_id"),
                "sequence_number": _bounded_int(input_ref["sequence_number"], "sequence_number", 1, 80),
            },
            "status": _enum(item["status"], "status", {"SUCCEEDED", "FAILED", "TIMEOUT"}),
            "started_at_ns": _positive_int(item["started_at_ns"], "started_at_ns"),
            "finished_at_ns": _positive_int(item["finished_at_ns"], "finished_at_ns"),
        }
        if result["finished_at_ns"] < result["started_at_ns"]:
            raise ValueError("finished_at_ns cannot precede started_at_ns")
        if result["device_id"] != result["input_ref"]["device_id"] or result["unit_id"] != result["input_ref"]["unit_id"]:
            raise ValueError("top-level and input_ref identity must match")
        _copy_v12_identity(item, result)

        if result["status"] == "SUCCEEDED":
            if item["error"] is not None:
                raise ValueError("successful result requires error=null")
            output = _mapping(item.get("output"), "output")
            required_output = {"edge_result", "confidence", "task_complexity", "edge_risk_level", "model_version"}
            if not required_output <= set(output):
                raise ValueError("output fields do not match the contract")
            confidence = _bounded_float(output["confidence"], "confidence", 0.0, 1.0)
            complexity = _bounded_float(output["task_complexity"], "task_complexity", 0.0, 1.0)
            if abs(complexity - (1.0 - confidence)) > 1e-6:
                raise PacketRouteError("TASK_COMPLEXITY_MISMATCH", "task_complexity must equal 1 - confidence")
            result["error"] = None
            result["output"] = {
                "edge_result": _text(output["edge_result"], "edge_result"),
                "confidence": confidence,
                "task_complexity": complexity,
                "edge_risk_level": _text(output["edge_risk_level"], "edge_risk_level"),
                "model_version": _text(output["model_version"], "model_version"),
            }
        else:
            result["error"] = _text(item["error"], "error")
            if item.get("output") is not None:
                raise ValueError("failed or timed-out result must omit output")
            result["output"] = None
        return result
    except PacketRouteError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise PacketRouteError("INVALID_PACKET_RESULT", str(error)) from error


def _validate_assignment_identity(result: Mapping[str, Any], assignment: Mapping[str, Any] | None) -> None:
    if assignment is None:
        raise PacketRouteError("PACKET_ASSIGNMENT_CONFLICT", "task_id is not assigned", 409)
    domain_assignment = assignment_row_to_domain(assignment)
    expected = {
        "task_id": result["task_id"],
        "device_id": result["device_id"],
        "sender_id": result["input_ref"]["sender_id"],
        "unit_id": result["unit_id"],
        "edge_node_id": result["edge_node_id"],
        "assignment_status": "ASSIGNED",
    }
    for field, value in expected.items():
        if domain_assignment.get(field) != value:
            raise PacketRouteError(
                "PACKET_ASSIGNMENT_CONFLICT",
                f"packet result does not match assigned {field}",
                409,
            )


def _decision_id(result: Mapping[str, Any]) -> str:
    identity = {
        "device_id": result["device_id"],
        "task_id": result["task_id"],
        "bearing_id": result["unit_id"],
        "packet_id": result["input_ref"]["packet_id"],
        "sequence_number": result["input_ref"]["sequence_number"],
    }
    if "decision_round_id" in result:
        identity.update(
            {
                "decision_round_id": result["decision_round_id"],
                "diagnosis_window_id": result["diagnosis_window_id"],
                "window_start_sequence": result["window_start_sequence"],
                "window_end_sequence": result["window_end_sequence"],
            }
        )
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"decision_packet_{digest}"


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    normalized = _text(value, field).upper()
    if normalized not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def _copy_v12_identity(item: Mapping[str, Any], result: dict[str, Any]) -> None:
    fields = {
        "decision_round_id",
        "diagnosis_window_id",
        "window_start_sequence",
        "window_end_sequence",
    }
    supplied = fields & set(item)
    if supplied and supplied != fields:
        raise ValueError("V1.2 route identity must include round, window, and sequence range")
    if not supplied:
        return
    start = _bounded_int(item["window_start_sequence"], "window_start_sequence", 1, 80)
    end = _bounded_int(item["window_end_sequence"], "window_end_sequence", start, 80)
    sender_id = result["input_ref"]["sender_id"]
    expected_round = build_decision_round_id(
        device_id=result["device_id"],
        task_id=result["task_id"],
        window_start_sequence=start,
        window_end_sequence=end,
    )
    expected_window = build_diagnosis_window_id(
        device_id=result["device_id"],
        task_id=result["task_id"],
        bearing_id=result["unit_id"],
        sender_id=sender_id,
        window_start_sequence=start,
        window_end_sequence=end,
    )
    if item["decision_round_id"] != expected_round or item["diagnosis_window_id"] != expected_window:
        raise ValueError("V1.2 route identity does not match deterministic identity")
    if not start <= result["input_ref"]["sequence_number"] <= end:
        raise ValueError("packet sequence_number must belong to the diagnosis window")
    result.update(
        {
            "decision_round_id": expected_round,
            "diagnosis_window_id": expected_window,
            "window_start_sequence": start,
            "window_end_sequence": end,
        }
    )


def _shared_route(legacy_route: str) -> PacketRoute:
    return {
        DIRECT_FINAL_TO_SUMMARY: PacketRoute.EDGE,
        CLOUD_REVIEW_NOW: PacketRoute.CLOUD_NOW,
        EDGE_PROVISIONAL_AND_DEFER_CLOUD: PacketRoute.DEFER,
    }[legacy_route]
