"""Format-neutral validation on one frozen test set."""

from __future__ import annotations

from collections import Counter
from typing import Any

from cloud_service.model_update.candidate_registry import schema_is_compatible
from cloud_service.model_update.classification_metrics import classification_metrics
from cloud_service.model_update.contracts import ModelUpdateConfig


TARGET_METRICS = {
    "risk_underestimation": "risk_underestimation_rate",
    "risk_overestimation": "risk_overestimation_rate",
}


def validate_candidate(
    update: dict[str, Any],
    dataset_manifest: dict[str, Any],
    test_results: list[dict[str, Any]],
    config: ModelUpdateConfig,
) -> dict[str, Any]:
    """Compare baseline and candidate predictions for the frozen test IDs."""

    expected_ids = dataset_manifest.get("test_sample_ids")
    actual_ids = [result.get("sample_id") for result in test_results]
    if (
        not isinstance(expected_ids, list)
        or len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != set(expected_ids)
    ):
        raise ValueError("FROZEN_TEST_SET_MISMATCH")
    if update.get("candidate_artifact", {}).get("training_dataset_id") != dataset_manifest.get("dataset_id"):
        raise ValueError("TRAINING_DATASET_MISMATCH")
    if update.get("candidate_artifact", {}).get("feature_pipeline_version") != dataset_manifest.get("feature_pipeline_version"):
        raise ValueError("FEATURE_PIPELINE_MISMATCH")
    if not schema_is_compatible(
        dataset_manifest.get("input_feature_schema"),
        update.get("candidate_artifact", {}).get("input_feature_schema"),
    ):
        raise ValueError("CANDIDATE_INPUT_SCHEMA_INCOMPATIBLE")

    frozen_labels = dataset_manifest.get("sample_labels")
    if not isinstance(frozen_labels, dict):
        raise ValueError("FROZEN_LABELS_NOT_FOUND")
    for result in test_results:
        frozen = frozen_labels.get(result["sample_id"])
        if not isinstance(frozen, dict):
            raise ValueError("FROZEN_LABELS_NOT_FOUND")
        for key in ("confirmed_label", "label_source"):
            if result.get(key) != frozen.get(key):
                raise ValueError("FROZEN_LABEL_MISMATCH")
        frozen_risk = frozen.get("confirmed_risk_level")
        if frozen_risk is not None and result.get("confirmed_risk_level") != frozen_risk:
            raise ValueError("FROZEN_LABEL_MISMATCH")
        if frozen_risk is None and frozen.get("confirmed_label") not in {
            "normal", "warning", "fault"
        }:
            raise ValueError("FROZEN_RISK_LEVEL_NOT_FOUND")

    baseline_metrics = classification_metrics(
        test_results, "baseline_prediction", "baseline_risk_level"
    )
    candidate_metrics = classification_metrics(
        test_results, "candidate_prediction", "candidate_risk_level"
    )
    target_metric = TARGET_METRICS.get(update.get("problem_type"))
    if target_metric is None:
        raise ValueError("UNSUPPORTED_TARGET_PROBLEM")
    focus_ids = set(dataset_manifest.get("focus_sample_ids") or [])
    focus_results = [
        result for result in test_results if result["sample_id"] in focus_ids
    ]
    if not focus_results:
        raise ValueError("TARGET_VALIDATION_SAMPLES_NOT_FOUND")
    baseline_focus = classification_metrics(
        focus_results, "baseline_prediction", "baseline_risk_level"
    )
    candidate_focus = classification_metrics(
        focus_results, "candidate_prediction", "candidate_risk_level"
    )
    target_improvement = baseline_focus[target_metric] - candidate_focus[target_metric]
    overall_degraded = any(
        candidate_metrics[metric]
        < baseline_metrics[metric] - config.max_overall_metric_degradation
        for metric in ("f1", "fault_recall")
    )
    passed = (
        target_improvement >= config.min_target_improvement
        and not overall_degraded
    )
    label_sources = Counter(
        frozen_labels[result["sample_id"]]["label_source"]
        for result in test_results
    )
    return {
        "validation_passed": passed,
        "test_sample_ids": list(expected_ids),
        "test_count": len(test_results),
        "baseline_version": update["baseline_version"],
        "candidate_version": update["candidate_version"],
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
        "target_metric": target_metric,
        "baseline_target_value": baseline_focus[target_metric],
        "candidate_target_value": candidate_focus[target_metric],
        "target_improvement": target_improvement,
        "overall_degraded": overall_degraded,
        "focus_test_sample_ids": [
            result["sample_id"] for result in focus_results
        ],
        "label_source_summary": {
            str(source): count for source, count in sorted(label_sources.items())
        },
    }
