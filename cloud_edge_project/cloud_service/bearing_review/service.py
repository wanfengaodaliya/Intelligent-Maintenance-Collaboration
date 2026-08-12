"""Create a non-blocking review for one exact 20-packet bearing manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    BearingReviewConflictError,
    BearingReviewValidationError,
    EXPECTED_PACKET_COUNT,
    validate_bearing_review_request,
)
from .repository import BearingReviewRepository


class RawContextTransport(Protocol):
    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        ...


class BearingReviewService:
    def __init__(self, database_path: Path, *, transport: RawContextTransport):
        self.repository = BearingReviewRepository(database_path)
        self.transport = transport

    def create(self, payload: Any) -> dict[str, Any]:
        request = validate_bearing_review_request(payload)
        stored, created = self.repository.create_or_get(request)
        if created:
            try:
                self.transport.send(
                    _raw_context_request(
                        stored,
                        edge_node_id=request.get("edge_node_id"),
                    )
                )
            except Exception:
                self.repository.mark_dispatch_failed(stored["bearing_review_id"], "EDGE_UNAVAILABLE")
            else:
                self.repository.mark_dispatched(stored["bearing_review_id"])
        return _response(stored)

    def get(self, bearing_review_id: str) -> dict[str, Any] | None:
        stored = self.repository.get(bearing_review_id)
        if stored is None:
            return None
        return _response(stored, self.repository.progress(bearing_review_id))


def _raw_context_request(
    stored: dict[str, Any],
    *,
    edge_node_id: str | None = None,
) -> dict[str, Any]:
    request = {
        "request_id": stored["raw_context_request_id"],
        "review_type": "bearing_review",
        "device_id": stored["device_id"],
        "task_id": stored["task_id"],
        "bearing_id": stored["bearing_id"],
        "window_index": stored["window_index"],
        "sender_id": stored["sender_id"],
        "expected_packet_count": EXPECTED_PACKET_COUNT,
        "requested_packets": json.loads(stored["packet_manifest_json"]),
    }
    if edge_node_id is not None:
        request["edge_node_id"] = edge_node_id
    return request


def _response(stored: dict[str, Any], progress: tuple[int, int] | None = None) -> dict[str, Any]:
    result = {
        "bearing_review_id": stored["bearing_review_id"],
        "window_index": stored["window_index"],
        "status": stored["status"],
        "raw_context_request_id": stored["raw_context_request_id"],
    }
    if progress is not None:
        result["received_packet_count"], result["expected_packet_count"] = progress
    if stored["result_json"] is not None:
        result["cloud_bearing_result"] = json.loads(stored["result_json"])
    if stored["error_code"] is not None:
        result["error_code"] = stored["error_code"]
    return result
