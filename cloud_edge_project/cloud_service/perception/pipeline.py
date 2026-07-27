"""Cloud perception orchestration for a validated review request."""

from __future__ import annotations

from typing import Any

from cloud_service.perception.feature_extractor import extract_single_packet_features
from cloud_service.perception.packet_buffer import DevicePacketBuffer
from cloud_service.perception.preprocessor import preprocess_packet
from cloud_service.perception.validator import validate_cloud_review_quality, validate_cloud_review_request


_BUFFER = DevicePacketBuffer()
_ENABLED_MODULES = [
    "validation", "basic_preprocessing", "edge_reference_capture",
    "cloud_feature_recomputation", "window_aggregation",
    "continuous_current_harmonics", "trend_analysis",
]


def run_perception(request: dict[str, Any]) -> dict[str, Any]:
    """Run core perception processing and return stable model context."""

    quality = validate_cloud_review_quality(request)
    validated = validate_cloud_review_request(request)
    preprocessed = preprocess_packet(validated)
    features = extract_single_packet_features(preprocessed)
    raw = validated["cloud_raw_packet"]
    edge = validated["edge_perception_result"]
    aggregation = _BUFFER.add({
        "device_id": raw["device_id"],
        "sequence_number": raw["sequence_number"],
        "preprocessed": preprocessed,
        "single_packet_features": features,
    })
    return {
        "schema_version": "cloud_perception_result/2.0",
        "feature_extractor_version": "cloud_high_rate_feature_v1",
        "task_id": raw["task_id"], "packet_id": raw["packet_id"],
        "device_id": raw["device_id"], "sequence_number": raw["sequence_number"],
        "analysis_window": {
            "start_timestamp_ns": aggregation["start_timestamp_ns"],
            "end_timestamp_ns": aggregation["end_timestamp_ns"],
            "packet_count": aggregation["packet_count"],
        },
        "enabled_modules": _ENABLED_MODULES,
        "data_quality": {
            "valid": quality.valid,
            "blocking_issues": quality.blocking_issues,
            "warning_flags": quality.warning_flags,
            "context_status": aggregation["context_status"],
        },
        "edge_reference_features": edge.get("features"),
        "cloud_recomputed_features": _documented_features(features),
        "cloud_enhanced_features": {
            "context_status": aggregation["context_status"],
            "trend_summaries": aggregation["aggregated_features"],
            "current_harmonics": aggregation["thd"],
        },
        "advanced_features": None,
        "context_features": None,
    }


def _documented_features(features: dict[str, Any]) -> dict[str, Any]:
    vibration = features["vibration"]
    phase_1 = features["phase_current_1"]
    phase_2 = features["phase_current_2"]
    return {
        "vibration": {
            "rms": vibration["rms"], "absolute_peak": vibration["peak"],
            "kurtosis": vibration["kurtosis"],
            "dominant_frequency_hz": vibration["dominant_frequency_hz"],
            "band_power_ratio_500_2000": vibration["energy_ratio_500_2000hz"],
            "spectral_entropy": vibration["spectral_entropy"],
        },
        "phase_current_1": {
            "rms_a": phase_1["rms"], "absolute_peak_a": phase_1["peak"],
            "fundamental_frequency_hz": phase_1["fundamental_frequency_hz"],
        },
        "phase_current_2": {
            "rms_a": phase_2["rms"], "absolute_peak_a": phase_2["peak"],
            "fundamental_frequency_hz": phase_2["fundamental_frequency_hz"],
        },
        "current_relationship": {
            "current_imbalance_ratio": features["current_rms_imbalance_ratio"],
        },
    }
