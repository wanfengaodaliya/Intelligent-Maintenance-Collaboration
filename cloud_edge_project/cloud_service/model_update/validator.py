"""Historical-baseline validation orchestration for update candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.model_update.contracts import (
    MIN_AGREEMENT_IMPROVEMENT,
    MIN_VALIDATION_SAMPLE_COUNT,
)
from scenarios.bearing.cloud.model_update.candidate_runner import run_candidate


VALIDATION_NOTE = "基线指标来自历史保存的边缘结果；候选指标来自候选版本对同一批历史特征的重跑。"


def validate_samples(candidate: dict[str, dict[str, float]], task: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    test_count = len(samples)
    if not test_count:
        return {"test_count": 0, "reason": "VALIDATION_SAMPLES_NOT_FOUND"}
    baseline_count = sum(
        sample["historical_edge_result"]["label"] == sample["cloud_reference"]["label"]
        for sample in samples
    )
    candidate_count = 0
    for sample in samples:
        candidate_label = run_candidate(candidate, sample["features"])
        candidate_count += candidate_label == sample["cloud_reference"]["label"]
    baseline_rate = baseline_count / test_count
    candidate_rate = candidate_count / test_count
    improvement = candidate_rate - baseline_rate
    return {
        "test_count": test_count,
        "baseline_version": task["old_version"],
        "candidate_version": task["new_version"],
        "baseline_agreement_count": baseline_count,
        "candidate_agreement_count": candidate_count,
        "baseline_agreement_rate": baseline_rate,
        "candidate_agreement_rate": candidate_rate,
        "agreement_improvement": improvement,
        "recommend_publish": (
            test_count >= MIN_VALIDATION_SAMPLE_COUNT
            and improvement >= MIN_AGREEMENT_IMPROVEMENT
        ),
        "note": VALIDATION_NOTE,
    }
