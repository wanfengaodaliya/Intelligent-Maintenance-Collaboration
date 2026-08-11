"""Background delivery of deferred device-arbitration controls."""
# 该模块在后台投递到期的设备级云端仲裁控制消息。

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

import requests

try:
    from .deferred_device_repository import DeferredDeviceArbitrationRepository
except ImportError:
    from deferred_device_repository import DeferredDeviceArbitrationRepository


class SummaryDispatchClient:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self.timeout_seconds = timeout_seconds

    def dispatch(self, base_url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = requests.post(
            base_url.rstrip("/") + "/summary/cloud-device-arbitration-tasks",
            json=dict(payload),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("summary cloud-arbitration response must be an object")
        return body


class DeferredDeviceArbitrationDispatcher:
    def __init__(
        self,
        repository: DeferredDeviceArbitrationRepository,
        *,
        summary_url_lookup: Callable[[str], str],
        client: SummaryDispatchClient | Any | None = None,
        eligibility_check: Callable[
            [Mapping[str, Any], int], tuple[bool, str | None]
        ] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.repository = repository
        self.summary_url_lookup = summary_url_lookup
        self.client = client or SummaryDispatchClient()
        self.eligibility_check = eligibility_check
        self.clock_ns = clock_ns
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def dispatch_once(self, *, now_ns: int | None = None) -> dict[str, Any] | None:
        now = self.clock_ns() if now_ns is None else now_ns
        task = self.repository.claim_due(now_ns=now)
        if task is None:
            return None
        if self.eligibility_check is not None:
            ready, reason_code = self.eligibility_check(task, now)
            if not ready:
                return self.repository.schedule_retry(
                    task["decision_id"],
                    reason_code=reason_code or "DEVICE_CLOUD_ROUTE_NOT_READY",
                    now_ns=now,
                )
        payload = {
            "decision_id": task["decision_id"],
            "cloud_task_id": task["cloud_task_id"],
            "device_id": task["device_id"],
            "task_id": task["task_id"],
            "trigger_reasons": task["reason_codes"],
            "source": {
                "holder_id": task["summary_module_id"],
                "bearing_results_ref": task["bearing_results_ref"],
                "provisional_result_ref": task["provisional_result_ref"],
            },
            "target": {
                "cloud_node_id": task["cloud_node_id"],
                "endpoint": task["endpoint"],
            },
            "created_at_ns": task["created_at_ns"],
        }
        try:
            self.client.dispatch(
                self.summary_url_lookup(task["summary_module_id"]),
                payload,
            )
        except Exception as error:
            return self.repository.schedule_retry(
                task["decision_id"],
                reason_code=_dispatch_reason(error),
                now_ns=now,
            )
        return self.repository.mark_dispatched(task["decision_id"], now_ns=now)

    def start(self, interval_seconds: float = 1.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.repository.recover_non_terminal()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(interval_seconds,),
            name="scheduler-deferred-device-arbitration-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self, interval_seconds: float) -> None:
        while not self._stop.wait(interval_seconds):
            self.dispatch_once()


def _dispatch_reason(error: Exception) -> str:
    if isinstance(error, requests.Timeout):
        return "SUMMARY_DISPATCH_TIMEOUT"
    if isinstance(error, requests.RequestException):
        return "SUMMARY_UNREACHABLE"
    return "SUMMARY_DISPATCH_FAILED"
