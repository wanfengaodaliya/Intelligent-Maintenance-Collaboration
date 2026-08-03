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
class ScheduleAssignment:
    task_id: str
    sender_id: str
    target_topic: str
    schedule_retry_count: int


def validate_assignment(
    payload: dict[str, Any],
    *,
    expected_task_id: str,
    expected_sender_id: str,
    retry_count: int = 0,
) -> ScheduleAssignment:
    if not isinstance(payload, dict):
        raise SchedulerError("scheduler response must be a JSON object", retry_count)
    if payload.get("task_id") != expected_task_id:
        raise SchedulerError("scheduler response task_id does not match request", retry_count)
    if payload.get("sender_id") != expected_sender_id:
        raise SchedulerError("scheduler response sender_id does not match request", retry_count)
    target_topic = payload.get("target_topic")
    if not isinstance(target_topic, str) or not target_topic.strip():
        raise SchedulerError("scheduler response target_topic is missing", retry_count)
    return ScheduleAssignment(
        task_id=expected_task_id,
        sender_id=expected_sender_id,
        target_topic=target_topic.strip(),
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
        task_id = request.get("task_id")
        sender_id = request.get("sender_id")
        if not isinstance(task_id, str) or not isinstance(sender_id, str):
            raise SchedulerError("schedule request needs task_id and sender_id")

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
                            expected_task_id=task_id,
                            expected_sender_id=sender_id,
                            retry_count=attempt,
                        )
                    except SchedulerError as exc:
                        last_error = str(exc)
            except (requests.RequestException, ValueError) as exc:
                last_error = f"scheduler request failed: {exc}"

            if attempt < self.max_retries and self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds)

        raise SchedulerError(last_error, self.max_retries)
