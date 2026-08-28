from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests


class SchedulerError(RuntimeError):
    def __init__(self, message: str, retry_count: int = 0) -> None:
        super().__init__(message)
        self.retry_count = retry_count


@dataclass(frozen=True)
class ScheduleAssignment:
    device_id: str
    sender_id: str
    task_id: str
    bearing_id: str
    target_topic: str
    schedule_retry_count: int
    delivery_mode: str
    delivery_interval_ms: int
    available_throughput_mbps: float | None


def validate_assignment(
    payload: dict[str, Any],
    *,
    expected_device_id: str,
    expected_sender_id: str,
    expected_task_id: str,
    expected_bearing_id: str,
    retry_count: int = 0,
) -> ScheduleAssignment:
    if not isinstance(payload, dict):
        raise SchedulerError("scheduler response must be a JSON object", retry_count)
    expected = {
        "device_id": expected_device_id,
        "sender_id": expected_sender_id,
        "task_id": expected_task_id,
        "bearing_id": expected_bearing_id,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise SchedulerError(f"scheduler response {field} does not match request", retry_count)
    target_topic = payload.get("target_topic")
    if not isinstance(target_topic, str) or not target_topic.strip():
        raise SchedulerError("scheduler response target_topic is missing", retry_count)
    # 缓传字段：缺少时回退为 realtime + 50ms，兼容旧 Scheduler 响应。
    delivery_mode = payload.get("delivery_mode", "realtime")
    if delivery_mode not in ("realtime", "buffered"):
        raise SchedulerError(
            "scheduler response delivery_mode must be realtime or buffered",
            retry_count,
        )
    delivery_interval_ms = payload.get("delivery_interval_ms", 50)
    # 布尔是 int 的子类，必须显式拒绝；间隔必须是正整数。
    if (
        isinstance(delivery_interval_ms, bool)
        or not isinstance(delivery_interval_ms, int)
        or delivery_interval_ms <= 0
    ):
        raise SchedulerError(
            "scheduler response delivery_interval_ms must be a positive integer",
            retry_count,
        )
    available_throughput_mbps = payload.get("available_throughput_mbps")
    if available_throughput_mbps is not None:
        if (
            isinstance(available_throughput_mbps, bool)
            or not isinstance(available_throughput_mbps, (int, float))
            or float(available_throughput_mbps) < 0
        ):
            raise SchedulerError(
                "scheduler response available_throughput_mbps must be a non-negative number",
                retry_count,
            )
        available_throughput_mbps = float(available_throughput_mbps)
    return ScheduleAssignment(
        device_id=expected_device_id,
        sender_id=expected_sender_id,
        task_id=expected_task_id,
        bearing_id=expected_bearing_id,
        target_topic=target_topic.strip(),
        schedule_retry_count=retry_count,
        delivery_mode=delivery_mode,
        delivery_interval_ms=delivery_interval_ms,
        available_throughput_mbps=available_throughput_mbps,
    )


class SchedulerClient:
    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_delay_seconds: float = 0.5,
        retry_delay_max_seconds: float = 16.0,
        retry_jitter_ratio: float = 0.2,
        retry_window_seconds: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.retry_delay_max_seconds = retry_delay_max_seconds
        self.retry_jitter_ratio = retry_jitter_ratio
        self.retry_window_seconds = retry_window_seconds
        self.session = session or requests.Session()

    def _retry_delay(self, attempt: int) -> float:
        delay = self.retry_delay_seconds * (2**attempt)
        if self.retry_jitter_ratio:
            delay *= random.uniform(
                1.0 - self.retry_jitter_ratio,
                1.0 + self.retry_jitter_ratio,
            )
        return min(delay, self.retry_delay_max_seconds)

    def _wait_before_retry(self, attempt: int, deadline: float) -> bool:
        if attempt >= self.max_retries or not self.retry_delay_seconds:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(self._retry_delay(attempt), remaining))
        return True

    def allocate_device_id(
        self, base_device_id: str, *, request_id: str | None = None
    ) -> str:
        if not isinstance(base_device_id, str) or not base_device_id.strip():
            raise SchedulerError("base device ID must be a non-empty string")
        allocation_request_id = request_id or uuid.uuid4().hex
        allocation_url = self.url.rsplit("/", 1)[0] + "/device-id/next"
        last_error = "device ID allocation failed"
        deadline = time.monotonic() + self.retry_window_seconds
        last_attempt = 0
        for attempt in range(self.max_retries + 1):
            if attempt > 0 and time.monotonic() >= deadline:
                break
            last_attempt = attempt
            retryable = True
            request_timeout = min(
                self.timeout_seconds,
                max(deadline - time.monotonic(), 0.001),
            )
            try:
                response = self.session.post(
                    allocation_url,
                    json={
                        "base_device_id": base_device_id.strip(),
                        "request_id": allocation_request_id,
                    },
                    timeout=request_timeout,
                )
                if not 200 <= response.status_code < 300:
                    last_error = _format_scheduler_error(response)
                    retryable = _is_retryable_response(response)
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        last_error = f"device ID response is not valid JSON: {exc}"
                        retryable = False
                        payload = None
                    device_id = payload.get("device_id") if isinstance(payload, dict) else None
                    if isinstance(device_id, str) and device_id.strip():
                        return device_id.strip()
                    if retryable:
                        last_error = "scheduler response device_id is missing"
                        retryable = False
            except requests.RequestException as exc:
                last_error = f"device ID allocation failed: {exc}"
                retryable = _is_retryable_request_exception(exc)

            if not retryable:
                raise SchedulerError(last_error, attempt)

            if not self._wait_before_retry(attempt, deadline):
                break

        raise SchedulerError(last_error, last_attempt)

    def assign(self, request: dict[str, Any]) -> ScheduleAssignment:
        string_fields = ("device_id", "sender_id", "task_id", "bearing_id")
        values = {field: request.get(field) for field in string_fields}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise SchedulerError("schedule request needs device_id, sender_id, task_id, and bearing_id")
        for field in ("packet_size_bytes", "expected_packet_count", "expected_duration_ms", "created_timestamp_ns"):
            value = request.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SchedulerError(f"schedule request {field} must be a positive integer")

        last_error = "scheduler request failed"
        deadline = time.monotonic() + self.retry_window_seconds
        last_attempt = 0
        for attempt in range(self.max_retries + 1):
            if attempt > 0 and time.monotonic() >= deadline:
                break
            last_attempt = attempt
            retryable = True
            request_timeout = min(
                self.timeout_seconds,
                max(deadline - time.monotonic(), 0.001),
            )
            try:
                response = self.session.post(self.url, json=request, timeout=request_timeout)
                if not 200 <= response.status_code < 300:
                    last_error = _format_scheduler_error(response)
                    retryable = _is_retryable_response(response)
                else:
                    try:
                        return validate_assignment(
                            response.json(),
                            expected_device_id=values["device_id"],
                            expected_sender_id=values["sender_id"],
                            expected_task_id=values["task_id"],
                            expected_bearing_id=values["bearing_id"],
                            retry_count=attempt,
                        )
                    except SchedulerError as exc:
                        last_error = str(exc)
                        retryable = False
            except requests.RequestException as exc:
                last_error = f"scheduler request failed: {exc}"
                retryable = _is_retryable_request_exception(exc)
            except ValueError as exc:
                last_error = f"scheduler response is not valid JSON: {exc}"
                retryable = False

            if not retryable:
                raise SchedulerError(last_error, attempt)

            if not self._wait_before_retry(attempt, deadline):
                break

        raise SchedulerError(last_error, last_attempt)


def _format_scheduler_error(response: Any) -> str:
    prefix = f"scheduler returned HTTP {response.status_code}"
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return prefix
    if not isinstance(payload, dict):
        return prefix

    summary: list[str] = []
    error_code = payload.get("error_code")
    message = payload.get("message")
    if isinstance(error_code, str) and error_code:
        summary.append(error_code)
    if isinstance(message, str) and message:
        summary.append(message)

    rejection_parts: list[str] = []
    rejections = payload.get("candidate_rejections")
    if isinstance(rejections, list):
        for rejection in rejections:
            if not isinstance(rejection, dict):
                continue
            edge_node_id = rejection.get("edge_node_id")
            reason_code = rejection.get("reason_code")
            if not isinstance(edge_node_id, str) or not isinstance(reason_code, str):
                continue
            item = f"{edge_node_id}={reason_code}"
            metrics = rejection.get("metrics")
            if isinstance(metrics, dict) and metrics:
                metric_text = ", ".join(
                    f"{key}={value}" for key, value in metrics.items()
                )
                item = f"{item} ({metric_text})"
            rejection_parts.append(item)
    if rejection_parts:
        summary.append("candidate_rejections: " + "; ".join(rejection_parts))
    return prefix if not summary else prefix + ": " + " - ".join(summary)


def _is_retryable_response(response: Any) -> bool:
    return response.status_code in {408, 425, 429, 500, 502, 503, 504}


def _is_retryable_request_exception(exc: requests.RequestException) -> bool:
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))
