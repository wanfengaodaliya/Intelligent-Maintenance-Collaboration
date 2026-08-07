from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests


class SchedulerError(RuntimeError):
    def __init__(self, message: str, retry_count: int = 0) -> None:
        super().__init__(message)
        self.retry_count = retry_count


@dataclass(frozen=True)
class BearingAssignment:
    bearing_id: str
    target_topic: str


@dataclass(frozen=True)
class ScheduleAssignment:
    device_id: str
    task_id: str
    assignments: tuple[BearingAssignment, ...]
    schedule_retry_count: int

    def topic_for(self, bearing_id: str) -> str:
        for assignment in self.assignments:
            if assignment.bearing_id == bearing_id:
                return assignment.target_topic
        raise SchedulerError(f"bearing has no assignment: {bearing_id}")


def validate_assignment(
    payload: dict[str, Any],
    *,
    expected_device_id: str,
    expected_task_id: str,
    expected_bearing_ids: set[str],
    retry_count: int = 0,
) -> ScheduleAssignment:
    if not isinstance(payload, dict):
        raise SchedulerError("scheduler response must be a JSON object", retry_count)
    if payload.get("device_id") != expected_device_id:
        raise SchedulerError("scheduler response device_id does not match request", retry_count)
    if payload.get("task_id") != expected_task_id:
        raise SchedulerError("scheduler response task_id does not match request", retry_count)

    raw_assignments = payload.get("assignments")
    if not isinstance(raw_assignments, list):
        raise SchedulerError("scheduler response assignments must be an array", retry_count)

    assignments: list[BearingAssignment] = []
    seen: set[str] = set()
    for item in raw_assignments:
        if not isinstance(item, dict):
            raise SchedulerError("scheduler assignment must be an object", retry_count)
        bearing_id = item.get("bearing_id")
        target_topic = item.get("target_topic")
        if not isinstance(bearing_id, str) or not bearing_id.strip():
            raise SchedulerError("scheduler assignment bearing_id is missing", retry_count)
        if bearing_id in seen:
            raise SchedulerError(f"duplicate bearing assignment: {bearing_id}", retry_count)
        if not isinstance(target_topic, str) or not target_topic.strip():
            raise SchedulerError(f"target_topic is missing for {bearing_id}", retry_count)
        seen.add(bearing_id)
        assignments.append(BearingAssignment(bearing_id, target_topic.strip()))

    if seen != expected_bearing_ids:
        raise SchedulerError("scheduler assignments do not match requested bearings", retry_count)

    return ScheduleAssignment(
        device_id=expected_device_id,
        task_id=expected_task_id,
        assignments=tuple(assignments),
        schedule_retry_count=retry_count,
    )


class SchedulerClient:
    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_retries: int,
        retry_delay_seconds: float = 0.2,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.session = session or requests.Session()

    def assign(self, request: dict[str, Any]) -> ScheduleAssignment:
        device_id = request.get("device_id")
        task_id = request.get("task_id")
        sender_id = request.get("sender_id")
        raw_bearings = request.get("bearings")
        if not all(isinstance(value, str) and value for value in (device_id, task_id, sender_id)):
            raise SchedulerError("schedule request needs device_id, task_id, and sender_id")
        if not isinstance(raw_bearings, list) or not raw_bearings:
            raise SchedulerError("schedule request needs bearings")
        bearing_ids = [item.get("bearing_id") for item in raw_bearings if isinstance(item, dict)]
        if len(bearing_ids) != len(raw_bearings) or not all(
            isinstance(bearing_id, str) and bearing_id for bearing_id in bearing_ids
        ):
            raise SchedulerError("schedule request has invalid bearings")
        expected_bearing_ids = set(bearing_ids)
        if len(expected_bearing_ids) != len(bearing_ids):
            raise SchedulerError("schedule request has duplicate bearings")

        last_error = "scheduler request failed"
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    self.url,
                    json=request,
                    timeout=self.timeout_seconds,
                )
                if not 200 <= response.status_code < 300:
                    last_error = f"scheduler returned HTTP {response.status_code}"
                else:
                    try:
                        return validate_assignment(
                            response.json(),
                            expected_device_id=device_id,
                            expected_task_id=task_id,
                            expected_bearing_ids=expected_bearing_ids,
                            retry_count=attempt,
                        )
                    except SchedulerError as exc:
                        last_error = str(exc)
            except (requests.RequestException, ValueError) as exc:
                last_error = f"scheduler request failed: {exc}"

            if attempt < self.max_retries and self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds)

        raise SchedulerError(last_error, self.max_retries)
