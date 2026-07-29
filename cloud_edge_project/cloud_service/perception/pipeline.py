"""Cloud perception orchestration for a validated review request."""

from __future__ import annotations

from typing import Any

from cloud_service.perception.feature_extractor import extract_single_packet_features
from cloud_service.perception.packet_buffer import SenderPacketBuffer
from cloud_service.perception.preprocessor import preprocess_packet
from cloud_service.perception.validator import validate_cloud_review_quality, validate_cloud_review_request


_BUFFER = SenderPacketBuffer()
_ENABLED_MODULES = [
    "validation", "basic_preprocessing", "edge_reference_capture",
    "cloud_feature_recomputation", "window_aggregation",
    "continuous_current_harmonics", "trend_analysis",
]


def run_perception(request: dict[str, Any]) -> dict[str, Any]:
    """Run core perception processing and return stable model context."""

    quality = validate_cloud_review_quality(request)
    if not quality.valid:
        return _invalid_result(request, quality)
    validated = validate_cloud_review_request(request)
    preprocessed = preprocess_packet(validated)
    features = extract_single_packet_features(preprocessed)
    raw = preprocessed["cloud_raw_packet"]
    edge = validated["edge_perception_result"]
    aggregation = _BUFFER.add({
        "sender_id": raw["sender_id"],
        "sequence_number": raw["sequence_number"],
        "preprocessed": preprocessed,
        "single_packet_features": features,
    })
    return {
        "schema_version": "cloud_perception_result/2.0",
        "feature_extractor_version": "cloud_high_rate_feature_v1",
        "task_id": raw["task_id"], "packet_id": raw["packet_id"],
        "sender_id": raw["sender_id"], "sequence_number": raw["sequence_number"],
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
        "operating_context": edge["features"]["operating_context"],
        "edge_reference_features": {key: value for key, value in edge.get("features", {}).items() if key != "operating_context"},
        "cloud_recomputed_features": _documented_features(features),
        "cloud_enhanced_features": {
            "context_status": aggregation["context_status"],
            "trend_summaries": aggregation["aggregated_features"],
            "current_harmonics": aggregation["thd"],
        },
        "advanced_features": None,
        "context_features": None,
    }


def run_preliminary_perception(
    request: dict[str, Any],
) -> dict[str, Any]:
    """Finish trigger-packet features without entering packet aggregation."""

    quality = validate_cloud_review_quality(request)
    if not quality.valid:
        return _invalid_result(request, quality)
    validated = validate_cloud_review_request(request)
    preprocessed = preprocess_packet(validated)
    features = extract_single_packet_features(preprocessed)
    raw = preprocessed["cloud_raw_packet"]
    edge = validated["edge_perception_result"]
    return {
        "schema_version": "cloud_perception_result/2.0",
        "feature_extractor_version": "cloud_high_rate_feature_v1",
        "task_id": raw["task_id"],
        "packet_id": raw["packet_id"],
        "sender_id": raw["sender_id"],
        "sequence_number": raw["sequence_number"],
        "analysis_window": {
            "start_timestamp_ns": raw["start_timestamp_ns"],
            "end_timestamp_ns": raw["end_timestamp_ns"],
            "packet_count": 1,
        },
        "enabled_modules": [
            "validation",
            "basic_preprocessing",
            "edge_reference_capture",
            "cloud_feature_recomputation",
        ],
        "data_quality": {
            "valid": quality.valid,
            "blocking_issues": quality.blocking_issues,
            "warning_flags": quality.warning_flags,
            "context_status": "pending_context",
        },
        "operating_context": edge["features"]["operating_context"],
        "edge_reference_features": {
            key: value
            for key, value in edge.get("features", {}).items()
            if key != "operating_context"
        },
        "cloud_recomputed_features": _documented_features(features),
        "cloud_enhanced_features": {
            "context_status": "pending_context",
            "trend_summaries": None,
            "current_harmonics": None,
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
        },
        "phase_current_2": {
            "rms_a": phase_2["rms"], "absolute_peak_a": phase_2["peak"],
        },
        "current_relationship": {
            "current_imbalance_ratio": features["current_rms_imbalance_ratio"],
        },
    }


def _invalid_result(request: dict[str, Any], quality: Any) -> dict[str, Any]:
    edge = request.get("edge_perception_result", {}) if isinstance(request, dict) else {}
    raw = request.get("cloud_raw_packet", {}) if isinstance(request, dict) else {}
    return {
        "schema_version": "cloud_perception_result/2.0", "feature_extractor_version": "cloud_high_rate_feature_v1",
        "task_id": raw.get("task_id", edge.get("task_id")), "packet_id": raw.get("packet_id", edge.get("packet_id")),
        "sender_id": raw.get("sender_id", edge.get("sender_id")), "sequence_number": raw.get("sequence_number", edge.get("sequence_number")),
        "analysis_window": None, "enabled_modules": _ENABLED_MODULES,
        "data_quality": {"valid": False, "blocking_issues": quality.blocking_issues, "warning_flags": quality.warning_flags, "context_status": "invalid"},
        "operating_context": None, "edge_reference_features": None, "cloud_recomputed_features": None,
        "cloud_enhanced_features": None, "advanced_features": None, "context_features": None,
    }
