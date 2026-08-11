"""Complete a manifest-defined bearing review without invoking an LLM."""

from __future__ import annotations

from math import sqrt
from typing import Any

from .contracts import EXPECTED_PACKET_COUNT
from .repository import BearingReviewRepository


class BearingReviewProcessor:
    """Aggregate 20 persisted high-rate packets and issue a structured diagnosis."""

    model_version = "cloud_bearing_model_v1"

    def __init__(self, repository: BearingReviewRepository):
        self.repository = repository

    def process(self, bearing_review_id: str) -> dict[str, Any]:
        review = self.repository.get(bearing_review_id)
        if review is None:
            raise ValueError("BEARING_REVIEW_NOT_FOUND")
        packets = self.repository.context_packets(bearing_review_id)
        if len(packets) != EXPECTED_PACKET_COUNT:
            raise ValueError("BEARING_REVIEW_INCOMPLETE")
        aggregation = _aggregate(packets)
        diagnosis = _diagnose(aggregation["enhanced_features"])
        result = {
            "bearing_review_id": bearing_review_id,
            "device_id": review["device_id"],
            "task_id": review["task_id"],
            "bearing_id": review["bearing_id"],
            "edge_state": review["edge_state"],
            "edge_confidence": review["edge_confidence"],
            "cloud_state": diagnosis["state"],
            "cloud_confidence": diagnosis["confidence"],
            "review_packet_count": EXPECTED_PACKET_COUNT,
            "result_source": "cloud_bearing_review",
            "model_version": self.model_version,
            "aggregation": aggregation,
        }
        self.repository.complete(bearing_review_id, aggregation=aggregation, result=result)
        return result


def _aggregate(packets: list[dict[str, Any]]) -> dict[str, Any]:
    packet_rms: list[float] = []
    peak = 0.0
    sample_count = 0
    manifest: list[dict[str, Any]] = []
    for packet in packets:
        values = [float(value) for value in packet["data"]["vibration"]["values"]]
        packet_rms.append(sqrt(sum(value * value for value in values) / len(values)))
        peak = max(peak, max(abs(value) for value in values))
        sample_count += len(values)
        manifest.append({"packet_id": packet["packet_id"], "sequence_number": packet["sequence_number"]})
    mean_rms = sum(packet_rms) / len(packet_rms)
    variance = sum((value - mean_rms) ** 2 for value in packet_rms) / len(packet_rms)
    return {
        "packet_manifest": manifest,
        "sample_rate_hz": 64_000,
        "total_sample_count": sample_count,
        "enhanced_features": {
            "vibration_rms_mean": mean_rms,
            "vibration_rms_std": sqrt(variance),
            "vibration_peak": peak,
        },
    }


def _diagnose(features: dict[str, float]) -> dict[str, Any]:
    if features["vibration_peak"] >= 4.0 or features["vibration_rms_mean"] >= 2.0:
        return {"state": "abnormal", "confidence": 0.9}
    if features["vibration_peak"] >= 2.0 or features["vibration_rms_mean"] >= 1.0:
        return {"state": "warning", "confidence": 0.78}
    return {"state": "normal", "confidence": 0.92}
