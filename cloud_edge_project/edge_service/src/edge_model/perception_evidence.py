# -*- coding: utf-8 -*-
"""边缘单包感知证据构建：原始 50ms 数据包 → PerceptionResult。

阶段 6 收口：特征提取是从 raw packet 构建正式模型输入（及云复核证据）的
共享基础设施，与已淘汰的蒸馏 H5 本地推理解耦后独立成模块。
纯 numpy/scipy 计算，不依赖任何模型权重。
"""
from __future__ import annotations

import time
from typing import Any, Mapping

import numpy as np
from scipy.signal import resample_poly


class PerceptionEvidenceBuilder:
    """从已校验的原始边缘数据包构建感知证据（validate_model_input 可通过）。"""

    version = "edge-perception-evidence-v1"

    def build_evidence(self, raw_packet: Mapping[str, Any]) -> dict[str, Any]:
        data = _mapping(raw_packet, "data")
        vibration = _series(data, "vibration", sample_rate=64_000, sample_count=3_200)
        vibration_16k = resample_poly(vibration, up=1, down=4).astype(np.float32)
        current_1 = _series(data, "phase_current_1_A", sample_rate=64_000, sample_count=3_200)
        current_2 = _series(data, "phase_current_2_A", sample_rate=64_000, sample_count=3_200)
        current_1_16k = resample_poly(current_1, up=1, down=4).astype(np.float32)
        current_2_16k = resample_poly(current_2, up=1, down=4).astype(np.float32)
        current_1_features = _current_evidence(current_1_16k)
        current_2_features = _current_evidence(current_2_16k)
        current_mean_rms = (current_1_features["rms_a"] + current_2_features["rms_a"]) / 2.0
        imbalance = (
            0.0 if current_mean_rms <= 1e-20
            else abs(current_1_features["rms_a"] - current_2_features["rms_a"]) / current_mean_rms
        )
        speed = _series(data, "shaft_speed_rpm", sample_rate=4_000, sample_count=200)
        torque = _series(data, "load_torque_nm", sample_rate=4_000, sample_count=200)
        force = _series(data, "bearing_radial_load_n", sample_rate=4_000, sample_count=200)
        temperature = data.get("bearing_module_temperature_c")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not np.isfinite(temperature):
            raise ValueError("bearing_module_temperature_c must be finite")
        end_ns = raw_packet.get("end_generate_timestamp_ns")
        if isinstance(end_ns, bool) or not isinstance(end_ns, int) or end_ns <= 0:
            raise ValueError("end_generate_timestamp_ns must be positive")
        identity = {
            name: raw_packet.get(name)
            for name in ("device_id", "bearing_id", "task_id", "packet_id", "sender_id")
        }
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ValueError("raw packet identity is invalid")
        sequence = raw_packet.get("sequence_number")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("raw packet sequence_number is invalid")
        return {
            **identity,
            "sequence_number": sequence,
            "end_generate_timestamp_ns": end_ns,
            "feature_generated_at_ns": max(end_ns, time.time_ns()),
            "perception_quality": {"status": "good", "flags": []},
            "features": {
                "vibration": _vibration_evidence(vibration_16k),
                "phase_current_1": current_1_features,
                "phase_current_2": current_2_features,
                "current_relationship": {"current_imbalance_ratio": float(imbalance)},
                "operating_context": {
                    "shaft_speed_rpm": _statistics(speed),
                    "load_torque_nm": _statistics(torque),
                    "bearing_radial_load_n": _statistics(force),
                    "bearing_module_temperature_c": float(temperature),
                },
            },
        }


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise ValueError("raw packet %s must be an object" % name)
    return result


def _series(data: Mapping[str, Any], name: str, *, sample_rate: int, sample_count: int) -> np.ndarray:
    source = _mapping(data, name)
    if source.get("sample_rate_hz") != sample_rate or source.get("sample_count") != sample_count:
        raise ValueError("%s must be %d Hz / %d samples" % (name, sample_rate, sample_count))
    try:
        values = np.asarray(source["values"], dtype=np.float32)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("%s values must be numeric" % name) from exc
    if values.shape != (sample_count,) or not np.isfinite(values).all():
        raise ValueError("%s values must be finite %d-sample data" % (name, sample_count))
    return values


def _statistics(values: np.ndarray) -> dict[str, float]:
    return {
        "mean": float(np.mean(values, dtype=np.float64)),
        "last": float(values[-1]),
        "minimum": float(np.min(values)),
        "maximum": float(np.max(values)),
        "standard_deviation": float(np.std(values, dtype=np.float64)),
    }


def _current_evidence(values: np.ndarray) -> dict[str, float | int | str]:
    centered = values - np.mean(values, dtype=np.float64)
    return {
        "source_sample_rate_hz": 64_000,
        "analysis_sample_rate_hz": 16_000,
        "rms_a": float(np.sqrt(np.mean(centered * centered, dtype=np.float64))),
        "absolute_peak_a": float(np.max(np.abs(centered))),
        "unit": "A",
    }


def _vibration_evidence(values: np.ndarray) -> dict[str, float | int | str]:
    centered = values - np.mean(values, dtype=np.float64)
    power = np.abs(np.fft.rfft(centered)) ** 2
    frequencies = np.fft.rfftfreq(centered.size, d=1.0 / 16_000)
    non_dc_power = power[1:]
    total = float(np.sum(non_dc_power))
    if total <= 1e-20:
        dominant_frequency, band_ratio, entropy, kurtosis = 20.0, 0.0, 0.0, 0.0
    else:
        dominant_frequency = float(frequencies[1 + int(np.argmax(non_dc_power))])
        band = (frequencies >= 500.0) & (frequencies <= 2_000.0)
        band_ratio = float(np.sum(power[band]) / total)
        probabilities = non_dc_power / total
        positive = probabilities > 0
        entropy = float(-np.sum(probabilities[positive] * np.log(probabilities[positive])) / np.log(non_dc_power.size))
        standard_deviation = float(np.std(centered, dtype=np.float64))
        kurtosis = 0.0 if standard_deviation <= 1e-20 else float(np.mean((centered / standard_deviation) ** 4) - 3.0)
    return {
        "source_sample_rate_hz": 64_000,
        "analysis_sample_rate_hz": 16_000,
        "rms": float(np.sqrt(np.mean(centered * centered, dtype=np.float64))),
        "absolute_peak": float(np.max(np.abs(centered))),
        "kurtosis": kurtosis,
        "dominant_frequency_hz": max(20.0, min(8_000.0, dominant_frequency)),
        "band_power_ratio_500_2000": max(0.0, min(1.0, band_ratio)),
        "spectral_entropy": max(0.0, min(1.0, entropy)),
        "unit": "mm/s",
    }
