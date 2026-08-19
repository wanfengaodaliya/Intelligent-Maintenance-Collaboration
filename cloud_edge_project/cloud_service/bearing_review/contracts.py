"""Contracts for a manifest-defined bearing review."""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any


EXPECTED_PACKET_COUNT = 20


class BearingReviewValidationError(ValueError):
    def __init__(self, code: str = "INVALID_BEARING_REVIEW"):
        super().__init__(code)
        self.code = code


class BearingReviewConflictError(ValueError):
    def __init__(self, code: str = "BEARING_REVIEW_MANIFEST_CONFLICT"):
        super().__init__(code)
        self.code = code


def validate_bearing_review_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BearingReviewValidationError("INVALID_BEARING_REVIEW")
    if payload.get("scenario_type", "bearing") != "bearing":
        raise BearingReviewValidationError("UNSUPPORTED_SCENARIO")
    result: dict[str, Any] = {}
    for field in ("device_id", "task_id", "bearing_id", "sender_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise BearingReviewValidationError("INVALID_BEARING_REVIEW")
        result[field] = value
    edge_node_id = payload.get("edge_node_id")
    if edge_node_id is not None:
        if not isinstance(edge_node_id, str) or not edge_node_id.strip():
            raise BearingReviewValidationError("INVALID_BEARING_REVIEW")
        result["edge_node_id"] = edge_node_id.strip()
    edge = payload.get("edge_bearing_result")
    if not isinstance(edge, dict) or edge.get("bearing_state") not in {"normal", "warning", "fault"}:
        raise BearingReviewValidationError("INVALID_EDGE_BEARING_RESULT")
    confidence = edge.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not isfinite(float(confidence)) or not 0 <= confidence <= 1:
        raise BearingReviewValidationError("INVALID_EDGE_BEARING_RESULT")
    if edge.get("packet_count") != EXPECTED_PACKET_COUNT:
        raise BearingReviewValidationError("INVALID_EDGE_BEARING_RESULT")
    manifest = payload.get("source_packet_manifest")
    if not isinstance(manifest, list) or len(manifest) != EXPECTED_PACKET_COUNT:
        raise BearingReviewValidationError("INVALID_SOURCE_PACKET_MANIFEST")
    packet_ids: set[str] = set()
    sequences: set[int] = set()
    normalized: list[dict[str, Any]] = []
    for item in manifest:
        if not isinstance(item, dict):
            raise BearingReviewValidationError("INVALID_SOURCE_PACKET_MANIFEST")
        packet_id = item.get("packet_id")
        sequence_number = item.get("sequence_number")
        if not isinstance(packet_id, str) or not packet_id.strip() or not isinstance(sequence_number, int) or isinstance(sequence_number, bool) or sequence_number <= 0:
            raise BearingReviewValidationError("INVALID_SOURCE_PACKET_MANIFEST")
        if packet_id in packet_ids or sequence_number in sequences:
            raise BearingReviewValidationError("INVALID_SOURCE_PACKET_MANIFEST")
        packet_ids.add(packet_id)
        sequences.add(sequence_number)
        normalized.append({"packet_id": packet_id, "sequence_number": sequence_number})
    normalized.sort(key=lambda item: item["sequence_number"])
    sequence_start = normalized[0]["sequence_number"]
    expected_sequences = list(range(sequence_start, sequence_start + EXPECTED_PACKET_COUNT))
    if sequence_start not in {1, 21, 41, 61} or [
        item["sequence_number"] for item in normalized
    ] != expected_sequences:
        raise BearingReviewValidationError("INVALID_SOURCE_PACKET_MANIFEST")
    result["window_index"] = (sequence_start - 1) // EXPECTED_PACKET_COUNT + 1
    result["edge_state"] = edge["bearing_state"]
    result["edge_confidence"] = float(confidence)
    result["source_packet_manifest"] = normalized
    result["packet_manifest_sha256"] = sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
