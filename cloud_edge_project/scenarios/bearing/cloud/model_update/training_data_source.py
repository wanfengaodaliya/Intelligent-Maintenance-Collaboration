"""Read saved edge features and cloud-reviewed focus candidates from SQLite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.model_update.dataset_repository import PacketSourceRepository
from cloud_service.storage.database import connect


class BearingTrainingDataSource:
    def __init__(
        self, database_path: Path, source_repository: PacketSourceRepository
    ) -> None:
        self.database_path = Path(database_path)
        self.source_repository = source_repository

    def load(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT edge.packet_id,edge.task_id,edge.edge_result,
                          edge.confidence,edge.edge_risk_level,edge.edge_model_version,
                          edge.vibration_rms,edge.vibration_absolute_peak,
                          edge.vibration_kurtosis,edge.vibration_dominant_frequency_hz,
                          edge.vibration_band_power_ratio_500_2000,
                          edge.vibration_spectral_entropy,edge.current_1_rms_a,
                          edge.current_1_absolute_peak_a,edge.current_2_rms_a,
                          edge.current_2_absolute_peak_a,edge.current_imbalance_ratio,
                          edge.shaft_speed_rpm_last,edge.load_torque_nm_last,
                          edge.bearing_radial_load_n_last,
                          edge.bearing_module_temperature_c,review.review_id,
                          summary.summary_json
                   FROM edge_packet_summary edge
                   LEFT JOIN cloud_review review
                     ON review.review_id=(
                         SELECT candidate.review_id
                         FROM cloud_review candidate
                         WHERE candidate.sender_id=edge.sender_id
                           AND candidate.anchor_packet_id=edge.packet_id
                           AND candidate.review_status='complete'
                         ORDER BY candidate.updated_at_ns DESC
                         LIMIT 1
                     )
                   LEFT JOIN final_diagnosis_summary summary
                     ON summary.review_id=review.review_id
                    AND summary.status='succeeded'
                   WHERE edge.device_id=? AND edge.edge_model_version=?
                     AND edge.processing_status='perception_completed'
                   ORDER BY edge.end_timestamp_ns,edge.packet_id""",
                (update["subject_id"], update["baseline_version"]),
            ).fetchall()
        samples: list[dict[str, Any]] = []
        for row in rows:
            try:
                summary = (
                    json.loads(row["summary_json"])
                    if row["summary_json"] is not None
                    else {}
                )
            except (TypeError, json.JSONDecodeError):
                summary = {}
            cloud_label = summary.get("label") if isinstance(summary, dict) else None
            if not isinstance(cloud_label, str) or not cloud_label:
                cloud_label = None
            source = self.source_repository.get_by_packet_id(row["packet_id"])
            is_reviewed = row["review_id"] is not None
            pools = []
            if source is not None:
                pools.append("base_dataset")
            else:
                pools.append("reliable_history")
            if is_reviewed:
                pools.append("focus")
            samples.append(
                {
                    "sample_id": row["packet_id"],
                    "packet_id": row["packet_id"],
                    "task_id": row["task_id"],
                    "source_file": source["source_file"] if source else None,
                    "features": _edge_features(row),
                    "historical_edge_result": {
                        "label": row["edge_result"],
                        "confidence": row["confidence"],
                        "risk_level": row["edge_risk_level"],
                        "version": row["edge_model_version"],
                    },
                    "cloud_label": cloud_label,
                    "is_cloud_reviewed": is_reviewed,
                    "sample_pools": pools,
                }
            )
        return samples


def _edge_features(row: Any) -> dict[str, Any]:
    """Return only features available to the deployed edge pipeline."""

    return {
        "vibration": {
            "rms": row["vibration_rms"],
            "absolute_peak": row["vibration_absolute_peak"],
            "kurtosis": row["vibration_kurtosis"],
            "dominant_frequency_hz": row["vibration_dominant_frequency_hz"],
            "band_power_ratio_500_2000": row["vibration_band_power_ratio_500_2000"],
            "spectral_entropy": row["vibration_spectral_entropy"],
        },
        "phase_current_1": {
            "rms_a": row["current_1_rms_a"],
            "absolute_peak_a": row["current_1_absolute_peak_a"],
        },
        "phase_current_2": {
            "rms_a": row["current_2_rms_a"],
            "absolute_peak_a": row["current_2_absolute_peak_a"],
        },
        "current_imbalance_ratio": row["current_imbalance_ratio"],
        "operating_context": {
            "shaft_speed_rpm": row["shaft_speed_rpm_last"],
            "load_torque_nm": row["load_torque_nm_last"],
            "bearing_radial_load_n": row["bearing_radial_load_n_last"],
            "bearing_module_temperature_c": row["bearing_module_temperature_c"],
        },
    }
