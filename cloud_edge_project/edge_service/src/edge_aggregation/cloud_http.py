"""HTTP implementation of the cloud review gateway with bounded polling."""

from __future__ import annotations

import time
import json
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

import numpy as np

from core.bearing_workflow_contracts import (
    FINAL_CLOUD,
    REVIEW_SUCCEEDED,
    BearingWindowResult,
    DeviceTaskResult,
    FinalPacketResult,
)
class HttpCloudReviewGateway:
    def __init__(
        self,
        base_url: str,
        *,
        request_timeout_seconds: float = 5.0,
        review_timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.1,
    ):
        self.client = _JsonHttpClient(base_url, timeout_seconds=request_timeout_seconds)
        self.review_timeout_seconds = review_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def review_packet(
        self, packet: FinalPacketResult, raw_packet: dict[str, Any]
    ) -> FinalPacketResult:
        review_id = "packet_review_%s" % packet.result_id
        self.client.post("/cloud/packet-reviews", {
            "review_id": review_id,
            "device_id": packet.device_id,
            "task_id": packet.task_id,
            "bearing_id": packet.bearing_id,
            "packet_result": packet.as_dict(),
            "raw_packet": raw_packet,
        })
        result = self._wait("/cloud/packet-reviews/%s" % review_id)
        return replace(
            packet,
            result_id=result["result_id"],
            action_grade=int(result["action_grade"]),
            confidence=float(result["confidence"]),
            data_quality_score=float(result["data_quality_score"]),
            risk_level=result["risk_level"],
            decision_source=FINAL_CLOUD,
        )

    def review_bearing_window(
        self, window: BearingWindowResult, raw_packets: list[dict[str, Any]]
    ) -> BearingWindowResult:
        review_id = "window_review_%s" % window.result_id
        self.client.post("/cloud/bearing-window-reviews", {
            "review_id": review_id,
            "device_id": window.device_id,
            "task_id": window.task_id,
            "bearing_id": window.bearing_id,
            "window_result": window.as_dict(),
        })
        self.client.post(
            "/cloud/bearing-window-reviews/%s/raw-batch" % review_id,
            {"raw_packets": raw_packets},
        )
        result = self._wait("/cloud/bearing-window-reviews/%s" % review_id)
        return replace(
            window,
            result_id=result["result_id"],
            action_grade=int(result["action_grade"]),
            confidence=float(result["confidence"]),
            data_quality_score=float(result["data_quality_score"]),
            result_source=FINAL_CLOUD,
            review_status=REVIEW_SUCCEEDED,
            review_required=False,
            review_reasons=(),
        )

    def review_device(self, result: DeviceTaskResult) -> DeviceTaskResult:
        review_id = "device_review_%s" % result.result_id
        self.client.post("/cloud/device-reviews", {
            "review_id": review_id,
            "device_id": result.device_id,
            "task_id": result.task_id,
            "device_result": result.as_dict(),
        })
        reviewed = self._wait("/cloud/device-reviews/%s" % review_id)
        return replace(
            result,
            status=reviewed["status"],
            action_grade=int(reviewed["action_grade"]),
            conflict=bool(reviewed["conflict"]),
            decision_source="CLOUD",
            final_report=reviewed.get("final_report"),
        )

    def _wait(self, path: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.review_timeout_seconds
        while time.monotonic() < deadline:
            job = self.client.get(path)
            if job.get("status") == "SUCCEEDED":
                result = job.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("cloud review succeeded without a result")
                return result
            if job.get("status") == "FAILED":
                raise RuntimeError("cloud review failed: %s" % job.get("error_code"))
            time.sleep(self.poll_interval_seconds)
        raise TimeoutError("cloud review timed out")


class _JsonHttpClient:
    def __init__(self, base_url: str, *, timeout_seconds: float):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(path, "POST", payload)

    def get(self, path: str) -> dict[str, Any]:
        return self._request(path, "GET", None)

    def _request(
        self, path: str, method: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(
                _json_value(payload), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("cloud HTTP %d: %s" % (exc.code, detail)) from exc
        if not isinstance(result, dict):
            raise RuntimeError("cloud response must be an object")
        return result


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
