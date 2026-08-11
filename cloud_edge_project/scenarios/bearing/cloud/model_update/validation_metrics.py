"""Prediction metrics shared by any future bearing candidate adapter."""

from __future__ import annotations

from typing import Any


RISK_LEVELS = ("normal", "warning", "abnormal")
RISK_RANK = {label: index for index, label in enumerate(RISK_LEVELS)}


def classification_metrics(
    results: list[dict[str, Any]], prediction_key: str, risk_prediction_key: str
) -> dict[str, float]:
    if not results:
        raise ValueError("VALIDATION_RESULTS_REQUIRED")
    truths: list[str] = []
    predictions: list[str] = []
    truth_risks: list[str] = []
    prediction_risks: list[str] = []
    for result in results:
        truth = result.get("confirmed_label")
        prediction = result.get(prediction_key)
        if not isinstance(truth, str) or not truth or not isinstance(prediction, str) or not prediction:
            raise ValueError("INVALID_VALIDATION_LABEL")
        truth_risk = result.get("confirmed_risk_level")
        prediction_risk = result.get(risk_prediction_key)
        if truth_risk is None and truth in RISK_RANK:
            truth_risk = truth
        if prediction_risk is None and prediction in RISK_RANK:
            prediction_risk = prediction
        if truth_risk not in RISK_RANK or prediction_risk not in RISK_RANK:
            raise ValueError("VALIDATION_RISK_LEVEL_REQUIRED")
        truths.append(truth)
        predictions.append(prediction)
        truth_risks.append(truth_risk)
        prediction_risks.append(prediction_risk)
    accuracy = sum(a == b for a, b in zip(truths, predictions)) / len(results)
    precisions: list[float] = []
    recalls: list[float] = []
    f1_values: list[float] = []
    labels = sorted(set(truths) | set(predictions))
    for label in labels:
        true_positive = sum(t == label and p == label for t, p in zip(truths, predictions))
        false_positive = sum(t != label and p == label for t, p in zip(truths, predictions))
        false_negative = sum(t == label and p != label for t, p in zip(truths, predictions))
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        precisions.append(precision)
        recalls.append(recall)
        f1_values.append(f1)
    abnormal_total = sum(risk == "abnormal" for risk in truth_risks)
    abnormal_recall = (
        sum(
            truth == "abnormal" and prediction == "abnormal"
            for truth, prediction in zip(truth_risks, prediction_risks)
        )
        / abnormal_total
        if abnormal_total
        else 0.0
    )
    return {
        "accuracy": accuracy,
        "precision": sum(precisions) / len(labels),
        "recall": sum(recalls) / len(labels),
        "f1": sum(f1_values) / len(labels),
        "abnormal_recall": abnormal_recall,
        "risk_underestimation_rate": sum(
            RISK_RANK[prediction] < RISK_RANK[truth]
            for truth, prediction in zip(truth_risks, prediction_risks)
        ) / len(results),
        "risk_overestimation_rate": sum(
            RISK_RANK[prediction] > RISK_RANK[truth]
            for truth, prediction in zip(truth_risks, prediction_risks)
        ) / len(results),
    }
