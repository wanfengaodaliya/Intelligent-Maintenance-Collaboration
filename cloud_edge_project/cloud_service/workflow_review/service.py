from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.device_arbitration.service import DeviceArbitrationService
from cloud_service.workflow_review.repository import WorkflowReviewRepository
from core.bearing_workflow_contracts import (
    REVIEW_BEARING_WINDOW,
    REVIEW_DEVICE,
    REVIEW_PACKET,
)
from scenarios.bearing.cloud.device_arbitration.adapter import BearingDeviceArbitrationAdapter
from scenarios.bearing.cloud.workflow_review import (
    device_arbitration_request,
    review_packet,
    review_window,
    reviewed_device_result,
)


class WorkflowReviewError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class WorkflowReviewService:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.repository = WorkflowReviewRepository(self.database_path)

    def submit(self, review_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if review_type not in {REVIEW_PACKET, REVIEW_BEARING_WINDOW, REVIEW_DEVICE}:
            raise WorkflowReviewError("INVALID_REVIEW_TYPE")
        if not isinstance(payload, dict):
            raise WorkflowReviewError("INVALID_REVIEW_REQUEST")
        review_id = payload.get("review_id")
        if not isinstance(review_id, str) or not review_id.strip():
            raise WorkflowReviewError("INVALID_REVIEW_ID")
        self._validate_identity(payload)
        self._validate_payload(review_type, payload)
        status = "WAITING_RAW" if review_type == REVIEW_BEARING_WINDOW else "PENDING"
        return self.repository.create(review_id, review_type, payload, status)

    def upload_window_raw(self, review_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = self.get(review_id)
        if job["review_type"] != REVIEW_BEARING_WINDOW:
            raise WorkflowReviewError("INVALID_REVIEW_TYPE")
        packets = payload.get("raw_packets") if isinstance(payload, dict) else None
        if not isinstance(packets, list) or len(packets) != 20:
            raise WorkflowReviewError("WINDOW_REQUIRES_20_RAW_PACKETS")
        window = job["request"].get("window_result", {})
        expected = list(range(window.get("sequence_start", 0), window.get("sequence_end", -1) + 1))
        actual = [packet.get("sequence_number") for packet in packets if isinstance(packet, dict)]
        if actual != expected:
            raise WorkflowReviewError("WINDOW_RAW_SEQUENCE_MISMATCH")
        for packet in packets:
            if any(
                packet.get(field) != window.get(field)
                for field in ("device_id", "task_id", "bearing_id", "sender_id")
            ):
                raise WorkflowReviewError("WINDOW_RAW_IDENTITY_MISMATCH")
        return self.repository.store_raw(review_id, packets)

    def process(self, review_id: str) -> None:
        if not self.repository.mark_running(review_id):
            return
        job = self.get(review_id)
        try:
            if job["review_type"] == REVIEW_PACKET:
                result = review_packet(job["request"])
            elif job["review_type"] == REVIEW_BEARING_WINDOW:
                raw_packets = job["raw_packets"]
                if raw_packets is None:
                    raise WorkflowReviewError("WINDOW_RAW_NOT_UPLOADED")
                result = review_window(job["request"], raw_packets)
            else:
                device = job["request"]["device_result"]
                arbitration = DeviceArbitrationService(
                    self.database_path, BearingDeviceArbitrationAdapter()
                ).arbitrate(device_arbitration_request(device, review_id))
                result = reviewed_device_result(device, arbitration)
            self.repository.succeed(review_id, result)
        except Exception as exc:
            code = exc.code if isinstance(exc, WorkflowReviewError) else type(exc).__name__.upper()
            self.repository.fail(review_id, code)

    def get(self, review_id: str) -> dict[str, Any]:
        job = self.repository.get(review_id)
        if job is None:
            raise WorkflowReviewError("REVIEW_NOT_FOUND")
        return job

    def process_pending(self, limit: int = 20) -> int:
        review_ids = self.repository.pending_ids(limit)
        for review_id in review_ids:
            self.process(review_id)
        return len(review_ids)

    @staticmethod
    def _validate_identity(payload: dict[str, Any]) -> None:
        for field in ("device_id", "task_id"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise WorkflowReviewError("INVALID_REVIEW_REQUEST", "%s is required" % field)

    @staticmethod
    def _validate_payload(review_type: str, payload: dict[str, Any]) -> None:
        field = {
            REVIEW_PACKET: "packet_result",
            REVIEW_BEARING_WINDOW: "window_result",
            REVIEW_DEVICE: "device_result",
        }[review_type]
        result = payload.get(field)
        if not isinstance(result, dict):
            raise WorkflowReviewError("INVALID_REVIEW_REQUEST", "%s is required" % field)
        identity_fields = ("device_id", "task_id")
        if review_type != REVIEW_DEVICE:
            identity_fields += ("bearing_id",)
        if any(result.get(name) != payload.get(name) for name in identity_fields):
            raise WorkflowReviewError("REVIEW_IDENTITY_MISMATCH")
        if review_type == REVIEW_PACKET:
            raw = payload.get("raw_packet")
            if not isinstance(raw, dict) or any(
                raw.get(name) != result.get(name)
                for name in ("device_id", "task_id", "bearing_id", "sender_id", "packet_id", "sequence_number")
            ):
                raise WorkflowReviewError("PACKET_RAW_IDENTITY_MISMATCH")
