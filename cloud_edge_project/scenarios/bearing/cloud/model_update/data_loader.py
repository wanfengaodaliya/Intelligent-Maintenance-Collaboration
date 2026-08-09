"""Load first-phase validation samples from SQLite or explicit demo data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect


def load_validation_samples(
    database_path: Path,
    task: dict[str, Any],
    *,
    use_demo_data: bool,
    demo_path: Path,
) -> list[dict[str, Any]]:
    if use_demo_data:
        return _load_demo(demo_path, task["old_version"], task["test_data_limit"])
    return _load_history(database_path, task)


def _load_demo(path: Path, old_version: str, limit: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    return [
        item for item in values
        if isinstance(item, dict)
        and item.get("historical_edge_result", {}).get("version") == old_version
        and _valid_sample(item)
    ][:limit]


def _load_history(database_path: Path, task: dict[str, Any]) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT edge.sender_id,edge.packet_id,edge.edge_node_id,edge.edge_result,
                   edge.confidence,edge.edge_risk_level,edge.edge_model_version,
                   edge.vibration_rms,edge.vibration_kurtosis,
                   edge.vibration_absolute_peak,edge.vibration_dominant_frequency_hz,
                   edge.vibration_band_power_ratio_500_2000,edge.vibration_spectral_entropy,
                   edge.current_1_rms_a,edge.current_1_absolute_peak_a,
                   edge.current_2_rms_a,edge.current_2_absolute_peak_a,
                   edge.current_imbalance_ratio,edge.shaft_speed_rpm_last,
                   edge.load_torque_nm_last,edge.bearing_radial_load_n_last,
                   edge.bearing_module_temperature_c,summary.summary_json
            FROM edge_packet_summary edge
            JOIN cloud_review review
              ON review.sender_id=edge.sender_id
             AND review.anchor_packet_id=edge.packet_id
             AND review.device_id=edge.device_id
             AND review.task_id=edge.task_id
             AND review.bearing_id=edge.bearing_id
            JOIN final_diagnosis_summary summary
              ON summary.review_id=review.review_id AND summary.status='succeeded'
            WHERE review.device_id=? AND edge.edge_model_version=?
              AND edge.processing_status='perception_completed' AND review.review_status='complete'
              AND edge.edge_result IS NOT NULL
            ORDER BY review.updated_at_ns ASC LIMIT ?
            """,
            (task["subject_id"], task["old_version"], task["test_data_limit"]),
        ).fetchall()
    samples = []
    for row in rows:
        try:
            reference = json.loads(row["summary_json"])
        except json.JSONDecodeError:
            continue
        label = reference.get("label") if isinstance(reference, dict) else None
        if label not in {"normal", "warning", "abnormal"}:
            continue
        sample = {
            "sample_id": f"{row['sender_id']}:{row['packet_id']}",
            "features": {
                "vibration": {"rms": row["vibration_rms"], "kurtosis": row["vibration_kurtosis"], "absolute_peak": row["vibration_absolute_peak"], "dominant_frequency_hz": row["vibration_dominant_frequency_hz"], "band_power_ratio_500_2000": row["vibration_band_power_ratio_500_2000"], "spectral_entropy": row["vibration_spectral_entropy"]},
                "phase_current_1": {"rms_a": row["current_1_rms_a"], "absolute_peak_a": row["current_1_absolute_peak_a"]},
                "phase_current_2": {"rms_a": row["current_2_rms_a"], "absolute_peak_a": row["current_2_absolute_peak_a"]},
                "current_imbalance_ratio": row["current_imbalance_ratio"],
            },
            "operating_context": {"shaft_speed_rpm": row["shaft_speed_rpm_last"], "load_torque_nm": row["load_torque_nm_last"], "bearing_radial_load_n": row["bearing_radial_load_n_last"], "bearing_module_temperature_c": row["bearing_module_temperature_c"]},
            "historical_edge_result": {"label": row["edge_result"], "confidence": row["confidence"], "risk_level": row["edge_risk_level"], "version": row["edge_model_version"]},
            "cloud_reference": {"label": label},
        }
        if _valid_sample(sample):
            samples.append(sample)
    return samples


def _valid_sample(sample: dict[str, Any]) -> bool:
    historical = sample.get("historical_edge_result")
    reference = sample.get("cloud_reference")
    vibration = sample.get("features", {}).get("vibration") if isinstance(sample.get("features"), dict) else None
    return (
        isinstance(historical, dict)
        and historical.get("label") in {"normal", "warning", "abnormal"}
        and isinstance(reference, dict)
        and reference.get("label") in {"normal", "warning", "abnormal"}
        and isinstance(vibration, dict)
        and isinstance(vibration.get("rms"), (int, float))
        and isinstance(vibration.get("kurtosis"), (int, float))
    )
