"""Adapt an exact bearing window into the existing enhanced-analysis pipeline."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from cloud_service.context_aggregation.service import ContextAggregationService
from cloud_service.enhanced_analysis.diagnosis_model import (
    DiagnosisModel,
    RuleBasedDiagnosisAdapter,
)
from cloud_service.enhanced_analysis.service import EnhancedAnalysisService
from cloud_service.storage.cloud_review_repository import CloudReviewRepository
from cloud_service.storage.edge_feature_repository import EdgeFeatureRepository
from cloud_service.storage.raw_context_repository import RawContextRequestRepository
from cloud_service.storage.raw_packet_repository import RawPacketRepository


class BearingWindowEnhancedBridge:
    def __init__(self, database_path: Path, model: DiagnosisModel | None = None):
        self.database_path = Path(database_path)
        self.model = model or RuleBasedDiagnosisAdapter()

    def analyze(self, review: dict[str, Any], packets: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(packets, key=lambda packet: packet["sequence_number"])
        if len(ordered) != 20:
            raise ValueError("BEARING_REVIEW_INCOMPLETE")
        anchor = ordered[-1]
        self._persist_anchor_summary(anchor)
        raw = RawPacketRepository(self.database_path)
        for packet in ordered:
            raw.store({**packet, "validation_status": "valid"})

        cloud_review_id = CloudReviewRepository(self.database_path).upsert_preliminary(
            device_id=review["device_id"],
            bearing_id=review["bearing_id"],
            sender_id=review["sender_id"],
            anchor_packet_id=anchor["packet_id"],
            task_id=review["task_id"],
            feature_extractor_version="bearing-window-raw-v1",
            schema_version="bearing-window-v1",
            data_quality_valid=True,
            data_quality={"valid": True, "warning_flags": []},
            start_timestamp_ns=ordered[0]["end_generate_timestamp_ns"] - 50_000_000,
            end_timestamp_ns=anchor["end_generate_timestamp_ns"],
        )
        now = time.time_ns()
        context = RawContextRequestRepository(self.database_path).create_or_get(
            request_id="bearing-window-context:%s" % review["bearing_review_id"],
            review_id=cloud_review_id,
            device_id=review["device_id"],
            task_id=review["task_id"],
            bearing_id=review["bearing_id"],
            sender_id=review["sender_id"],
            anchor_packet_id=anchor["packet_id"],
            anchor_sequence_number=anchor["sequence_number"],
            before_packet_count=19,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=now,
            deadline_at_ns=now + 60_000_000_000,
        )
        for packet in ordered[:-1]:
            status, error = raw.ingest_context(
                packet,
                review_id=cloud_review_id,
                relative_position=packet["sequence_number"] - anchor["sequence_number"],
                role="before",
            )
            if status == "conflict":
                raise ValueError(error or "CONTEXT_PACKET_CONFLICT")
        RawContextRequestRepository(self.database_path).mark_complete(context["request_id"])
        ContextAggregationService(self.database_path).aggregate(cloud_review_id)
        result = EnhancedAnalysisService(
            self.database_path,
            model=self.model,
        ).analyze(cloud_review_id)
        return result.to_dict()

    def _persist_anchor_summary(self, packet: dict[str, Any]) -> None:
        EdgeFeatureRepository(self.database_path).ingest_summary(
            {
                "device_id": packet["device_id"],
                "task_id": packet["task_id"],
                "bearing_id": packet["bearing_id"],
                "sender_id": packet["sender_id"],
                "packet_id": packet["packet_id"],
                "sequence_number": packet["sequence_number"],
                "edge_node_id": "edge_window_upload",
                "end_timestamp_ns": packet["end_generate_timestamp_ns"],
                # Keep the synthetic summary byte-for-byte stable so a failed
                # analysis can be retried without creating an ingestion conflict.
                "summary_generated_at_ns": packet["end_generate_timestamp_ns"],
                "processing_status": "perception_rejected",
                "perception_error_codes": ["WINDOW_LEVEL_RAW_REVIEW"],
            }
        )
