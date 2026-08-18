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


class CloudArbitrationDispatchClient:
    def __init__(
        self,
        *,
        endpoint: str = "/cloud/device-arbitration",
        timeout_seconds: float = 3.0,
    ) -> None:
        if not endpoint.startswith("/"):
            raise ValueError("cloud arbitration endpoint must start with '/'")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def dispatch(self, base_url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = requests.post(
            base_url.rstrip("/") + self.endpoint,
            json=dict(payload),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("cloud arbitration response must be an object")
        return body


# Kept for integrations that imported the old dispatcher client name.
SummaryDispatchClient = CloudArbitrationDispatchClient


class EdgeArbitrationResultClient:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self.timeout_seconds = timeout_seconds

    def deliver(self, base_url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = requests.post(
            base_url.rstrip("/") + "/edge/device-arbitration-results",
            json=dict(payload),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("edge arbitration response must be an object")
        return body


class DeferredDeviceArbitrationDispatcher:
    def __init__(
        self,
        repository: DeferredDeviceArbitrationRepository,
        *,
        cloud_url_lookup: Callable[[str], str] | None = None,
        summary_url_lookup: Callable[[str], str] | None = None,
        client: CloudArbitrationDispatchClient | Any | None = None,
        edge_url_lookup: Callable[[str], str] | None = None,
        edge_result_client: EdgeArbitrationResultClient | Any | None = None,
        eligibility_check: Callable[
            [Mapping[str, Any], int], tuple[bool, str | None]
        ] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.repository = repository
        self.cloud_url_lookup = cloud_url_lookup or summary_url_lookup
        if self.cloud_url_lookup is None:
            raise ValueError("cloud_url_lookup is required")
        self.client = client or SummaryDispatchClient()
        self.edge_url_lookup = edge_url_lookup
        self.edge_result_client = edge_result_client or EdgeArbitrationResultClient()
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
        payload = _cloud_arbitration_payload(task)
        try:
            response = self.client.dispatch(
                self.cloud_url_lookup(task["cloud_node_id"]),
                payload,
            )
        except Exception as error:
            return self.repository.schedule_retry(
                task["decision_id"],
                reason_code=_dispatch_reason(error),
                now_ns=now,
            )
        if "decision_round_id" in task and isinstance(response, Mapping):
            arbitration_id = response.get("arbitration_id")
            if isinstance(arbitration_id, str) and arbitration_id.strip():
                if task["edge_node_id"] is not None:
                    if self.edge_url_lookup is None:
                        return self.repository.schedule_retry(
                            task["decision_id"],
                            reason_code="EDGE_CALLBACK_NOT_CONFIGURED",
                            now_ns=now,
                        )
                    try:
                        self.edge_result_client.deliver(
                            self.edge_url_lookup(task["edge_node_id"]), response
                        )
                    except Exception as error:
                        return self.repository.schedule_retry(
                            task["decision_id"],
                            reason_code=_edge_callback_reason(error),
                            now_ns=now,
                        )
                return self.repository.save_arbitration_result(
                    {
                        "decision_id": task["decision_id"],
                        "cloud_task_id": task["cloud_task_id"],
                        "device_id": task["device_id"],
                        "task_id": task["task_id"],
                        "summary_module_id": task["summary_module_id"],
                        "arbitration_status": "SUCCESS",
                        "arbitration_id": arbitration_id,
                        "reason_code": None,
                        "reported_at_ns": now,
                    }
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
        return "CLOUD_ARBITRATION_DISPATCH_TIMEOUT"
    if isinstance(error, requests.RequestException):
        return "CLOUD_ARBITRATION_UNREACHABLE"
    return "CLOUD_ARBITRATION_DISPATCH_FAILED"


def _edge_callback_reason(error: Exception) -> str:
    if isinstance(error, requests.Timeout):
        return "EDGE_ARBITRATION_CALLBACK_TIMEOUT"
    if isinstance(error, requests.RequestException):
        return "EDGE_ARBITRATION_CALLBACK_UNREACHABLE"
    return "EDGE_ARBITRATION_CALLBACK_FAILED"


def _cloud_arbitration_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    """Build the V1.2 cloud contract from a persisted deferred task."""

    if "decision_round_id" not in task:
        return {
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
    return {
        "conflict_id": task["conflict_id"],
        "device_id": task["device_id"],
        "task_id": task["task_id"],
        "decision_round_id": task["decision_round_id"],
        "device_result_revision": task["device_result_revision"],
        "bearing_result_ids": task["bearing_result_ids"],
        "bearing_results": task["bearing_results"],
        "comparison": task["comparison"],
        "local_arbitration_supported": task["local_arbitration_supported"],
    }
