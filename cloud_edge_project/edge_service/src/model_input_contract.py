# -*- coding: utf-8 -*-
"""Shared contract for the packet-level model input."""
from __future__ import annotations

import math
from typing import Any


MODEL_INPUT_SCHEMA_VERSION = "edge-model-input/1.1"

_TOP_LEVEL_FIELDS = {
    "device_id",
    "bearing_id",
    "task_id",
    "packet_id",
    "sender_id",
    "sequence_number",
    "end_generate_timestamp_ns",
    "feature_generated_at_ns",
    "perception_quality",
    "features",
}
_QUALITY_FLAGS = {
    "DEVICE_NOT_RUNNING",
    "VIBRATION_CONSTANT_SIGNAL",
    "PHASE_CURRENT_1_CONSTANT_SIGNAL",
    "PHASE_CURRENT_2_CONSTANT_SIGNAL",
}
_STATS_FIELDS = {"mean", "last", "minimum", "maximum", "standard_deviation"}


class ModelInputValidationError(ValueError):
    """Raised when a model input is not a complete PerceptionResult."""


def _fail(path: str, message: str) -> None:
    raise ModelInputValidationError(f"{path}: {message}")


def _object(value: Any, path: str, fields: set[str]) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        _fail(path, "fields do not match contract (" + "; ".join(details) + ")")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, "must be a non-empty string")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(path, "must be a positive integer")
    return value


def _number(value: Any, path: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        _fail(path, "must be a finite number")
    if minimum is not None and result < minimum:
        _fail(path, f"must be >= {minimum}")
    if maximum is not None and result > maximum:
        _fail(path, f"must be <= {maximum}")
    return result


def _fixed(value: Any, expected: Any, path: str) -> None:
    if value != expected or isinstance(value, bool):
        _fail(path, f"must equal {expected!r}")


def _validate_quality(value: Any) -> None:
    quality = _object(value, "input.perception_quality", {"status", "flags"})
    status = quality["status"]
    if status not in {"good", "warning"}:
        _fail("input.perception_quality.status", "must be 'good' or 'warning'")
    flags = quality["flags"]
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        _fail("input.perception_quality.flags", "must be a string array")
    if len(flags) != len(set(flags)) or not set(flags) <= _QUALITY_FLAGS:
        _fail("input.perception_quality.flags", "contains duplicate or unsupported values")
    if (status == "good") != (not flags):
        _fail("input.perception_quality", "status must be good exactly when flags is empty")


def _validate_vibration(value: Any) -> None:
    path = "input.features.vibration"
    vibration = _object(value, path, {
        "source_sample_rate_hz", "analysis_sample_rate_hz", "rms", "absolute_peak",
        "kurtosis", "dominant_frequency_hz", "band_power_ratio_500_2000",
        "spectral_entropy", "unit",
    })
    _fixed(vibration["source_sample_rate_hz"], 64000, path + ".source_sample_rate_hz")
    _fixed(vibration["analysis_sample_rate_hz"], 16000, path + ".analysis_sample_rate_hz")
    _fixed(vibration["unit"], "mm/s", path + ".unit")
    _number(vibration["rms"], path + ".rms", minimum=0.0)
    _number(vibration["absolute_peak"], path + ".absolute_peak", minimum=0.0)
    _number(vibration["kurtosis"], path + ".kurtosis")
    _number(vibration["dominant_frequency_hz"], path + ".dominant_frequency_hz",
            minimum=20.0, maximum=8000.0)
    _number(vibration["band_power_ratio_500_2000"],
            path + ".band_power_ratio_500_2000", minimum=0.0, maximum=1.0)
    _number(vibration["spectral_entropy"], path + ".spectral_entropy",
            minimum=0.0, maximum=1.0)


def _validate_current(value: Any, name: str) -> None:
    path = "input.features." + name
    current = _object(value, path, {
        "source_sample_rate_hz", "analysis_sample_rate_hz", "rms_a",
        "absolute_peak_a", "unit",
    })
    _fixed(current["source_sample_rate_hz"], 64000, path + ".source_sample_rate_hz")
    _fixed(current["analysis_sample_rate_hz"], 16000, path + ".analysis_sample_rate_hz")
    _fixed(current["unit"], "A", path + ".unit")
    _number(current["rms_a"], path + ".rms_a", minimum=0.0)
    _number(current["absolute_peak_a"], path + ".absolute_peak_a", minimum=0.0)


def _validate_stats(value: Any, path: str) -> None:
    stats = _object(value, path, _STATS_FIELDS)
    for field in _STATS_FIELDS:
        _number(stats[field], path + "." + field,
                minimum=0.0 if field == "standard_deviation" else None)
    if float(stats["minimum"]) > float(stats["maximum"]):
        _fail(path, "minimum must be <= maximum")


def _validate_features(value: Any) -> None:
    features = _object(value, "input.features", {
        "vibration", "phase_current_1", "phase_current_2", "current_relationship",
        "operating_context",
    })
    _validate_vibration(features["vibration"])
    _validate_current(features["phase_current_1"], "phase_current_1")
    _validate_current(features["phase_current_2"], "phase_current_2")

    relationship = _object(
        features["current_relationship"],
        "input.features.current_relationship",
        {"current_imbalance_ratio"},
    )
    _number(relationship["current_imbalance_ratio"],
            "input.features.current_relationship.current_imbalance_ratio", minimum=0.0)

    context = _object(features["operating_context"], "input.features.operating_context", {
        "shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n",
        "bearing_module_temperature_c",
    })
    for name in ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n"):
        _validate_stats(context[name], "input.features.operating_context." + name)
    _number(context["bearing_module_temperature_c"],
            "input.features.operating_context.bearing_module_temperature_c")


def validate_model_input(model_input: Any) -> None:
    """Validate the exact PerceptionResult currently produced by EdgePerception."""

    result = _object(model_input, "input", _TOP_LEVEL_FIELDS)
    for field in ("device_id", "bearing_id", "task_id", "packet_id", "sender_id"):
        _text(result[field], "input." + field)
    _positive_int(result["sequence_number"], "input.sequence_number")
    _positive_int(result["end_generate_timestamp_ns"], "input.end_generate_timestamp_ns")
    _positive_int(result["feature_generated_at_ns"], "input.feature_generated_at_ns")
    _validate_quality(result["perception_quality"])
    _validate_features(result["features"])


def model_input_probe() -> dict:
    """Return one complete, valid input for model warm-up and availability checks."""

    stats = {
        "mean": 900.0,
        "last": 900.0,
        "minimum": 899.5,
        "maximum": 900.5,
        "standard_deviation": 0.3,
    }
    return {
        "device_id": "device-model-probe",
        "bearing_id": "bearing-model-probe",
        "task_id": "task-model-probe",
        "packet_id": "packet-model-probe",
        "sender_id": "sender-model-probe",
        "sequence_number": 1,
        "end_generate_timestamp_ns": 1_700_000_000_000_000_000,
        "feature_generated_at_ns": 1_700_000_000_000_000_001,
        "perception_quality": {"status": "good", "flags": []},
        "features": {
            "vibration": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "rms": 0.35,
                "absolute_peak": 1.8,
                "kurtosis": 3.1,
                "dominant_frequency_hz": 120.0,
                "band_power_ratio_500_2000": 0.31,
                "spectral_entropy": 0.64,
                "unit": "mm/s",
            },
            "phase_current_1": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "rms_a": 2.4,
                "absolute_peak_a": 3.4,
                "unit": "A",
            },
            "phase_current_2": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "rms_a": 2.35,
                "absolute_peak_a": 3.38,
                "unit": "A",
            },
            "current_relationship": {"current_imbalance_ratio": 0.03},
            "operating_context": {
                "shaft_speed_rpm": stats,
                "load_torque_nm": {
                    "mean": 0.7, "last": 0.7, "minimum": 0.69,
                    "maximum": 0.71, "standard_deviation": 0.01,
                },
                "bearing_radial_load_n": {
                    "mean": 1000.0, "last": 1000.0, "minimum": 998.0,
                    "maximum": 1002.0, "standard_deviation": 1.0,
                },
                "bearing_module_temperature_c": 46.0,
            },
        },
    }
