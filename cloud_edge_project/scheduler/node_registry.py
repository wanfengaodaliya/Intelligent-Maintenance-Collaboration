"""In-memory edge-node and sender-to-edge link state."""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping


STATUS_REPORT_TIMEOUT_NS = 6_000_000_000
LINK_SNAPSHOT_TIMEOUT_NS = 30_000_000_000
MAX_FUTURE_CLOCK_SKEW_NS = 300_000_000_000
MEMORY_REFERENCE_MB = 8_192.0


class RegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EdgeNodeConfig:
    edge_node_id: str
    control_url: str
    target_topic: str


@dataclass
class EdgeNodeState:
    config: EdgeNodeConfig
    report: dict[str, Any] | None = None
    last_reported_at_ns: int = 0
    last_status_received_at_ns: int = 0
    last_status_received_monotonic_ns: int = 0
    status: str = "OFFLINE"
    recovery_report_count: int = 0
    base_score: float = 0.0


@dataclass
class LinkSnapshot:
    sender_id: str
    edge_node_id: str
    measured_at_ns: int
    received_at_ns: int
    received_monotonic_ns: int
    rtt_ms_avg: float
    rtt_ms_p95: float
    jitter_ms: float
    available_throughput_mbps: float
    mqtt_publish_success_rate: float


def load_edge_node_configs() -> dict[str, EdgeNodeConfig]:
    """Load registered nodes from one optional JSON environment variable."""

    raw = os.getenv("SCHEDULER_EDGE_NODES_JSON", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistryError(
                "INVALID_NODE_CONFIG",
                f"SCHEDULER_EDGE_NODES_JSON is invalid JSON: {exc}",
            ) from exc
    else:
        payload = {
            "edge_01": {
                "control_url": "http://127.0.0.1:8001",
                "target_topic": "edge/edge_01/input",
            },
        }

    if not isinstance(payload, Mapping) or not payload:
        raise RegistryError("INVALID_NODE_CONFIG", "edge node config must be a non-empty object")

    result: dict[str, EdgeNodeConfig] = {}
    for edge_node_id, value in payload.items():
        node_id = _non_empty_text(edge_node_id, "edge_node_id")
        item = _mapping(value, f"config for {node_id}")
        control_url = _non_empty_text(item.get("control_url"), "control_url")
        target_topic = _non_empty_text(item.get("target_topic"), "target_topic")
        if not control_url.startswith(("http://", "https://")):
            raise RegistryError("INVALID_NODE_CONFIG", "control_url must start with http:// or https://")
        result[node_id] = EdgeNodeConfig(node_id, control_url.rstrip("/"), target_topic)
    return result


class NodeRegistry:
    def __init__(
        self,
        configs: Mapping[str, EdgeNodeConfig] | None = None,
        *,
        status_timeout_ns: int = STATUS_REPORT_TIMEOUT_NS,
        link_timeout_ns: int = LINK_SNAPSHOT_TIMEOUT_NS,
    ) -> None:
        selected = dict(configs or load_edge_node_configs())
        self._nodes = {
            node_id: EdgeNodeState(config=config)
            for node_id, config in selected.items()
        }
        self._links: dict[tuple[str, str], LinkSnapshot] = {}
        self.status_timeout_ns = status_timeout_ns
        self.link_timeout_ns = link_timeout_ns
        self._lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start_monitor(self, interval_seconds: float = 0.5) -> None:
        if self._monitor_thread is not None:
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            args=(interval_seconds,),
            name="scheduler-node-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
            self._monitor_thread = None

    def control_url(self, edge_node_id: str) -> str:
        """Return the registered edge control URL without exposing registry internals."""

        with self._lock:
            state = self._nodes.get(edge_node_id)
            if state is None:
                raise RegistryError(
                    "UNREGISTERED_EDGE_NODE",
                    f"unregistered edge node: {edge_node_id}",
                )
            return state.config.control_url

    def update_status(
        self,
        report: Mapping[str, Any],
        *,
        received_at_ns: int | None = None,
        received_monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        validated = _validate_status_report(report)
        edge_node_id = validated["edge_node_id"]
        now = time.time_ns() if received_at_ns is None else received_at_ns
        monotonic_now = (
            time.monotonic_ns()
            if received_monotonic_ns is None
            else received_monotonic_ns
        )

        with self._lock:
            state = self._nodes.get(edge_node_id)
            if state is None:
                raise RegistryError("UNREGISTERED_EDGE_NODE", f"unregistered edge node: {edge_node_id}")
            if validated["reported_at_ns"] > now + MAX_FUTURE_CLOCK_SKEW_NS:
                return {
                    "edge_node_id": edge_node_id,
                    "accepted": False,
                    "status": state.status,
                    "reason_code": "FUTURE_STATUS_REPORT",
                    "received_at_ns": now,
                }
            if validated["reported_at_ns"] <= state.last_reported_at_ns:
                return {
                    "edge_node_id": edge_node_id,
                    "accepted": False,
                    "status": state.status,
                    "reason_code": "STALE_STATUS_REPORT",
                    "received_at_ns": now,
                }

            previous_monotonic_ns = state.last_status_received_monotonic_ns
            state.report = validated
            state.last_reported_at_ns = validated["reported_at_ns"]
            state.last_status_received_at_ns = now
            state.last_status_received_monotonic_ns = monotonic_now
            state.base_score = _compute_base_score(validated["resources"])
            if state.status == "OFFLINE":
                if (
                    previous_monotonic_ns > 0
                    and monotonic_now - previous_monotonic_ns >= self.status_timeout_ns
                ):
                    state.recovery_report_count = 1
                else:
                    state.recovery_report_count += 1
                    if state.recovery_report_count >= 2:
                        state.status = "ONLINE"
            else:
                state.recovery_report_count = 2

            return {
                "edge_node_id": edge_node_id,
                "accepted": True,
                "status": state.status,
                "base_score": state.base_score,
                "received_at_ns": now,
            }

    def update_link(
        self,
        payload: Mapping[str, Any],
        *,
        received_at_ns: int | None = None,
        received_monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        snapshot = _validate_link_snapshot(
            payload,
            time.time_ns() if received_at_ns is None else received_at_ns,
            (
                time.monotonic_ns()
                if received_monotonic_ns is None
                else received_monotonic_ns
            ),
        )
        with self._lock:
            if snapshot.edge_node_id not in self._nodes:
                raise RegistryError(
                    "UNREGISTERED_EDGE_NODE",
                    f"unregistered edge node: {snapshot.edge_node_id}",
                )
            if snapshot.measured_at_ns > snapshot.received_at_ns + MAX_FUTURE_CLOCK_SKEW_NS:
                return {
                    "sender_id": snapshot.sender_id,
                    "edge_node_id": snapshot.edge_node_id,
                    "accepted": False,
                    "reason_code": "FUTURE_LINK_SNAPSHOT",
                }
            key = (snapshot.sender_id, snapshot.edge_node_id)
            previous = self._links.get(key)
            if previous and snapshot.measured_at_ns <= previous.measured_at_ns:
                return {
                    "sender_id": snapshot.sender_id,
                    "edge_node_id": snapshot.edge_node_id,
                    "accepted": False,
                    "reason_code": "STALE_LINK_SNAPSHOT",
                }
            self._links[key] = snapshot
        return {
            "sender_id": snapshot.sender_id,
            "edge_node_id": snapshot.edge_node_id,
            "accepted": True,
        }

    def online_nodes(self, *, now_ns: int | None = None) -> list[EdgeNodeState]:
        self.refresh_liveness(now_ns=now_ns)
        with self._lock:
            return [
                copy.deepcopy(state)
                for state in self._nodes.values()
                if state.status == "ONLINE" and state.report is not None
            ]

    def link_snapshot(
        self,
        sender_id: str,
        edge_node_id: str,
        *,
        now_ns: int | None = None,
    ) -> LinkSnapshot | None:
        now = time.monotonic_ns() if now_ns is None else now_ns
        with self._lock:
            snapshot = self._links.get((sender_id, edge_node_id))
            if snapshot is None or now - snapshot.received_monotonic_ns >= self.link_timeout_ns:
                return None
            return copy.deepcopy(snapshot)

    def refresh_liveness(self, *, now_ns: int | None = None) -> None:
        now = time.monotonic_ns() if now_ns is None else now_ns
        with self._lock:
            for state in self._nodes.values():
                if (
                    state.status == "ONLINE"
                    and now - state.last_status_received_monotonic_ns >= self.status_timeout_ns
                ):
                    state.status = "OFFLINE"
                    state.recovery_report_count = 0

    def status_counts(self) -> dict[str, int]:
        self.refresh_liveness()
        with self._lock:
            return {
                "registered": len(self._nodes),
                "online": sum(state.status == "ONLINE" for state in self._nodes.values()),
                "offline": sum(state.status == "OFFLINE" for state in self._nodes.values()),
            }

    def _monitor(self, interval_seconds: float) -> None:
        while not self._monitor_stop.wait(interval_seconds):
            self.refresh_liveness()


def _compute_base_score(resources: Mapping[str, Any]) -> float:
    cpu_idle_score = 100.0 - float(resources["cpu_utilization_percent"])
    memory_score = min(float(resources["memory_available_mb"]) / MEMORY_REFERENCE_MB, 1.0) * 100.0
    queue_score = max(0.0, 100.0 - float(resources["queue_length"]) * 10.0)
    return round(cpu_idle_score * 0.40 + memory_score * 0.30 + queue_score * 0.30, 4)


def _validate_status_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    report = _mapping(payload, "status report")
    edge_node_id = _non_empty_text(report.get("edge_node_id"), "edge_node_id")
    reported_at_ns = _positive_int(report.get("reported_at_ns"), "reported_at_ns")
    resources = _mapping(report.get("resources"), "resources")
    validated_resources = {
        "logical_cpu_count": _positive_int(resources.get("logical_cpu_count"), "logical_cpu_count"),
        "cpu_utilization_percent": _bounded_float(
            resources.get("cpu_utilization_percent"), "cpu_utilization_percent", 0.0, 100.0
        ),
        "memory_available_mb": _non_negative_float(
            resources.get("memory_available_mb"), "memory_available_mb"
        ),
        "gpu_available": _boolean(resources.get("gpu_available"), "gpu_available"),
        "npu_available": _boolean(resources.get("npu_available"), "npu_available"),
        "queue_length": _non_negative_int(resources.get("queue_length"), "queue_length"),
    }

    models = report.get("models")
    if not isinstance(models, list):
        raise RegistryError("INVALID_STATUS_REPORT", "models must be an array")
    validated_models = []
    for model in models:
        item = _mapping(model, "model")
        validated_models.append(
            {
                "model_version": _non_empty_text(item.get("model_version"), "model_version"),
                "load_status": _non_empty_text(item.get("load_status"), "load_status").upper(),
            }
        )

    validated_network = None
    if report.get("network_to_scheduler") is not None:
        network = _mapping(report.get("network_to_scheduler"), "network_to_scheduler")
        validated_network = {
            "measured_at_ns": _positive_int(network.get("measured_at_ns"), "measured_at_ns"),
            "available_uplink_mbps_estimate": _non_negative_float(
                network.get("available_uplink_mbps_estimate"), "available_uplink_mbps_estimate"
            ),
            "rtt_ms_avg": _non_negative_float(network.get("rtt_ms_avg"), "rtt_ms_avg"),
            "rtt_ms_p95": _non_negative_float(network.get("rtt_ms_p95"), "rtt_ms_p95"),
            "loss_rate": _bounded_float(network.get("loss_rate"), "loss_rate", 0.0, 1.0),
        }
    last_task_activity_ns = _non_negative_int(
        report.get("last_task_activity_ns"), "last_task_activity_ns"
    )
    validated = {
        "edge_node_id": edge_node_id,
        "reported_at_ns": reported_at_ns,
        "resources": validated_resources,
        "models": validated_models,
        "last_task_activity_ns": last_task_activity_ns,
    }
    if validated_network is not None:
        validated["network_to_scheduler"] = validated_network
    return validated


def _validate_link_snapshot(
    payload: Mapping[str, Any],
    received_at_ns: int,
    received_monotonic_ns: int,
) -> LinkSnapshot:
    try:
        item = _mapping(payload, "link snapshot")
        return LinkSnapshot(
            sender_id=_non_empty_text(item.get("sender_id"), "sender_id"),
            edge_node_id=_non_empty_text(item.get("edge_node_id"), "edge_node_id"),
            measured_at_ns=_positive_int(item.get("measured_at_ns"), "measured_at_ns"),
            received_at_ns=received_at_ns,
            received_monotonic_ns=received_monotonic_ns,
            rtt_ms_avg=_non_negative_float(item.get("rtt_ms_avg"), "rtt_ms_avg"),
            rtt_ms_p95=_non_negative_float(item.get("rtt_ms_p95"), "rtt_ms_p95"),
            jitter_ms=_non_negative_float(item.get("jitter_ms"), "jitter_ms"),
            available_throughput_mbps=_non_negative_float(
                item.get("available_throughput_mbps"), "available_throughput_mbps"
            ),
            mqtt_publish_success_rate=_bounded_float(
                item.get("mqtt_publish_success_rate"), "mqtt_publish_success_rate", 0.0, 1.0
            ),
        )
    except RegistryError as error:
        raise RegistryError("INVALID_LINK_SNAPSHOT", error.message) from error


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be an object")
    return value


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be a non-negative integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be a boolean")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} must be finite")
    return number


def _non_negative_float(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise RegistryError("INVALID_STATUS_REPORT", f"{field} cannot be negative")
    return number


def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    number = _number(value, field)
    if not minimum <= number <= maximum:
        raise RegistryError(
            "INVALID_STATUS_REPORT",
            f"{field} must be between {minimum} and {maximum}",
        )
    return number
