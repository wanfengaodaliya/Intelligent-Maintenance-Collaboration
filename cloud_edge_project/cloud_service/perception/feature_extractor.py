"""Single-packet feature extraction for preprocessed cloud signals."""

from __future__ import annotations

import cmath
from math import isfinite, log2, pi, sqrt
from typing import Any


_MAX_ANALYSIS_FREQUENCY_HZ = 8_000.0


def extract_single_packet_features(preprocessed: dict[str, Any]) -> dict[str, Any]:
    """Extract vibration and current features from one preprocessed packet."""

    signals = preprocessed["signals"]
    vibration = signals["vibration"]
    current_1 = signals["phase_current_1"]
    current_2 = signals["phase_current_2"]
    vibration_features = _vibration_features(vibration)
    current_1_features = _current_features(current_1)
    current_2_features = _current_features(current_2)

    rms_1 = current_1_features["rms"]
    rms_2 = current_2_features["rms"]
    current_rms_imbalance_ratio = _imbalance_ratio(rms_1, rms_2)
    return {
        "vibration": vibration_features,
        "phase_current_1": current_1_features,
        "phase_current_2": current_2_features,
        "current_rms_imbalance_ratio": current_rms_imbalance_ratio,
    }


def _vibration_features(signal: dict[str, Any]) -> dict[str, float | None]:
    time_domain = signal["time_domain"]
    frequencies, power = _power_spectrum(signal)
    limited_spectrum = [
        (frequency, value)
        for frequency, value in zip(frequencies, power)
        if 0.0 < frequency <= _MAX_ANALYSIS_FREQUENCY_HZ
    ]
    limited_frequencies = [frequency for frequency, _ in limited_spectrum]
    limited_power = [value for _, value in limited_spectrum]
    return {
        "rms": _rms(time_domain),
        "peak": _peak(time_domain),
        "kurtosis": _kurtosis(time_domain),
        "dominant_frequency_hz": _dominant_frequency(limited_frequencies, limited_power),
        "energy_ratio_500_2000hz": _energy_ratio(
            limited_frequencies,
            limited_power,
            500.0,
            2_000.0,
            float(signal["sample_rate_hz"]) / 2.0,
        ),
        "spectral_entropy": _spectral_entropy(limited_power),
    }


def _current_features(signal: dict[str, Any]) -> dict[str, float | None]:
    time_domain = signal["time_domain"]
    frequencies, power = _power_spectrum(signal)
    return {
        "rms": _rms(time_domain),
        "peak": _peak(time_domain),
        "fundamental_frequency_hz": _interpolated_peak_frequency(
            frequencies, power, 40.0, 60.0
        ),
    }


def _power_spectrum(signal: dict[str, Any]) -> tuple[list[float], list[float]]:
    frequency_domain = signal["frequency_domain"]
    sample_rate_hz = float(signal["sample_rate_hz"])
    spectrum = _fft([complex(value) for value in frequency_domain])
    size = len(frequency_domain)
    positive_indices = range(1, size // 2 + 1)
    frequencies = [sample_rate_hz * index / size for index in positive_indices]
    power = [abs(spectrum[index]) ** 2 for index in positive_indices]
    return frequencies, power


def _fft(values: list[complex]) -> list[complex]:
    """Return an exact DFT using radix decomposition where possible."""

    size = len(values)
    if size == 1:
        return values
    factor = _smallest_factor(size)
    if factor == size:
        return _direct_dft(values)

    segment_size = size // factor
    segment_spectra = [_fft(values[offset::factor]) for offset in range(factor)]
    result = [0j] * size
    for frequency in range(segment_size):
        combined = [
            segment_spectra[offset][frequency]
            * cmath.exp(-2j * pi * offset * frequency / size)
            for offset in range(factor)
        ]
        for group in range(factor):
            result[frequency + segment_size * group] = sum(
                combined[offset] * cmath.exp(-2j * pi * offset * group / factor)
                for offset in range(factor)
            )
    return result


def _smallest_factor(value: int) -> int:
    factor = 2
    while factor * factor <= value:
        if value % factor == 0:
            return factor
        factor += 1
    return value


def _direct_dft(values: list[complex]) -> list[complex]:
    size = len(values)
    return [
        sum(value * cmath.exp(-2j * pi * index * frequency / size) for index, value in enumerate(values))
        for frequency in range(size)
    ]


def _rms(values: list[float]) -> float:
    return sqrt(sum(value * value for value in values) / len(values))


def _peak(values: list[float]) -> float:
    return max(abs(value) for value in values)


def _kurtosis(values: list[float]) -> float | None:
    mean = sum(values) / len(values)
    centered = [value - mean for value in values]
    variance = sum(value * value for value in centered) / len(centered)
    if variance == 0.0:
        return None
    return sum(value**4 for value in centered) / len(centered) / variance**2


def _dominant_frequency(
    frequencies: list[float], power: list[float]
) -> float | None:
    valid_indices = [
        index for index, value in enumerate(power) if value > 0.0 and isfinite(value)
    ]
    if not valid_indices:
        return None
    return frequencies[max(valid_indices, key=power.__getitem__)]


def _energy_ratio(
    frequencies: list[float],
    power: list[float],
    low_hz: float,
    high_hz: float,
    nyquist_hz: float,
) -> float | None:
    if not frequencies or nyquist_hz < high_hz:
        return None
    total_energy = sum(power)
    if total_energy <= 0.0 or not isfinite(total_energy):
        return None
    band_energy = sum(
        value for frequency, value in zip(frequencies, power) if low_hz <= frequency <= high_hz
    )
    return band_energy / total_energy


def _spectral_entropy(power: list[float]) -> float | None:
    total_energy = sum(power)
    if total_energy <= 0.0 or not isfinite(total_energy) or len(power) <= 1:
        return None
    probabilities = [value / total_energy for value in power if value > 0.0]
    entropy = -sum(value * log2(value) for value in probabilities)
    return entropy / log2(len(power))


def _interpolated_peak_frequency(
    frequencies: list[float],
    power: list[float],
    low_hz: float,
    high_hz: float,
) -> float | None:
    """Estimate a spectral peak with three-point parabolic interpolation."""

    search_indices = [
        index
        for index, frequency in enumerate(frequencies)
        if low_hz <= frequency <= high_hz
    ]
    if not search_indices:
        return None
    peak_index = max(search_indices, key=power.__getitem__)
    if power[peak_index] <= 0.0 or not isfinite(power[peak_index]):
        return None

    bin_center = frequencies[peak_index]
    if peak_index == 0 or peak_index == len(power) - 1:
        return bin_center
    denominator = (
        power[peak_index - 1]
        - 2.0 * power[peak_index]
        + power[peak_index + 1]
    )
    if denominator == 0.0 or not isfinite(denominator):
        return bin_center
    delta = 0.5 * (
        power[peak_index - 1] - power[peak_index + 1]
    ) / denominator
    if not isfinite(delta) or abs(delta) > 1.0:
        return bin_center
    bin_width_hz = frequencies[peak_index] - frequencies[peak_index - 1]
    interpolated = bin_center + delta * bin_width_hz
    if not low_hz <= interpolated <= high_hz:
        return bin_center
    return interpolated


def _imbalance_ratio(rms_1: float, rms_2: float) -> float:
    average_rms = (rms_1 + rms_2) / 2.0
    if average_rms == 0.0:
        return 0.0
    return abs(rms_1 - rms_2) / average_rms
