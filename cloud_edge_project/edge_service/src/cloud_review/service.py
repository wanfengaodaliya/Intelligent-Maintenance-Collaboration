"""Upload persisted edge records through the existing cloud API and report outcomes."""
# 该模块负责上传已持久化的边缘记录并回报云端复核结果。

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

import requests

from .contracts import CloudReviewError, parse_cloud_bearing_result, validate_control
from .store import CloudReviewStore


class CloudUploadError(RuntimeError):
    def __init__(self, reason_code: str, *, retryable: bool) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable


class HttpCloudClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def infer(self, cloud_node_id: str, endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        del cloud_node_id
        try:
            response = requests.post(
                self.base_url + endpoint,
                json=dict(payload),
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as error:
            raise CloudUploadError("CLOUD_TIMEOUT", retryable=True) from error
        except requests.RequestException as error:
            raise CloudUploadError("CLOUD_UNREACHABLE", retryable=True) from error
        if response.status_code >= 500:
            raise CloudUploadError("CLOUD_SERVER_ERROR", retryable=True)
        if response.status_code >= 400:
            raise CloudUploadError("INVALID_CLOUD_REQUEST", retryable=False)
        try:
            body = response.json()
        except ValueError as error:
            raise CloudUploadError("INVALID_CLOUD_RESPONSE", retryable=False) from error
        if not isinstance(body, dict):
            raise CloudUploadError("INVALID_CLOUD_RESPONSE", retryable=False)
        return body


class SchedulerUploadReporter:
    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def report(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = requests.post(
            self.base_url + "/scheduler/cloud-upload-results",
            json=dict(payload),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("scheduler upload-result response must be an object")
        return body


class CloudReviewService:
    def __init__(
        self,
        store: CloudReviewStore,
        *,
        cloud_client: HttpCloudClient | Any,
        scheduler_reporter: SchedulerUploadReporter | Any,
        edge_node_id: str,
        result_lifecycle: Any | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.store = store
        self.cloud_client = cloud_client
        self.scheduler_reporter = scheduler_reporter
        self.edge_node_id = edge_node_id
        self.result_lifecycle = result_lifecycle
        self.clock_ns = clock_ns
        self._decision_locks_guard = threading.Lock()
        self._decision_locks: dict[str, threading.Lock] = {}

    def handle(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        control = validate_control(payload)
        lock = self._decision_lock(control["decision_id"])
        with lock:
            return self._handle_control(control)

    def _handle_control(self, control: Mapping[str, Any]) -> dict[str, Any]:
        checkpoint = self.store.get_decision(control["decision_id"])
        if checkpoint is not None:
            if checkpoint["control"] != control:
                raise CloudReviewError("CLOUD_REVIEW_DECISION_CONFLICT", "decision_id has different control data", 409)
            if checkpoint["phase"] == "COMPLETED":
                response = dict(checkpoint["response"])
                response["duplicate"] = True
                return response

        self._validate_source(control)
        record = self.store.get(control["task_id"], control["bearing_id"], control["packet_id"])
        if record is None:
            raise CloudReviewError("RAW_PACKET_NOT_AVAILABLE", "persisted raw packet was not found", 409)

        review_id = checkpoint.get("review_id") if checkpoint is not None else None
        if checkpoint is None or checkpoint["phase"] != "CLOUD_SUCCEEDED":
            cloud_request = {
                "schema_version": "cloud-infer/2.0",
                "decision_round_id": control["decision_round_id"],
                "diagnosis_window_id": control["diagnosis_window_id"],
                "edge_perception_result": record["edge_perception_result"],
                "cloud_raw_window": _cloud_raw_window(control, record["raw_packet"]),
            }
            try:
                cloud_response = self.cloud_client.infer(
                    control["target"]["cloud_node_id"],
                    control["target"]["endpoint"],
                    cloud_request,
                )
                if cloud_response.get("success") is not True or not isinstance(cloud_response.get("review_id"), str) or not cloud_response["review_id"].strip():
                    raise CloudUploadError("INVALID_CLOUD_RESPONSE", retryable=False)
                review_id = cloud_response["review_id"].strip()
                cloud_payload = cloud_response.get("cloud_packet_result")
                if not isinstance(cloud_payload, Mapping):
                    raise CloudUploadError("INVALID_CLOUD_RESPONSE", retryable=False)
                cloud_result = parse_cloud_bearing_result(cloud_payload)
                if cloud_result.review_id != review_id:
                    raise CloudUploadError("INVALID_CLOUD_RESPONSE", retryable=False)
                if self.result_lifecycle is not None:
                    self.result_lifecycle.apply_cloud_result(
                        cloud_result, accepted_at_ns=self.clock_ns()
                    )
                self.store.save_decision(control, phase="CLOUD_SUCCEEDED", review_id=review_id)
            except CloudUploadError as error:
                return self._report_failure(control, error)

        report = self._result_report(
            control,
            upload_status="SUCCESS",
            review_id=review_id,
            reason_code=None,
        )
        self.scheduler_reporter.report(report)
        self.store.release(control["task_id"], control["bearing_id"], control["packet_id"])
        response = self._response(control, "SUCCESS", review_id)
        self.store.save_decision(
            control,
            phase="COMPLETED",
            review_id=review_id,
            response=response,
        )
        return response


    def _decision_lock(self, decision_id: str) -> threading.Lock:
        with self._decision_locks_guard:
            return self._decision_locks.setdefault(decision_id, threading.Lock())

    def _report_failure(self, control: Mapping[str, Any], error: CloudUploadError) -> dict[str, Any]:
        status = "RETRYABLE_FAILED" if error.retryable else "PERMANENT_FAILED"
        report = self._result_report(
            control,
            upload_status=status,
            review_id=None,
            reason_code=error.reason_code,
        )
        self.scheduler_reporter.report(report)
        response = self._response(control, status, None, reason_code=error.reason_code)
        if not error.retryable:
            self.store.release(control["task_id"], control["bearing_id"], control["packet_id"])
            self.store.save_decision(control, phase="COMPLETED", response=response)
        return response

    def _validate_source(self, control: Mapping[str, Any]) -> None:
        if control["source"]["holder_id"] != self.edge_node_id:
            raise CloudReviewError("CLOUD_REVIEW_HOLDER_MISMATCH", "control targets another edge node", 409)
        expected = (
            f"edge-cache://{self.edge_node_id}/{control['task_id']}/"
            f"{control['bearing_id']}/{control['packet_id']}"
        )
        if control["source"]["raw_data_ref"] != expected:
            raise CloudReviewError("CLOUD_REVIEW_RAW_REF_MISMATCH", "raw_data_ref does not match packet identity", 409)

    def _result_report(
        self,
        control: Mapping[str, Any],
        *,
        upload_status: str,
        review_id: str | None,
        reason_code: str | None,
    ) -> dict[str, Any]:
        return {
            "decision_id": control["decision_id"],
            "cloud_task_id": control["cloud_task_id"],
            "device_id": control["device_id"],
            "task_id": control["task_id"],
            "bearing_id": control["bearing_id"],
            "packet_id": control["packet_id"],
            "edge_node_id": self.edge_node_id,
            "upload_status": upload_status,
            "review_id": review_id,
            "reason_code": reason_code,
            "reported_at_ns": self.clock_ns(),
        }

    @staticmethod
    def _response(
        control: Mapping[str, Any],
        upload_status: str,
        review_id: str | None,
        *,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        response = {
            "accepted": True,
            "duplicate": False,
            "decision_id": control["decision_id"],
            "cloud_task_id": control["cloud_task_id"],
            "upload_status": upload_status,
            "review_id": review_id,
        }
        if reason_code is not None:
            response["reason_code"] = reason_code
        return response


def _cloud_raw_window(control: Mapping[str, Any], raw_packet: Mapping[str, Any]) -> dict[str, Any]:
    """Represent a compatible 50ms stored packet as the V1.2 raw-window form."""
    start_ns = raw_packet.get("start_timestamp_ns", 0)
    end_ns = raw_packet.get("end_timestamp_ns") or raw_packet.get("end_generate_timestamp_ns")
    if not isinstance(start_ns, int) or start_ns < 0 or not isinstance(end_ns, int) or end_ns <= start_ns:
        raise CloudReviewError("INVALID_CLOUD_REVIEW_RECORD", "raw packet lacks a valid time range", 409)
    data = raw_packet.get("data")
    if not isinstance(data, Mapping):
        raise CloudReviewError("INVALID_CLOUD_REVIEW_RECORD", "raw packet lacks signal data", 409)
    return {
        "device_id": control["device_id"],
        "task_id": control["task_id"],
        "bearing_id": control["bearing_id"],
        "sender_id": raw_packet.get("sender_id"),
        "window_start_sequence": control["window_start_sequence"],
        "window_end_sequence": control["window_end_sequence"],
        "window_start_ns": start_ns,
        "window_end_ns": end_ns,
        "contributing_packet_ids": [control["packet_id"]],
        "sample_rate_hz": raw_packet.get("sample_rate_hz", 64_000),
        "sample_count": raw_packet.get("sample_count", 3_200),
        "data": dict(data),
    }
