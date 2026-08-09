"""Run the first-phase JSON threshold candidate on normalized bearing features."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CandidateInputIncompatible(ValueError):
    pass


class InvalidCandidate(ValueError):
    pass


_SUPPORTED_THRESHOLD_KEYS = {"vibration_rms_min", "kurtosis_min"}


def load_candidate(candidate_path: Path) -> dict[str, dict[str, float]]:
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidCandidate("候选文件不是有效 JSON") from error
    if not isinstance(candidate, dict):
        raise InvalidCandidate("候选文件必须是 JSON 对象")
    normalized: dict[str, dict[str, float]] = {}
    for label in ("normal", "warning", "abnormal"):
        if label not in candidate:
            raise InvalidCandidate(f"缺少 {label} 阈值定义")
        thresholds = candidate[label]
        if not isinstance(thresholds, dict):
            raise InvalidCandidate(f"{label} 阈值必须是对象")
        if label in {"warning", "abnormal"} and not thresholds:
            raise InvalidCandidate(f"{label} 阈值不能为空")
        if not set(thresholds).issubset(_SUPPORTED_THRESHOLD_KEYS):
            raise InvalidCandidate(f"{label} 包含不支持的阈值")
        if not all(isinstance(value, (int, float)) for value in thresholds.values()):
            raise InvalidCandidate(f"{label} 阈值必须是数值")
        normalized[label] = dict(thresholds)
    return normalized


def run_candidate(candidate: dict[str, dict[str, float]], features: dict[str, Any]) -> str:
    vibration = features.get("vibration")
    if not isinstance(vibration, dict):
        raise CandidateInputIncompatible("缺少 vibration 特征")
    values = {
        "vibration_rms_min": vibration.get("rms"),
        "kurtosis_min": vibration.get("kurtosis"),
    }
    for key, value in values.items():
        if not isinstance(value, (int, float)):
            raise CandidateInputIncompatible(f"缺少候选所需特征 {key}")
    if _matches(candidate["abnormal"], values):
        return "abnormal"
    if _matches(candidate["warning"], values):
        return "warning"
    return "normal"


def _matches(thresholds: dict[str, float], values: dict[str, Any]) -> bool:
    return all(values[key] >= value for key, value in thresholds.items())
