from __future__ import annotations

from collections import Counter
from typing import Sequence

import numpy as np
from sklearn.metrics import confusion_matrix, f1_score, precision_score, recall_score


LABELS = ("normal", "fault")
SOURCE_TO_BINARY_LABEL = {
    "healthy": "normal",
    "outer_ring_damage": "fault",
    "inner_ring_damage": "fault",
}


def to_binary_label(label: str) -> str:
    try:
        return SOURCE_TO_BINARY_LABEL[label]
    except KeyError as exc:
        raise ValueError(f"Unknown source label: {label}") from exc


def _majority_vote(predictions: Sequence[str]) -> str:
    counts = Counter(predictions)
    return min(LABELS, key=lambda label: (-counts[label], LABELS.index(label)))


def evaluate_predictions(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    bearing_ids: Sequence[str],
) -> dict:
    if not (len(y_true) == len(y_pred) == len(bearing_ids)) or not y_true:
        raise ValueError("真实标签、预测标签和轴承 ID 必须等长且非空")
    if not set(y_true) <= set(LABELS) or not set(y_pred) <= set(LABELS):
        raise ValueError("评价数据含未知标签")

    recalls = recall_score(
        y_true,
        y_pred,
        labels=LABELS,
        average=None,
        zero_division=0,
    )
    precisions = precision_score(
        y_true,
        y_pred,
        labels=LABELS,
        average=None,
        zero_division=0,
    )
    bearing_predictions: dict[str, dict] = {}
    bearing_window_accuracy: dict[str, float] = {}
    for bearing_id in dict.fromkeys(bearing_ids):
        indices = [index for index, value in enumerate(bearing_ids) if value == bearing_id]
        truths = {y_true[index] for index in indices}
        if len(truths) != 1:
            raise ValueError(f"同一轴承出现多个真实标签: {bearing_id}")
        true_label = next(iter(truths))
        predictions = [y_pred[index] for index in indices]
        predicted_label = _majority_vote(predictions)
        bearing_predictions[bearing_id] = {
            "true": true_label,
            "predicted": predicted_label,
            "correct": predicted_label == true_label,
        }
        bearing_window_accuracy[bearing_id] = float(
            np.mean([prediction == true_label for prediction in predictions])
        )

    return {
        "window_macro_f1": float(
            f1_score(y_true, y_pred, labels=LABELS, average="macro", zero_division=0)
        ),
        "class_recall": dict(zip(LABELS, map(float, recalls))),
        "class_precision": dict(zip(LABELS, map(float, precisions))),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=LABELS).tolist(),
        "bearing_majority_accuracy": float(
            np.mean([item["correct"] for item in bearing_predictions.values()])
        ),
        "bearing_predictions": bearing_predictions,
        "bearing_window_accuracy": bearing_window_accuracy,
        "worst_bearing_accuracy": min(bearing_window_accuracy.values()),
    }


def select_winner(
    reports: Sequence[dict],
    *,
    macro_f1_tie_tolerance: float,
) -> dict:
    if not reports:
        raise ValueError("没有可选实验")
    best_macro_f1 = max(float(report["window_macro_f1"]) for report in reports)
    close = [
        report
        for report in reports
        if best_macro_f1 - float(report["window_macro_f1"]) <= macro_f1_tie_tolerance
    ]
    return min(
        close,
        key=lambda report: (
            -float(report["worst_bearing_accuracy"]),
            int(report["feature_count"]),
            int(report["model_bytes"]),
            str(report["experiment"]),
        ),
    )
