"""Cloud-node telemetry and edge-to-cloud link snapshots used by packet routing."""
# 该模块管理数据包路由所需的云节点遥测和边云链路快照。

from __future__ import annotations

import copy
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

try:
    from .node_registry import MAX_FUTURE_CLOCK_SKEW_NS, RegistryError
except ImportError:
    from node_registry import MAX_FUTURE_CLOCK_SKEW_NS, RegistryError


CLOUD_STATUS_TTL_NS = 5_000_000_000
CLOUD_LINK_TTL_NS = 5_000_000_000
_HEALTH_STATUSES = {"ONLINE", "OFFLINE", "DEGRADED"}
_MODEL_STATUSES = {"LOADED", "LOADING", "UNLOADED", "FAILED"}
_MEASUREMENT_STATUSES = {"AVAILABLE", "UNAVAILABLE"}


@dataclass(frozen=True)
class CloudNodeSnapshot:
    cloud_node_id: str
    status_message_id: str
    report: dict[str, Any]
    received_at_ns: int
    is_fresh: bool

    @property
    def health_status(self) -> str:
        return str(self.report["health_status"])

    @property
    def queue_length(self) -> int:
        return int(self.report["resources"]["queue_length"])

    def model_loaded(self, model_version: str | None = None) -> bool:
        models = self.report["models"]
        if model_version is None:
            return any(item["model_load_status"] == "LOADED" for item in models)
        return any(
            item["model_version"] == model_version
            and item["model_load_status"] == "LOADED"
            for item in models
        )


@dataclass(frozen=True)
class EdgeCloudLinkSnapshot:
    sent_at_ns: int
    link_id: str
    source_id: str
    target_id: str
    measurement_status: str
    connected: bool
    measured_at_ns: int
    goodput_mbps: float | None
    rtt_ms_p50: float | None
    rtt_ms_p95: float | None
    jitter_ms: float | None
    loss_rate: float | None
    expires_at_ns: int
    received_at_ns: int

    def is_fresh(self, now_ns: int) -> bool:
        return now_ns <= self.expires_at_ns


class CloudNodeRegistry:
    def __init__(self, *, status_ttl_ns: int = CLOUD_STATUS_TTL_NS, link_ttl_ns: int = CLOUD_LINK_TTL_NS) -> None:
        self.status_ttl_ns = status_ttl_ns
        self.link_ttl_ns = link_ttl_ns
        self._nodes: dict[str, tuple[dict[str, Any], int]] = {}
        self._status_messages: dict[str, str] = {}
        self._links: dict[tuple[str, str], EdgeCloudLinkSnapshot] = {}
        self._link_payloads: dict[tuple[str, int], str] = {}
        self._lock = threading.RLock()

    def update_status(self, payload: Mapping[str, Any], *, received_at_ns: int | None = None) -> dict[str, Any]:
        report = _validate_cloud_status(payload)
        try:
            now = time.time_ns() if received_at_ns is None else _positive_int(received_at_ns, "received_at_ns")
        except ValueError as error:
            raise RegistryError("INVALID_CLOUD_STATUS", str(error)) from error
        if report["reported_at_ns"] > now + MAX_FUTURE_CLOCK_SKEW_NS:
            raise RegistryError("INVALID_CLOUD_STATUS", "reported_at_ns is too far in the future")
        message_id = report["status_message_id"]
        canonical = _canonical(report)
        with self._lock:
            previous_payload = self._status_messages.get(message_id)
            if previous_payload is not None:
                if previous_payload != canonical:
                    raise RegistryError("CLOUD_STATUS_CONFLICT", "status_message_id already refers to another payload")
                return {"status_message_id": message_id, "cloud_node_id": report["cloud_node_id"], "accepted": True, "duplicate": True, "received_at_ns": now}
            previous = self._nodes.get(report["cloud_node_id"])
            if previous and report["reported_at_ns"] <= previous[0]["reported_at_ns"]:
                return {"status_message_id": message_id, "cloud_node_id": report["cloud_node_id"], "accepted": False, "duplicate": False, "reason_code": "STALE_STATUS_REPORT", "received_at_ns": now}
            self._nodes[report["cloud_node_id"]] = (report, now)
            self._status_messages[message_id] = canonical
        return {"status_message_id": message_id, "cloud_node_id": report["cloud_node_id"], "accepted": True, "duplicate": False, "received_at_ns": now}

    def snapshot(self, cloud_node_id: str, *, now_ns: int | None = None) -> CloudNodeSnapshot | None:
        now = time.time_ns() if now_ns is None else now_ns
        with self._lock:
            value = self._nodes.get(cloud_node_id)
            if value is None:
                return None
            report, received_at_ns = value
            copied = copy.deepcopy(report)
        return CloudNodeSnapshot(cloud_node_id, copied["status_message_id"], copied, received_at_ns, now - copied["reported_at_ns"] <= self.status_ttl_ns)

    def snapshots(self, *, now_ns: int | None = None) -> list[CloudNodeSnapshot]:
        with self._lock:
            node_ids = sorted(self._nodes)
        return [snapshot for node_id in node_ids if (snapshot := self.snapshot(node_id, now_ns=now_ns)) is not None]

    def update_link(self, payload: Mapping[str, Any], *, received_at_ns: int | None = None, received_monotonic_ns: int | None = None) -> dict[str, Any]:
        del received_monotonic_ns
        try:
            now = time.time_ns() if received_at_ns is None else _positive_int(received_at_ns, "received_at_ns")
        except ValueError as error:
            raise RegistryError("INVALID_CLOUD_LINK", str(error)) from error
        snapshot = _validate_cloud_link(payload, now, self.link_ttl_ns)
        if snapshot.measured_at_ns > now + MAX_FUTURE_CLOCK_SKEW_NS:
            raise RegistryError("INVALID_CLOUD_LINK", "measured_at_ns is too far in the future")
        canonical = _canonical(dict(payload))
        message_key = (snapshot.link_id, snapshot.measured_at_ns)
        link_key = (snapshot.source_id, snapshot.target_id)
        with self._lock:
            previous_payload = self._link_payloads.get(message_key)
            if previous_payload is not None:
                if previous_payload != canonical:
                    raise RegistryError("CLOUD_LINK_CONFLICT", "link_id and measured_at_ns already refer to another payload")
                duplicate = True
            else:
                previous = self._links.get(link_key)
                if previous and snapshot.measured_at_ns <= previous.measured_at_ns:
                    return {"link_id": snapshot.link_id, "source_id": snapshot.source_id, "target_id": snapshot.target_id, "accepted": False, "duplicate": False, "reason_code": "STALE_LINK_SNAPSHOT"}
                self._links[link_key] = snapshot
                self._link_payloads[message_key] = canonical
                duplicate = False
        return {"link_id": snapshot.link_id, "source_id": snapshot.source_id, "target_id": snapshot.target_id, "accepted": True, "duplicate": duplicate}

    def link_snapshot(self, source_id: str, target_id: str, *, now_ns: int | None = None) -> EdgeCloudLinkSnapshot | None:
        now = time.time_ns() if now_ns is None else now_ns
        with self._lock:
            snapshot = self._links.get((source_id, target_id))
            copied = copy.deepcopy(snapshot) if snapshot is not None else None
        return copied if copied is not None and copied.is_fresh(now) else None


def _validate_cloud_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        item = _mapping(payload, "cloud status")
        resources = _mapping(item.get("resources"), "resources")
        models = item.get("models")
        if not isinstance(models, list) or not models:
            raise ValueError("models must be a non-empty array")
        validated_models = []
        for model in models:
            model_item = _mapping(model, "model")
            validated_models.append({"model_version": _text(model_item.get("model_version"), "model_version"), "model_load_status": _enum(model_item.get("model_load_status"), "model_load_status", _MODEL_STATUSES)})
        network = _mapping(item.get("network_to_scheduler"), "network_to_scheduler")
        return {
            "status_message_id": _text(item.get("status_message_id"), "status_message_id"),
            "cloud_node_id": _text(item.get("cloud_node_id"), "cloud_node_id"),
            "reported_at_ns": _positive_int(item.get("reported_at_ns"), "reported_at_ns"),
            "health_status": _enum(item.get("health_status"), "health_status", _HEALTH_STATUSES),
            "resources": {
                "logical_cpu_count": _positive_int(resources.get("logical_cpu_count"), "logical_cpu_count"),
                "cpu_utilization_percent": _bounded(resources.get("cpu_utilization_percent"), "cpu_utilization_percent", 0, 100),
                "memory_available_mb": _non_negative(resources.get("memory_available_mb"), "memory_available_mb"),
                "gpu_available": _bool(resources.get("gpu_available"), "gpu_available"),
                "npu_available": _bool(resources.get("npu_available"), "npu_available"),
                "queue_length": _non_negative_int(resources.get("queue_length"), "queue_length"),
            },
            "models": validated_models,
            "network_to_scheduler": {
                "measured_at_ns": _positive_int(network.get("measured_at_ns"), "measured_at_ns"),
                "available_uplink_mbps_estimate": _non_negative(network.get("available_uplink_mbps_estimate"), "available_uplink_mbps_estimate"),
                "rtt_ms_avg": _non_negative(network.get("rtt_ms_avg"), "rtt_ms_avg"),
                "rtt_ms_p95": _non_negative(network.get("rtt_ms_p95"), "rtt_ms_p95"),
                "loss_rate": _bounded(network.get("loss_rate"), "loss_rate", 0, 1),
            },
            "last_task_activity_ns": _non_negative_int(item.get("last_task_activity_ns"), "last_task_activity_ns"),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise RegistryError("INVALID_CLOUD_STATUS", str(error)) from error


def _validate_cloud_link(payload: Mapping[str, Any], received_at_ns: int, default_ttl_ns: int) -> EdgeCloudLinkSnapshot:
    try:
        item = _mapping(payload, "cloud link")
        status = _enum(item.get("measurement_status"), "measurement_status", _MEASUREMENT_STATUSES)
        measured_at_ns = _positive_int(item.get("measured_at_ns"), "measured_at_ns")
        expires_at = item.get("expires_at_ns")
        expires_at_ns = measured_at_ns + default_ttl_ns if expires_at is None else _positive_int(expires_at, "expires_at_ns")
        if expires_at_ns < measured_at_ns:
            raise ValueError("expires_at_ns cannot precede measured_at_ns")
        metric_fields = ("goodput_mbps", "rtt_ms_p50", "rtt_ms_p95", "jitter_ms", "loss_rate")
        if status == "UNAVAILABLE":
            if any(item.get(field) is not None for field in metric_fields):
                raise ValueError("UNAVAILABLE measurements must be null")
            metrics = {field: None for field in metric_fields}
        else:
            metrics = {"goodput_mbps": _non_negative(item.get("goodput_mbps"), "goodput_mbps"), "rtt_ms_p50": _non_negative(item.get("rtt_ms_p50"), "rtt_ms_p50"), "rtt_ms_p95": _non_negative(item.get("rtt_ms_p95"), "rtt_ms_p95"), "jitter_ms": _non_negative(item.get("jitter_ms"), "jitter_ms"), "loss_rate": _bounded(item.get("loss_rate"), "loss_rate", 0, 1)}
        return EdgeCloudLinkSnapshot(
            sent_at_ns=_positive_int(item.get("sent_at_ns"), "sent_at_ns"), link_id=_text(item.get("link_id"), "link_id"), source_id=_text(item.get("source_id"), "source_id"), target_id=_text(item.get("target_id"), "target_id"), measurement_status=status, connected=_bool(item.get("connected"), "connected"), measured_at_ns=measured_at_ns, goodput_mbps=metrics["goodput_mbps"], rtt_ms_p50=metrics["rtt_ms_p50"], rtt_ms_p95=metrics["rtt_ms_p95"], jitter_ms=metrics["jitter_ms"], loss_rate=metrics["loss_rate"], expires_at_ns=expires_at_ns, received_at_ns=received_at_ns,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RegistryError("INVALID_CLOUD_LINK", str(error)) from error


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


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _non_negative(value: Any, field: str) -> float:
    result = _number(value, field)
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result


def _bounded(value: Any, field: str, minimum: float, maximum: float) -> float:
    result = _number(value, field)
    if not minimum <= result <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
