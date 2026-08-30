"""Bearing-specific problem candidate rules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cloud_service.global_analysis.problem_detector import build_problem_candidate
from scenarios.bearing.cloud.global_analysis.config import GlobalAnalysisConfig


def detect_bearing_problem_candidates(
    analysis_results: Mapping[str, Any],
    previous_analysis: list[dict[str, Any]],
    config: GlobalAnalysisConfig,
) -> list[dict[str, Any]]:
    review = analysis_results.get("cloud_bearing_review_analysis")
    if not isinstance(review, Mapping):
        return []
    if review.get("status") != "succeeded":
        return []
    if review.get("bearing_correction_rate", 0) < config.bearing_correction_warning_rate:
        return []
    return [
        build_problem_candidate(
            "cloud_bearing_review",
            "high_correction_rate",
            "medium",
            {
                "sample_count": review.get("bearing_review_count"),
                "bearing_correction_rate": review.get("bearing_correction_rate"),
            },
            "cloud_review_policy_review",
            previous_analysis,
        )
    ]
