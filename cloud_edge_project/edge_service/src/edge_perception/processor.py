# -*- coding: utf-8 -*-
"""同步降采样、质量判断与单包轴承特征提取。"""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .config import PerceptionConfig, file_sha256
from .contracts import (
    DOWNSAMPLING_FAILED,
    PERCEPTION_FAILED,
    ModuleResult,
    PerceptionInvocationContext,
)


_IDENTITY_FIELDS = (
    "device_id",
    "bearing_id",
    "task_id",
    "packet_id",
    "sender_id",
)
_HIGH_CHANNELS = (
    ("vibration", "mm/s"),
    ("phase_current_1_A", "A"),
    ("phase_current_2_A", "A"),
)
_OPERATING_CHANNELS = (
    "shaft_speed_rpm",
    "load_torque_nm",
    "bearing_radial_load_n",
)
_FLAG_BY_CHANNEL = {
    "vibration": "VIBRATION_CONSTANT_SIGNAL",
    "phase_current_1_A": "PHASE_CURRENT_1_CONSTANT_SIGNAL",
    "phase_current_2_A": "PHASE_CURRENT_2_CONSTANT_SIGNAL",
}
_RESULT_TOP_LEVEL = {
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


class _ProcessingError(ValueError):
    def __init__(self, scope: str, expected: object, actual: object):
        super().__init__(scope)
        self.scope = scope
        self.expected = expected
        self.actual = actual


class EdgePerception:
    """线程安全的无跨包状态感知处理器。"""

    def __init__(
        self,
        config: PerceptionConfig,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        on_error: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        errors = config.validate()
        if errors:
            raise ValueError("感知配置无效: " + "; ".join(errors))
        if config.runtime_dependencies.get("numpy") != np.__version__:
            raise ValueError(
                "numpy运行时版本与配置不一致: "
                f"expected={config.runtime_dependencies.get('numpy')}, actual={np.__version__}"
            )
        self.config = config
        self._clock_ns = clock_ns
        self._on_error = on_error or (lambda _: None)
        self._fir = self._load_and_validate_fir(Path(config.fir_coefficients_path))
        self._counter_lock = threading.Lock()
        self._near_zero_current_count = 0

    @property
    def near_zero_current_count(self) -> int:
        with self._counter_lock:
            return self._near_zero_current_count

    def downsample(
        self, raw_packet: dict[str, Any], context: PerceptionInvocationContext
    ) -> ModuleResult:
        try:
            self._validate_context(context)
            identity = self._read_identity(raw_packet)
            data = raw_packet["data"]
            output_data: dict[str, Any] = {}

            for channel, unit in _HIGH_CHANNELS:
                source = data[channel]
                values = _float_array(source["values"], 3200, channel)
                if source.get("sample_rate_hz") != 64000 or source.get("sample_count") != 3200:
                    raise _ProcessingError(f"{channel}.source_spec", "64000Hz/3200", _spec(source))
                if source.get("unit") != unit:
                    raise _ProcessingError(f"{channel}.unit", unit, source.get("unit"))
                filtered = np.convolve(
                    np.pad(values, (184, 184), mode="reflect"), self._fir, mode="valid"
                )
                reduced = _readonly(filtered[::4].astype(np.float64, copy=True))
                if reduced.shape != (800,) or not np.isfinite(reduced).all():
                    raise _ProcessingError(f"{channel}.downsampled", "800 finite values", reduced.shape)
                output_data[channel] = {
                    "unit": unit,
                    "source_sample_rate_hz": 64000,
                    "analysis_sample_rate_hz": 16000,
                    "sample_count": 800,
                    "values": reduced,
                }

            for channel in _OPERATING_CHANNELS:
                source = data[channel]
                values = _float_array(source["values"], 200, channel)
                if source.get("sample_rate_hz") != 4000 or source.get("sample_count") != 200:
                    raise _ProcessingError(f"{channel}.source_spec", "4000Hz/200", _spec(source))
                output_data[channel] = {
                    "sample_rate_hz": 4000,
                    "sample_count": 200,
                    "values": _readonly(values.copy()),
                }

            temperature = data["bearing_module_temperature_c"]
            if not _finite_number(temperature):
                raise _ProcessingError("bearing_module_temperature_c", "finite number", type(temperature).__name__)
            output_data["bearing_module_temperature_c"] = float(temperature)
            return ModuleResult.succeeded({**identity, "data": output_data})
        except Exception as exc:  # module boundary: never return partial payload
            self._emit_error("downsampling", DOWNSAMPLING_FAILED, raw_packet, context, exc)
            return ModuleResult.failed(DOWNSAMPLING_FAILED)

    def perceive(
        self, packet: dict[str, Any], context: PerceptionInvocationContext
    ) -> ModuleResult:
        try:
            self._validate_context(context)
            identity = self._read_identity(packet)
            data = packet["data"]
            arrays = self._validate_downsampled_data(data)

            speed = arrays["shaft_speed_rpm"]
            speed_mean = float(np.mean(speed, dtype=np.float64))
            running = speed_mean >= self.config.running_speed_threshold_rpm
            flags: list[str] = []
            if not running:
                flags.append("DEVICE_NOT_RUNNING")
            else:
                for channel, _ in _HIGH_CHANNELS:
                    rule = self.config.constant_detection[channel]
                    if rule.enabled:
                        peak_to_peak = float(np.ptp(arrays[channel]))
                        if peak_to_peak <= float(rule.threshold):
                            flags.append(_FLAG_BY_CHANNEL[channel])

            centered = {
                channel: values - np.mean(values, dtype=np.float64)
                for channel, values in arrays.items()
                if channel in {name for name, _ in _HIGH_CHANNELS}
            }
            vibration_features = self._vibration_features(centered["vibration"])
            current_1 = self._current_features(centered["phase_current_1_A"])
            current_2 = self._current_features(centered["phase_current_2_A"])
            current_mean_rms = (current_1["rms_a"] + current_2["rms_a"]) / 2.0
            if current_mean_rms <= self.config.current_relationship_zero_rms_threshold:
                imbalance = 0.0
                with self._counter_lock:
                    self._near_zero_current_count += 1
            else:
                imbalance = abs(current_1["rms_a"] - current_2["rms_a"]) / current_mean_rms

            features = {
                "vibration": vibration_features,
                "phase_current_1": current_1,
                "phase_current_2": current_2,
                "current_relationship": {"current_imbalance_ratio": float(imbalance)},
                "operating_context": {
                    "shaft_speed_rpm": _series_stats(speed, known_mean=speed_mean),
                    "load_torque_nm": _series_stats(arrays["load_torque_nm"]),
                    "bearing_radial_load_n": _series_stats(arrays["bearing_radial_load_n"]),
                    "bearing_module_temperature_c": float(data["bearing_module_temperature_c"]),
                },
            }
            draft = {
                **identity,
                "perception_quality": {
                    "status": "warning" if flags else "good",
                    "flags": flags,
                },
                "features": features,
            }
            self._validate_result(draft, include_generated_time=False)
            generated_at = self._clock_ns()
            if (
                isinstance(generated_at, bool)
                or not isinstance(generated_at, int)
                or generated_at <= 0
                or generated_at < context.perception_received_at_ns
            ):
                raise _ProcessingError(
                    "feature_generated_at_ns",
                    f"integer >= {context.perception_received_at_ns}",
                    generated_at,
                )
            result = {**draft, "feature_generated_at_ns": generated_at}
            self._validate_result(result, include_generated_time=True)
            return ModuleResult.succeeded(result)
        except Exception as exc:  # module boundary: never return partial payload
            self._emit_error("perception", PERCEPTION_FAILED, packet, context, exc)
            return ModuleResult.failed(PERCEPTION_FAILED)

    def _load_and_validate_fir(self, path: Path) -> np.ndarray:
        actual_hash = file_sha256(path)
        if actual_hash.lower() != self.config.fir_sha256.lower():
            raise ValueError("FIR系数资产SHA-256不匹配")
        coefficients = np.loadtxt(path, dtype=np.float64)
        if coefficients.shape != (369,) or not np.isfinite(coefficients).all():
            raise ValueError("FIR系数必须包含369个有限float64")
        if not np.allclose(
            coefficients,
            coefficients[::-1],
            rtol=self.config.relative_tolerance,
            atol=self.config.absolute_tolerance,
        ):
            raise ValueError("FIR系数不对称")
        if not math.isclose(
            float(np.sum(coefficients, dtype=np.float64)),
            1.0,
            rel_tol=self.config.relative_tolerance,
            abs_tol=self.config.absolute_tolerance,
        ):
            raise ValueError("FIR系数直流增益不是1")
        response = np.abs(np.fft.rfft(coefficients, 131072))
        frequencies = np.fft.rfftfreq(131072, d=1.0 / 64000.0)
        passband = 20.0 * np.log10(np.maximum(response[frequencies <= 7000.0], 1e-300))
        stopband = 20.0 * np.log10(np.maximum(response[frequencies >= 8000.0], 1e-300))
        if float(np.max(passband) - np.min(passband)) > 0.1:
            raise ValueError("FIR通带波动超过0.1 dB")
        if float(np.max(stopband)) > -80.0:
            raise ValueError("FIR阻带衰减不足80 dB")
        return _readonly(coefficients.copy())

    @staticmethod
    def _validate_context(context: PerceptionInvocationContext) -> None:
        if not isinstance(context.edge_node_id, str) or not context.edge_node_id.strip():
            raise _ProcessingError("edge_node_id", "non-empty string", context.edge_node_id)
        received = context.perception_received_at_ns
        if isinstance(received, bool) or not isinstance(received, int) or received <= 0:
            raise _ProcessingError("perception_received_at_ns", "positive integer", received)

    @staticmethod
    def _read_identity(packet: dict[str, Any]) -> dict[str, Any]:
        identity: dict[str, Any] = {}
        for field in _IDENTITY_FIELDS:
            value = packet.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _ProcessingError(field, "non-empty string", value)
            identity[field] = value
        sequence = packet.get("sequence_number")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise _ProcessingError("sequence_number", "integer", sequence)
        timestamp = packet.get("end_generate_timestamp_ns")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise _ProcessingError("end_generate_timestamp_ns", "positive integer", timestamp)
        identity["sequence_number"] = sequence
        identity["end_generate_timestamp_ns"] = timestamp
        return identity

    @staticmethod
    def _validate_downsampled_data(data: dict[str, Any]) -> dict[str, np.ndarray]:
        arrays: dict[str, np.ndarray] = {}
        for channel, unit in _HIGH_CHANNELS:
            source = data[channel]
            if (
                source.get("source_sample_rate_hz") != 64000
                or source.get("analysis_sample_rate_hz") != 16000
                or source.get("sample_count") != 800
                or source.get("unit") != unit
            ):
                raise _ProcessingError(f"{channel}.downsampled_spec", "64000/16000/800/valid unit", _spec(source))
            arrays[channel] = _float_array(source["values"], 800, channel)
        for channel in _OPERATING_CHANNELS:
            source = data[channel]
            if source.get("sample_rate_hz") != 4000 or source.get("sample_count") != 200:
                raise _ProcessingError(f"{channel}.spec", "4000Hz/200", _spec(source))
            arrays[channel] = _float_array(source["values"], 200, channel)
        if not _finite_number(data.get("bearing_module_temperature_c")):
            raise _ProcessingError("bearing_module_temperature_c", "finite number", type(data.get("bearing_module_temperature_c")).__name__)
        return arrays

    def _vibration_features(self, values: np.ndarray) -> dict[str, Any]:
        squared = values * values
        m2 = float(np.mean(squared, dtype=np.float64))
        if m2 <= self.config.feature_zero_power_threshold:
            raise _ProcessingError("INSUFFICIENT_SIGNAL_POWER", "> feature_zero_power_threshold", m2)
        rms = math.sqrt(m2)
        m4 = float(np.mean(squared * squared, dtype=np.float64))
        kurtosis = m4 / (m2 * m2)

        n = values.size
        window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n, dtype=np.float64) / n)
        spectrum = np.fft.rfft(values * window)
        psd = (np.abs(spectrum) ** 2) / (16000.0 * float(np.sum(window * window)))
        psd[1:-1] *= 2.0
        non_dc = psd[1:]
        spectral_sum = float(np.sum(non_dc, dtype=np.float64))
        non_dc_power = 20.0 * spectral_sum
        if non_dc_power <= self.config.feature_zero_power_threshold:
            raise _ProcessingError("INSUFFICIENT_SIGNAL_POWER", "> feature_zero_power_threshold", non_dc_power)
        dominant_index = int(np.argmax(non_dc)) + 1
        frequencies = np.fft.rfftfreq(n, d=1.0 / 16000.0)
        band = (frequencies >= 500.0) & (frequencies <= 2000.0)
        band_ratio = float(np.sum(psd[band], dtype=np.float64) / spectral_sum)
        probabilities = non_dc / spectral_sum
        positive = probabilities > 0
        entropy = float(
            -np.sum(probabilities[positive] * np.log(probabilities[positive]), dtype=np.float64)
            / math.log(non_dc.size)
        )
        result = {
            "source_sample_rate_hz": 64000,
            "analysis_sample_rate_hz": 16000,
            "rms": float(rms),
            "absolute_peak": float(np.max(np.abs(values))),
            "kurtosis": float(kurtosis),
            "dominant_frequency_hz": float(frequencies[dominant_index]),
            "band_power_ratio_500_2000": float(np.clip(band_ratio, 0.0, 1.0)),
            "spectral_entropy": float(np.clip(entropy, 0.0, 1.0)),
            "unit": "mm/s",
        }
        if not _all_finite(result):
            raise _ProcessingError("vibration.features", "finite values", "non-finite")
        return result

    @staticmethod
    def _current_features(values: np.ndarray) -> dict[str, Any]:
        rms = math.sqrt(float(np.mean(values * values, dtype=np.float64)))
        result = {
            "source_sample_rate_hz": 64000,
            "analysis_sample_rate_hz": 16000,
            "rms_a": float(rms),
            "absolute_peak_a": float(np.max(np.abs(values))),
            "unit": "A",
        }
        if not _all_finite(result):
            raise _ProcessingError("current.features", "finite values", "non-finite")
        return result

    @staticmethod
    def _validate_result(result: dict[str, Any], *, include_generated_time: bool) -> None:
        expected_top = _RESULT_TOP_LEVEL if include_generated_time else _RESULT_TOP_LEVEL - {"feature_generated_at_ns"}
        if set(result) != expected_top:
            raise _ProcessingError("PerceptionResult.top_level", sorted(expected_top), sorted(result))
        quality = result["perception_quality"]
        if quality.get("status") not in {"good", "warning"}:
            raise _ProcessingError("perception_quality.status", "good|warning", quality.get("status"))
        flags = quality.get("flags")
        allowed = {"DEVICE_NOT_RUNNING", *_FLAG_BY_CHANNEL.values()}
        if not isinstance(flags, list) or len(flags) != len(set(flags)) or not set(flags) <= allowed:
            raise _ProcessingError("perception_quality.flags", sorted(allowed), flags)
        if (quality["status"] == "good") != (not flags):
            raise _ProcessingError("perception_quality", "good iff flags empty", quality)
        if not _all_finite(result["features"]):
            raise _ProcessingError("features", "all finite numeric values", "non-finite")
        vibration = result["features"]["vibration"]
        if not (20.0 <= vibration["dominant_frequency_hz"] <= 8000.0):
            raise _ProcessingError("dominant_frequency_hz", "20..8000", vibration["dominant_frequency_hz"])
        for field in ("band_power_ratio_500_2000", "spectral_entropy"):
            if not 0.0 <= vibration[field] <= 1.0:
                raise _ProcessingError(field, "0..1", vibration[field])

    def _emit_error(
        self,
        stage: str,
        error_code: str,
        packet: object,
        context: PerceptionInvocationContext,
        exc: Exception,
    ) -> None:
        source = packet if isinstance(packet, dict) else {}
        if isinstance(exc, _ProcessingError):
            scope, expected, actual = exc.scope, exc.expected, exc.actual
        else:
            scope, expected, actual = "UNEXPECTED_ERROR", "successful processing", type(exc).__name__
        log = {
            field: source.get(field) for field in _IDENTITY_FIELDS
        }
        log.update(
            {
                "edge_node_id": getattr(context, "edge_node_id", None),
                "perception_received_at_ns": getattr(context, "perception_received_at_ns", None),
                "log_generated_at_ns": self._clock_ns(),
                "stage": stage,
                "scope": scope,
                "error_code": error_code,
                "expected": expected,
                "actual": actual,
                "action": "processing_stopped",
            }
        )
        try:
            self._on_error(log)
        except Exception:
            pass


def _float_array(values: object, expected_size: int, scope: str) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise _ProcessingError(scope, f"float64[{expected_size}]", type(values).__name__) from exc
    if array.shape != (expected_size,) or not np.isfinite(array).all():
        raise _ProcessingError(scope, f"{expected_size} finite values", array.shape)
    return array


def _readonly(array: np.ndarray) -> np.ndarray:
    array.setflags(write=False)
    return array


def _series_stats(values: np.ndarray, *, known_mean: Optional[float] = None) -> dict[str, float]:
    mean = float(known_mean) if known_mean is not None else float(np.mean(values, dtype=np.float64))
    return {
        "mean": mean,
        "last": float(values[-1]),
        "minimum": float(np.min(values)),
        "maximum": float(np.max(values)),
        "standard_deviation": float(np.std(values, dtype=np.float64, ddof=0)),
    }


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float, np.number)) and math.isfinite(float(value))


def _all_finite(value: object) -> bool:
    if isinstance(value, dict):
        return all(_all_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_all_finite(item) for item in value)
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, np.number)):
        return math.isfinite(float(value))
    return isinstance(value, str)


def _spec(source: object) -> object:
    if not isinstance(source, dict):
        return type(source).__name__
    return {
        key: source.get(key)
        for key in ("unit", "sample_rate_hz", "sample_count", "source_sample_rate_hz", "analysis_sample_rate_hz")
        if key in source
    }
