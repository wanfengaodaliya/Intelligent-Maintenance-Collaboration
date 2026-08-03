"""Build the auditable enhanced-analysis result."""

from __future__ import annotations

from typing import Any

from .config import AnalysisConfig
from .contracts import AnalysisContext, EnhancedAnalysisResult


def build_enhanced_analysis_result(
    *,
    context: AnalysisContext,
    config: AnalysisConfig,
    time_domain_evidence: dict[str, Any],
    spectrum_evidence: dict[str, Any],
    envelope_evidence: dict[str, Any],
    time_frequency_evidence: dict[str, Any],
    bearing_evidence: dict[str, Any],
    history_evidence: dict[str, Any],
    model_evidence: dict[str, Any],
    extra_limitations: list[dict[str, str]],
    created_at_ns: int,
) -> EnhancedAnalysisResult:
    all_limitations = list(context.limitations)
    if context.context_status == "partial_context":
        all_limitations.append(
            {
                "code": "partial_context_window",
                "severity": "warning",
                "message": "partial context limits conclusion confidence",
            }
        )
    limitations = _dedupe(
        all_limitations
        + list(extra_limitations)
        + list(spectrum_evidence.get("limitations") or [])
        + list(envelope_evidence.get("limitations") or [])
        + list(time_frequency_evidence.get("limitations") or [])
        + list(bearing_evidence.get("limitations") or [])
        + list(history_evidence.get("limitations") or [])
    )
    data_quality = {
        "level": "good" if not limitations else "degraded",
        "issues": [item["code"] for item in limitations],
    }
    signal_evidence = {
        "time_domain": time_domain_evidence,
        "spectrum": {
            "dominant_peaks_hz": spectrum_evidence["dominant_peaks_hz"],
            "frequency_resolution_hz": spectrum_evidence["frequency_resolution_hz"],
            "peaks": spectrum_evidence["peaks"],
            "band_energy": spectrum_evidence["band_energy"],
        },
        "envelope": {
            "dominant_peaks_hz": envelope_evidence["dominant_peaks_hz"],
            "frequency_resolution_hz": envelope_evidence["frequency_resolution_hz"],
            "peaks": envelope_evidence["peaks"],
        },
        "time_frequency": {
            "nonstationarity_score": time_frequency_evidence["nonstationarity_score"],
            "frame_count": time_frequency_evidence["frame_count"],
            "time_step_s": time_frequency_evidence["time_step_s"],
            "frame_rms": time_frequency_evidence["frame_rms"],
            "energy_mutation_frames": time_frequency_evidence["energy_mutation_frames"],
        },
        "bearing_matches": bearing_evidence["evidence"],
    }
    result = EnhancedAnalysisResult(
        producer="cloud.enhanced_analysis",
        review_id=context.review_id,
        status="succeeded",
        context_status=context.context_status,
        algorithm_version=config.algorithm_version,
        config_version=config.config_version,
        input={
            "aggregation_result_id": context.aggregation_result_id,
            "preprocessed_window_path": context.preprocessed_window_path,
            "preprocessed_window_sha256": context.preprocessed_window_sha256,
            "window_duration_ms": context.window_duration_ms,
            "frequency_resolution_hz": context.frequency_resolution_hz,
            "sample_count": context.sample_count,
        },
        data_quality=data_quality,
        signal_evidence=signal_evidence,
        history_evidence=history_evidence,
        model_evidence=model_evidence,
        operating_conditions={
            "speed_rpm": context.speed_rpm,
            "radial_load_n": context.radial_load_n,
        },
        limitations=limitations,
        created_at_ns=created_at_ns,
        suggested_review_required=False,
    )
    result = EnhancedAnalysisResult(
        **{**result.to_dict(), "suggested_review_required": should_suggest_manual_review(result)}
    )
    return result


def should_suggest_manual_review(result: EnhancedAnalysisResult) -> bool:
    if result.context_status == "partial_context":
        return True
    if result.data_quality["level"] != "good":
        return True
    model = result.model_evidence
    if model.get("status") == "available" and (model.get("uncertainty") or 0.0) > 0.30:
        return True
    physical_label = best_confirmed_physical_label(result.signal_evidence["bearing_matches"])
    model_label = model.get("label")
    if physical_label is not None and model_label is not None and physical_label != model_label:
        return True
    return False


def best_confirmed_physical_label(bearing_matches: list[dict[str, Any]]) -> str | None:
    confirmed = [
        item
        for item in bearing_matches
        if item.get("confidence_level") in {"high", "medium"}
    ]
    if not confirmed:
        return None
    return max(confirmed, key=lambda item: item.get("score") or 0.0).get("hypothesis")


def _dedupe(limitations: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in limitations:
        if item["code"] not in seen:
            seen.add(item["code"])
            result.append(item)
    return result
