"""Adapt cloud-review request data to the SQLite repositories."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cloud_review_repository import CloudReviewRepository
from .database import initialize_database
from .edge_feature_repository import EdgeFeatureRepository
from .raw_packet_repository import RawPacketRepository


class CloudReviewPersistence:
    """Persist one completed cloud-review request using short repository calls."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)
        self.edge_features = EdgeFeatureRepository(self.database_path)
        self.raw_packets = RawPacketRepository(self.database_path)
        self.reviews = CloudReviewRepository(self.database_path)

    def persist(self, request: dict[str, Any], perception_result: dict[str, Any]) -> str:
        edge = request["edge_perception_result"]
        raw = request["cloud_raw_packet"]
        self.edge_features.ingest(_edge_summary(edge))
        validation_status = "valid" if perception_result["data_quality"]["valid"] else "invalid"
        if validation_status == "valid" and perception_result["data_quality"]["warning_flags"]:
            validation_status = "warning"
        self.raw_packets.store({**raw, "validation_status": validation_status})
        review_id = self.reviews.upsert_preliminary(
            sender_id=raw["sender_id"], anchor_packet_id=raw["packet_id"], task_id=raw["task_id"],
            feature_extractor_version=perception_result["feature_extractor_version"],
            schema_version=perception_result["schema_version"],
            data_quality_valid=perception_result["data_quality"]["valid"],
            data_quality=perception_result["data_quality"],
            start_timestamp_ns=perception_result["analysis_window"]["start_timestamp_ns"],
            end_timestamp_ns=perception_result["analysis_window"]["end_timestamp_ns"],
        )
        enhanced = perception_result["cloud_enhanced_features"]
        if enhanced["context_status"] == "complete":
            self.reviews.complete(
                review_id, cloud_recomputed_features=perception_result["cloud_recomputed_features"],
                cloud_enhanced_features=enhanced, advanced_features=None, context_features=None,
                packet_count=perception_result["analysis_window"]["packet_count"],
            )
        else:
            self.reviews.mark_insufficient_context(review_id)
        return review_id


def _edge_summary(edge: dict[str, Any]) -> dict[str, Any]:
    """Adapt a cloud-review edge result to the documented summary shape."""

    edge_features = edge["features"]
    vibration, current_1 = edge_features["vibration"], edge_features["phase_current_1"]
    current_2, relationship = edge_features["phase_current_2"], edge_features["current_relationship"]
    quality = edge.get("perception_quality", {})
    inference = edge.get(
        "edge_inference",
        {"edge_result": "warning", "confidence": 0.0, "edge_risk_level": "medium"},
    )
    return {
        "sender_id": edge["sender_id"], "packet_id": edge["packet_id"], "task_id": edge["task_id"],
        "sequence_number": edge["sequence_number"], "edge_node_id": edge.get("edge_node_id", "cloud_review_edge"),
        "end_timestamp_ns": edge["end_generate_timestamp_ns"], "summary_generated_at_ns": edge["feature_generated_at_ns"],
        "processing_status": "perception_completed",
        "edge_model_version": edge.get("edge_model_version", "cloud_review_legacy"),
        "perception_quality": {"status": quality.get("status", "good"), "flags": quality.get("flags", [])},
        "features": {
            "vibration": {"source_sample_rate_hz": vibration.get("source_sample_rate_hz", 64_000), "analysis_sample_rate_hz": vibration.get("analysis_sample_rate_hz", 16_000), "unit": vibration.get("unit", "mm/s"), **vibration},
            "phase_current_1": {"source_sample_rate_hz": current_1.get("source_sample_rate_hz", 64_000), "analysis_sample_rate_hz": current_1.get("analysis_sample_rate_hz", 16_000), "unit": current_1.get("unit", "A"), **current_1},
            "phase_current_2": {"source_sample_rate_hz": current_2.get("source_sample_rate_hz", 64_000), "analysis_sample_rate_hz": current_2.get("analysis_sample_rate_hz", 16_000), "unit": current_2.get("unit", "A"), **current_2},
            "current_relationship": relationship,
            "operating_context": edge_features["operating_context"],
        },
        "edge_inference": inference,
    }
