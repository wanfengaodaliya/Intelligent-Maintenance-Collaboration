"""Choose the best edge node for one sender task."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.control_auth import (
    DEFAULT_KEY_ID,
    encode_control_json,
    load_control_shared_secret,
    sign_control_request,
)

try:
    from .node_registry import EdgeNodeState, LinkSnapshot, NodeRegistry
    from .task_repository import TaskRepository, TaskRepositoryError
except ImportError:  # Allows running scheduler/api.py directly.
    from node_registry import EdgeNodeState, LinkSnapshot, NodeRegistry
    from task_repository import TaskRepository, TaskRepositoryError


EXPECTED_PACKET_COUNT = 80
# 缓传门控：链路可用带宽低于该阈值时直接拒绝候选，等待网络恢复后重试。
MIN_BUFFERED_THROUGHPUT_MBPS = 4.0
REALTIME_DELIVERY_MODE = "realtime"
BUFFERED_DELIVERY_MODE = "buffered"
_MODULE_LOGGER = logging.getLogger(__name__)
ASSIGNMENT_REQUEST_FIELDS = frozenset(
    {
        "device_id",
        "sender_id",
        "task_id",
        "bearing_id",
        "packet_size_bytes",
        "expected_packet_count",
        "expected_duration_ms",
        "created_timestamp_ns",
    }
)
SENDER_ID_PATTERN = re.compile(r"^sender_([0-9]+)$")
TASK_ID_PATTERN = re.compile(r"^sd_([0-9]+)_tk_([0-9]{4})$")
EDGE_ACK_TIMEOUT_SECONDS = 0.5
SCHEDULING_TIMEOUT_SECONDS = 1.5
MAX_EFFECTIVE_QUEUE_LENGTH = 10
MIN_RESERVATION_TTL_SECONDS = 30.0


class AssignmentError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


@dataclass(frozen=True)
class AssignmentDecision:
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    target_topic: str
    delivery_mode: str
    delivery_interval_ms: int
    available_throughput_mbps: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "sender_id": self.sender_id,
            "task_id": self.task_id,
            "bearing_id": self.bearing_id,
            "target_topic": self.target_topic,
            "delivery_mode": self.delivery_mode,
            "delivery_interval_ms": self.delivery_interval_ms,
            "available_throughput_mbps": self.available_throughput_mbps,
        }


@dataclass(frozen=True)
class RankedNode:
    state: EdgeNodeState
    total_score: float
    network_score: float
    stability_score: float
    delivery_mode: str


@dataclass(frozen=True)
class NodeReservation:
    edge_node_id: str
    expires_at: float


class EdgeAssignmentClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = EDGE_ACK_TIMEOUT_SECONDS,
        shared_secret: bytes | str | None = None,
        key_id: str | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.shared_secret = shared_secret
        self.key_id = key_id or os.getenv("EDGE_CONTROL_KEY_ID", DEFAULT_KEY_ID)

    def request_assignment(
        self,
        node: EdgeNodeState,
        request: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = {
            "task_id": request["task_id"],
            "target_edge_node_id": node.config.edge_node_id,
            "task_type": "BEARING_EDGE_INFERENCE",
            "input_ref": {
                "device_id": request["device_id"],
                "expected_bearing_ids": [request["bearing_id"]],
                "assigned_bearings": [
                    {
                        "bearing_id": request["bearing_id"],
                        "sender_id": request["sender_id"],
                        "expected_packet_count": request["expected_packet_count"],
                    }
                ],
            },
            "dispatched_at_ns": request["created_timestamp_ns"],
        }
        body = encode_control_json(payload)
        secret = (
            load_control_shared_secret()
            if self.shared_secret is None
            else self.shared_secret
        )
        try:
            http_request = Request(
                node.config.control_url + "/edge/tasks",
                data=body,
                headers=sign_control_request(
                    secret,
                    method="POST",
                    path="/edge/tasks",
                    body=body,
                    key_id=self.key_id,
                ),
                method="POST",
            )
            with urlopen(
                http_request,
                timeout=timeout_seconds or self.timeout_seconds,
            ) as response:
                ack = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                ack = json.loads(exc.read().decode("utf-8"))
            except (OSError, ValueError) as decode_error:
                raise AssignmentError(
                    "EDGE_ACK_FAILED",
                    f"edge node {node.config.edge_node_id} did not acknowledge: {exc}",
                    503,
                ) from decode_error
            validated_ack = _validate_ack(
                ack, request["task_id"], node.config.edge_node_id
            )
            if validated_ack["ack_status"] != "REJECTED":
                raise AssignmentError(
                    "INVALID_EDGE_ACK",
                    "a non-success HTTP response cannot accept a task",
                    502,
                )
            return validated_ack
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise AssignmentError(
                "EDGE_ACK_FAILED",
                f"edge node {node.config.edge_node_id} did not acknowledge: {exc}",
                503,
            ) from exc
        return _validate_ack(ack, request["task_id"], node.config.edge_node_id)


class AssignmentScheduler:
    def __init__(
        self,
        registry: NodeRegistry,
        repository: TaskRepository,
        *,
        edge_client: EdgeAssignmentClient | Any | None = None,
        scheduling_timeout_seconds: float = SCHEDULING_TIMEOUT_SECONDS,
        reservation_ttl_seconds: float = MIN_RESERVATION_TTL_SECONDS,
    ) -> None:
        if (
            not math.isfinite(reservation_ttl_seconds)
            or reservation_ttl_seconds <= 0
        ):
            raise ValueError(
                "reservation_ttl_seconds must be a finite positive number"
            )
        self.registry = registry
        self.repository = repository
        self.edge_client = edge_client or EdgeAssignmentClient()
        self.scheduling_timeout_seconds = scheduling_timeout_seconds
        self.reservation_ttl_seconds = reservation_ttl_seconds
        self._allocation_lock = threading.RLock()
        self._reservations: dict[str, NodeReservation] = {}

    def decide(self, request: Mapping[str, Any]) -> AssignmentDecision:
        validated = validate_assignment_request(request)
        claim_id = str(uuid.uuid4())
        deadline = time.monotonic() + self.scheduling_timeout_seconds
        existing = self._claim_or_wait(validated, claim_id, deadline)

        if existing["assignment_status"] == "ASSIGNED":
            if not existing.get("edge_node_id") or not existing.get("target_topic"):
                raise AssignmentError(
                    "TASK_ID_CONFLICT",
                    "task_id belongs to an incompatible batch assignment",
                    409,
                )
            # 已分配任务幂等重试：从 repository 中的 edge_node_id 重新读取当前
            # 链路快照并调用 _delivery_plan()，不修改数据库表结构。
            existing_link = self.registry.link_snapshot(
                existing["sender_id"], existing["edge_node_id"]
            )
            retry_mode, retry_interval, retry_mbps = _delivery_plan(
                validated, existing_link
            )
            return AssignmentDecision(
                device_id=existing["device_id"],
                sender_id=existing["sender_id"],
                task_id=existing["task_id"],
                bearing_id=existing["bearing_id"],
                target_topic=existing["target_topic"],
                delivery_mode=retry_mode,
                delivery_interval_ms=retry_interval,
                available_throughput_mbps=retry_mbps,
            )

        retry_constraints = self.repository.retry_constraints(validated["task_id"])
        candidate_rejections: list[dict[str, Any]] = []
        node = self._select_and_reserve(
            validated,
            deadline=deadline,
            excluded_edge_node_ids=frozenset(
                retry_constraints["rejected_edge_node_ids"]
            ),
            pinned_edge_node_id=retry_constraints["pinned_edge_node_id"],
            rejection_sink=candidate_rejections,
        )
        if time.monotonic() >= deadline:
            self.release_reservation(validated["task_id"])
            self.repository.mark_failed(
                validated["task_id"], "SCHEDULING_TIMEOUT", claim_id
            )
            raise AssignmentError(
                "SCHEDULING_TIMEOUT",
                "edge node assignment exceeded its time budget",
                503,
            )
        if node is None:
            failed_at_ns = self.repository.mark_failed(
                validated["task_id"], "NO_AVAILABLE_EDGE_NODE", claim_id
            )
            if time.monotonic() >= deadline:
                if failed_at_ns is not None:
                    self.repository.replace_failure_code(
                        validated["task_id"],
                        failed_at_ns,
                        "SCHEDULING_TIMEOUT",
                    )
                raise AssignmentError(
                    "SCHEDULING_TIMEOUT",
                    "edge node assignment exceeded its time budget",
                    503,
                )
            raise AssignmentError(
                "NO_AVAILABLE_EDGE_NODE",
                "no edge node can accept this task",
                503,
                details={
                    "retryable": True,
                    "retry_after_ms": 500,
                    "candidate_rejections": candidate_rejections,
                },
            )

        keep_reservation = False
        try:
            attempt_id, _ = self.repository.start_attempt(
                validated["task_id"],
                node.config.edge_node_id,
                claim_id,
                bearing_id=validated["bearing_id"],
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.repository.fail_attempt(
                    attempt_id,
                    validated["task_id"],
                    claim_id,
                    "SCHEDULING_TIMEOUT",
                )
                raise AssignmentError(
                    "SCHEDULING_TIMEOUT",
                    "edge node assignment exceeded its time budget",
                    503,
                )

            try:
                ack = self.edge_client.request_assignment(
                    node,
                    validated,
                    timeout_seconds=min(EDGE_ACK_TIMEOUT_SECONDS, remaining),
                )
            except AssignmentError as exc:
                timed_out = time.monotonic() >= deadline
                failure_code = "SCHEDULING_TIMEOUT" if timed_out else exc.code
                self.repository.fail_attempt(
                    attempt_id,
                    validated["task_id"],
                    claim_id,
                    failure_code,
                )
                raise AssignmentError(
                    failure_code,
                    (
                        "edge node assignment exceeded its time budget"
                        if timed_out
                        else exc.message
                    ),
                    503,
                ) from exc
            except Exception as exc:
                failure_code = (
                    "SCHEDULING_TIMEOUT"
                    if time.monotonic() >= deadline
                    else "EDGE_ACK_FAILED"
                )
                self.repository.fail_attempt(
                    attempt_id,
                    validated["task_id"],
                    claim_id,
                    failure_code,
                )
                raise AssignmentError(
                    failure_code,
                    (
                        "edge node assignment exceeded its time budget"
                        if failure_code == "SCHEDULING_TIMEOUT"
                        else "the highest-ranked edge node did not acknowledge"
                    ),
                    503,
                ) from exc

            if ack["ack_status"] == "ACCEPTED" and time.monotonic() >= deadline:
                self.repository.fail_attempt(
                    attempt_id,
                    validated["task_id"],
                    claim_id,
                    "SCHEDULING_TIMEOUT",
                )
                raise AssignmentError(
                    "SCHEDULING_TIMEOUT",
                    "edge node assignment exceeded its time budget",
                    503,
                )

            if ack["ack_status"] != "ACCEPTED":
                failure_code = ack["reason_code"] or "EDGE_REJECTED"
                failed_at_ns = self.repository.fail_attempt(
                    attempt_id,
                    validated["task_id"],
                    claim_id,
                    failure_code,
                    attempt_status="REJECTED",
                )
                if time.monotonic() >= deadline:
                    failure_code = "SCHEDULING_TIMEOUT"
                    self.repository.replace_failure_code(
                        validated["task_id"],
                        failed_at_ns,
                        failure_code,
                        attempt_id=attempt_id,
                    )
                raise AssignmentError(
                    failure_code,
                    "the highest-ranked edge node rejected this task",
                    503,
                )

            self.repository.accept_attempt(
                attempt_id,
                validated["task_id"],
                claim_id,
                node.config.edge_node_id,
                node.config.target_topic,
            )
            keep_reservation = True
            # 新任务首次分配成功：基于当前链路快照计算缓传计划。
            success_link = self.registry.link_snapshot(
                validated["sender_id"], node.config.edge_node_id
            )
            success_mode, success_interval, success_mbps = _delivery_plan(
                validated, success_link
            )
            return AssignmentDecision(
                device_id=validated["device_id"],
                sender_id=validated["sender_id"],
                task_id=validated["task_id"],
                bearing_id=validated["bearing_id"],
                target_topic=node.config.target_topic,
                delivery_mode=success_mode,
                delivery_interval_ms=success_interval,
                available_throughput_mbps=success_mbps,
            )
        finally:
            if not keep_reservation:
                self.release_reservation(validated["task_id"])

    def save_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        saved = self.repository.save_result(request)
        self.release_reservation(saved["task_id"])
        return saved

    def release_reservation(self, task_id: str) -> None:
        with self._allocation_lock:
            self._reservations.pop(task_id, None)

    def _select_and_reserve(
        self,
        request: Mapping[str, Any],
        *,
        deadline: float,
        excluded_edge_node_ids: frozenset[str],
        pinned_edge_node_id: str | None,
        rejection_sink: list[dict[str, Any]] | None = None,
    ) -> EdgeNodeState | None:
        with self._allocation_lock:
            reservation_counts = self._active_reservation_counts()
            candidates = self._rank_candidates(
                request,
                deadline=deadline,
                reservation_counts=reservation_counts,
                excluded_edge_node_ids=excluded_edge_node_ids,
                pinned_edge_node_id=pinned_edge_node_id,
                rejection_sink=rejection_sink,
            )
            if not candidates:
                return None
            node = candidates[0].state
            duration_seconds = float(request["expected_duration_ms"]) / 1000.0
            ttl_seconds = max(
                self.reservation_ttl_seconds,
                duration_seconds * 2.0,
            )
            self._reservations[request["task_id"]] = NodeReservation(
                edge_node_id=node.config.edge_node_id,
                expires_at=time.monotonic() + ttl_seconds,
            )
            return node

    def _active_reservation_counts(self) -> dict[str, int]:
        now = time.monotonic()
        expired_task_ids = [
            task_id
            for task_id, reservation in self._reservations.items()
            if reservation.expires_at <= now
        ]
        for task_id in expired_task_ids:
            del self._reservations[task_id]

        counts: dict[str, int] = {}
        for reservation in self._reservations.values():
            counts[reservation.edge_node_id] = (
                counts.get(reservation.edge_node_id, 0) + 1
            )
        return counts

    def _claim_or_wait(
        self,
        request: Mapping[str, Any],
        claim_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        while True:
            try:
                return self.repository.claim(
                    request,
                    claim_id,
                    lease_seconds=self.scheduling_timeout_seconds + 0.1,
                )
            except TaskRepositoryError as exc:
                if exc.code != "TASK_SCHEDULING":
                    raise AssignmentError(exc.code, exc.message, exc.status_code) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssignmentError(
                        "TASK_SCHEDULING",
                        "task assignment is still in progress",
                        409,
                    ) from exc
                time.sleep(min(0.05, remaining))

    def _rank_candidates(
        self,
        request: Mapping[str, Any],
        *,
        deadline: float,
        reservation_counts: Mapping[str, int] | None = None,
        excluded_edge_node_ids: frozenset[str] = frozenset(),
        pinned_edge_node_id: str | None = None,
        rejection_sink: list[dict[str, Any]] | None = None,
    ) -> list[RankedNode]:
        required_mbps = _required_throughput_mbps(request)
        active_reservations = reservation_counts or {}
        ranked: list[RankedNode] = []
        for node in self.registry.registered_nodes():
            if time.monotonic() >= deadline:
                break
            if pinned_edge_node_id is not None:
                if node.config.edge_node_id != pinned_edge_node_id:
                    continue
            elif node.config.edge_node_id in excluded_edge_node_ids:
                continue
            if node.status != "ONLINE" or node.report is None:
                _append_rejection(
                    rejection_sink,
                    node,
                    "NODE_OFFLINE",
                    "edge node is offline or has no current status report",
                    {"status": node.status},
                )
                continue
            resources = node.report["resources"] if node.report else {}
            # EDGE-2: 采集状态不可信（FAILED）时保守排除，
            # 绝不让 FAILED queue=0 被当作“空闲”通过容量门控。
            gate_reason = _measurement_gate(resources)
            if gate_reason is not None:
                _MODULE_LOGGER.debug(
                    "skip edge node %s in ranking: %s",
                    node.config.edge_node_id,
                    gate_reason,
                )
                _append_rejection(
                    rejection_sink,
                    node,
                    "MEASUREMENT_FAILED",
                    "required resource measurement is not trustworthy",
                    {"measurement": gate_reason},
                )
                continue
            cpu_utilization = float(resources.get("cpu_utilization_percent", 100.0))
            if cpu_utilization > 90.0:
                _append_rejection(
                    rejection_sink,
                    node,
                    "CPU_OVERLOADED",
                    "CPU utilization exceeds the scheduling limit",
                    {"cpu_utilization_percent": cpu_utilization, "maximum_percent": 90.0},
                )
                continue
            # EDGE-3: 只有 memory status 为 OK（或字段缺失，等同旧行为）才执行
            # 512MB 硬门控；DEGRADED 时 0MiB 是遥测 fallback，不是真实低内存，
            # 该分量已由 base_score 中性化，故不执行硬门控。
            if (
                resources.get("memory_measurement_status", "OK") == "OK"
                and float(resources.get("memory_available_mb", 0.0)) < 512.0
            ):
                _append_rejection(
                    rejection_sink,
                    node,
                    "INSUFFICIENT_MEMORY",
                    "available memory is below the scheduling minimum",
                    {
                        "available_memory_mb": float(resources.get("memory_available_mb", 0.0)),
                        "minimum_memory_mb": 512.0,
                    },
                )
                continue
            reported_queue_length = int(resources.get("queue_length", 999))
            reservation_count = active_reservations.get(
                node.config.edge_node_id, 0
            )
            effective_queue_length = reported_queue_length + reservation_count
            if effective_queue_length >= MAX_EFFECTIVE_QUEUE_LENGTH:
                _append_rejection(
                    rejection_sink,
                    node,
                    "QUEUE_CAPACITY_EXCEEDED",
                    "effective queue length reached the scheduling limit",
                    {
                        "reported_queue_length": reported_queue_length,
                        "reservation_count": reservation_count,
                        "effective_queue_length": effective_queue_length,
                        "queue_limit": MAX_EFFECTIVE_QUEUE_LENGTH,
                    },
                )
                continue
            if not _has_loaded_model(node):
                _append_rejection(
                    rejection_sink,
                    node,
                    "MODEL_NOT_LOADED",
                    "edge node has no loaded diagnosis model",
                    {},
                )
                continue

            link = self.registry.link_snapshot(
                request["sender_id"], node.config.edge_node_id
            )
            if (
                link is not None
                and link.available_throughput_mbps < MIN_BUFFERED_THROUGHPUT_MBPS
            ):
                # 低于 4Mbps 直接拒绝候选；Sender 沿用现有调度重试机制等待恢复。
                _append_rejection(
                    rejection_sink,
                    node,
                    "INSUFFICIENT_BANDWIDTH",
                    "available throughput is below the buffered delivery minimum",
                    {
                        "required_mbps": round(required_mbps, 4),
                        "available_mbps": round(link.available_throughput_mbps, 4),
                        "minimum_buffered_mbps": MIN_BUFFERED_THROUGHPUT_MBPS,
                    },
                )
                continue
            # 4Mbps 以上、不足实时需求时允许成为候选，标记为 buffered。
            delivery_mode, _, _ = _delivery_plan(request, link)
            network_score = _network_score(link, required_mbps)
            stability_score = self.repository.stability_score(node.config.edge_node_id)
            if time.monotonic() >= deadline:
                break
            resource_score = _base_score_with_reservations(
                node.base_score,
                reported_queue_length,
                reservation_count,
            )
            total_score = (
                resource_score * 0.35
                + network_score * 0.45
                + stability_score * 0.20
            )
            ranked.append(
                RankedNode(
                    state=node,
                    total_score=round(total_score, 4),
                    network_score=network_score,
                    stability_score=stability_score,
                    delivery_mode=delivery_mode,
                )
            )
        # 实时候选永远优先于缓传候选；同模式内按总分降序、edge_node_id 升序。
        return sorted(
            ranked,
            key=lambda item: (
                item.delivery_mode != REALTIME_DELIVERY_MODE,
                -item.total_score,
                item.state.config.edge_node_id,
            ),
        )


def _append_rejection(
    sink: list[dict[str, Any]] | None,
    node: EdgeNodeState,
    reason_code: str,
    reason_message: str,
    metrics: Mapping[str, Any],
) -> None:
    if sink is None:
        return
    sink.append(
        {
            "edge_node_id": node.config.edge_node_id,
            "reason_code": reason_code,
            "reason_message": reason_message,
            "metrics": dict(metrics),
        }
    )


def validate_assignment_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AssignmentError("INVALID_REQUEST", "request must be an object")
    if set(payload) != ASSIGNMENT_REQUEST_FIELDS:
        missing = sorted(ASSIGNMENT_REQUEST_FIELDS - set(payload))
        unexpected = sorted(set(payload) - ASSIGNMENT_REQUEST_FIELDS)
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected fields: {', '.join(unexpected)}")
        raise AssignmentError("INVALID_REQUEST", "; ".join(details))

    device_id = _non_empty_text(payload.get("device_id"), "device_id")
    sender_id = _non_empty_text(payload.get("sender_id"), "sender_id")
    sender_match = SENDER_ID_PATTERN.fullmatch(sender_id)
    if sender_match is None:
        raise AssignmentError(
            "INVALID_REQUEST", "sender_id must match sender_<sender number>"
        )
    task_id = _non_empty_text(payload.get("task_id"), "task_id")
    task_match = TASK_ID_PATTERN.fullmatch(task_id)
    if task_match is None or not 1 <= int(task_match.group(2)) <= 9999:
        raise AssignmentError(
            "INVALID_REQUEST",
            "task_id must match sd_<sender number>_tk_<0001-9999>",
        )
    if task_match.group(1) != sender_match.group(1):
        raise AssignmentError(
            "INVALID_REQUEST",
            "task_id sender number must match sender_id",
        )
    expected_packet_count = _positive_int(
        payload.get("expected_packet_count"), "expected_packet_count"
    )
    if expected_packet_count != EXPECTED_PACKET_COUNT:
        raise AssignmentError(
            "INVALID_REQUEST",
            f"expected_packet_count must equal {EXPECTED_PACKET_COUNT}",
        )
    return {
        "device_id": device_id,
        "sender_id": sender_id,
        "task_id": task_id,
        "bearing_id": _non_empty_text(payload.get("bearing_id"), "bearing_id"),
        "packet_size_bytes": _positive_int(
            payload.get("packet_size_bytes"), "packet_size_bytes"
        ),
        "expected_packet_count": expected_packet_count,
        "expected_duration_ms": _positive_int(
            payload.get("expected_duration_ms"), "expected_duration_ms"
        ),
        "created_timestamp_ns": _positive_int(
            payload.get("created_timestamp_ns"), "created_timestamp_ns"
        ),
    }


def _required_throughput_mbps(request: Mapping[str, Any]) -> float:
    duration_seconds = float(request["expected_duration_ms"]) / 1000.0
    required_bits = (
        float(request["packet_size_bytes"])
        * int(request["expected_packet_count"])
        * 8.0
    )
    return required_bits / duration_seconds / 1_000_000.0


def _delivery_plan(
    request: Mapping[str, Any],
    link: LinkSnapshot | None,
) -> tuple[str, int, float | None]:
    """计算缓传计划，返回 (delivery_mode, delivery_interval_ms, available_mbps)。

    - 无链路快照时保持旧兼容行为：realtime + 50ms（base_interval）。
    - 链路带宽足以覆盖传感器采样窗口时返回 realtime。
    - 带宽不足实时需求但 ≥ 4Mbps 时返回 buffered，并按链路容量降低发送速度。
    - 调用方（_rank_candidates）负责在 < 4Mbps 时直接拒绝候选。
    """
    base_interval_ms = math.ceil(
        request["expected_duration_ms"] / request["expected_packet_count"]
    )
    if link is None:
        return (REALTIME_DELIVERY_MODE, base_interval_ms, None)
    effective_mbps = (
        link.available_throughput_mbps
        * (1.0 - link.simulated_packet_loss_rate)
    )
    # packet_size_bytes × 8 / Mbps / 1000 的结果是毫秒。
    wire_interval_ms = math.ceil(
        request["packet_size_bytes"] * 8.0
        / max(effective_mbps, 0.001)
        / 1000.0
    )
    delivery_interval_ms = max(base_interval_ms, wire_interval_ms)
    delivery_mode = (
        REALTIME_DELIVERY_MODE
        if delivery_interval_ms <= base_interval_ms
        else BUFFERED_DELIVERY_MODE
    )
    return (delivery_mode, delivery_interval_ms, link.available_throughput_mbps)


def _base_score_with_reservations(
    reported_base_score: float,
    reported_queue_length: int,
    reservation_count: int,
) -> float:
    reported_queue_score = max(0.0, 100.0 - reported_queue_length * 10.0)
    effective_queue_score = max(
        0.0,
        100.0 - (reported_queue_length + reservation_count) * 10.0,
    )
    return round(
        reported_base_score
        + (effective_queue_score - reported_queue_score) * 0.30,
        4,
    )


def _network_score(snapshot: LinkSnapshot | None, required_mbps: float) -> float:
    if snapshot is None:
        return 50.0
    rtt_score = max(0.0, 100.0 - snapshot.rtt_ms_p95 / 200.0 * 100.0)
    bandwidth_score = min(
        snapshot.available_throughput_mbps / max(required_mbps, 0.001),
        1.0,
    ) * 100.0
    # 该分项基于网络模型生成的模拟丢包率推导链路可靠性，
    # 不是 MQTT 实测发布成功率。
    loss_score = (1.0 - snapshot.simulated_packet_loss_rate) * 100.0
    return round(rtt_score * 0.40 + bandwidth_score * 0.40 + loss_score * 0.20, 4)


def _measurement_gate(resources: Mapping[str, Any]) -> str | None:
    """EDGE-2: 基于采集状态做候选保守排除。

    返回跳过原因（None=通过）。规则：
    - CPU FAILED / QUEUE FAILED：采集不可信且无法确认空闲/未过载 → 排除；
    - CPU DEGRADED：不排除（psutil 缺失等遥测缺口），仅由 base_score 中性化；
    - QUEUE STALE：TTL 内使用 last-known-good 真实历史值，不排除；
    - 字段缺失：默认 OK，保持旧行为。
    """
    if resources.get("cpu_measurement_status") == "FAILED":
        return "cpu_measurement_failed"
    if resources.get("queue_measurement_status") == "FAILED":
        return "queue_measurement_failed"
    # EDGE-3: 内存采集 FAILED 时拿到的 0MiB 不可信，保守排除候选。
    if resources.get("memory_measurement_status") == "FAILED":
        return "memory_measurement_failed"
    return None


def _has_loaded_model(node: EdgeNodeState) -> bool:
    if not node.report:
        return False
    return any(
        model.get("load_status") == "LOADED"
        for model in node.report.get("models", [])
    )


def _validate_ack(payload: Any, task_id: str, edge_node_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AssignmentError("INVALID_EDGE_ACK", "edge acknowledgement must be an object", 502)
    if payload.get("task_id") != task_id:
        raise AssignmentError("INVALID_EDGE_ACK", "ack task_id does not match", 502)
    if payload.get("edge_node_id") != edge_node_id:
        raise AssignmentError("INVALID_EDGE_ACK", "ack edge_node_id does not match", 502)
    ack_status = payload.get("ack_status")
    if ack_status not in {"ACCEPTED", "REJECTED"}:
        raise AssignmentError(
            "INVALID_EDGE_ACK",
            "ack_status must be ACCEPTED or REJECTED",
            502,
        )
    reason_code = payload.get("reason_code")
    if ack_status == "REJECTED" and (
        not isinstance(reason_code, str) or not reason_code.strip()
    ):
        raise AssignmentError(
            "INVALID_EDGE_ACK",
            "a rejected acknowledgement needs reason_code",
            502,
        )
    _ack_positive_int(payload.get("received_at_ns"), "received_at_ns")
    _ack_positive_int(payload.get("acknowledged_at_ns"), "acknowledged_at_ns")
    return {
        "task_id": task_id,
        "edge_node_id": edge_node_id,
        "ack_status": ack_status,
        "reason_code": reason_code.strip() if isinstance(reason_code, str) else None,
    }


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssignmentError("INVALID_REQUEST", f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AssignmentError("INVALID_REQUEST", f"{field} must be a positive integer")
    return value


def _ack_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AssignmentError(
            "INVALID_EDGE_ACK",
            f"{field} must be a positive integer",
            502,
        )
    return value
