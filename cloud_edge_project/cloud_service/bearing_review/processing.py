"""Complete a manifest-defined bearing review without invoking an LLM."""

from __future__ import annotations

from math import sqrt
from typing import Any

from .contracts import EXPECTED_PACKET_COUNT
from .repository import BearingReviewRepository
from .enhanced_bridge import BearingWindowEnhancedBridge


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
        try:
            enhanced = BearingWindowEnhancedBridge(
                self.repository.database_path
            ).analyze(review, packets)
        except Exception as error:
            self.repository.fail(bearing_review_id, type(error).__name__.upper())
            raise
        model = enhanced["model_evidence"]
        cloud_state = model.get("label") or "normal"
        cloud_confidence = model.get("probability")
        if cloud_confidence is None:
            cloud_confidence = 0.5
        result = {
            "bearing_review_id": bearing_review_id,
            "device_id": review["device_id"],
            "task_id": review["task_id"],
            "bearing_id": review["bearing_id"],
            "window_index": review["window_index"],
            "edge_state": review["edge_state"],
            "edge_confidence": review["edge_confidence"],
            "cloud_state": cloud_state,
            "cloud_confidence": cloud_confidence,
            "review_packet_count": EXPECTED_PACKET_COUNT,
            "result_source": "cloud_bearing_review",
            "model_version": model.get("model_version", self.model_version),
            "aggregation": aggregation,
            "enhanced_analysis_review_id": enhanced["review_id"],
            "enhanced_analysis": enhanced,
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
