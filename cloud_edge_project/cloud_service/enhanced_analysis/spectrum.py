"""FFT/PSD and deterministic spectral peak extraction."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.signal import find_peaks, welch

from .config import AnalysisConfig
from .contracts import limitation


EPS = 1e-12


def analyze_spectrum(x: np.ndarray, sample_rate_hz: int, config: AnalysisConfig) -> dict[str, Any]:
    values = np.asarray(x, dtype=np.float64)
    sample_count = values.size
    frequency_resolution_hz = sample_rate_hz / sample_count
    hann = np.hanning(sample_count)
    amplitude = 2.0 * np.abs(np.fft.rfft(values * hann)) / max(float(np.sum(hann)), EPS)
    frequencies = np.fft.rfftfreq(sample_count, d=1.0 / sample_rate_hz)

    segment_length = min(round(sample_rate_hz * config.psd_segment_seconds), sample_count)
    limitations: list[dict[str, str]] = []
    if segment_length >= sample_count and sample_count < sample_rate_hz * config.psd_segment_seconds:
        limitations.append(limitation("short_psd_segment", "window is shorter than one PSD segment"))
    if segment_length < 8:
        segment_length = sample_count
    noverlap = round(segment_length * config.psd_overlap_ratio)
    psd_freq, psd = welch(values, fs=sample_rate_hz, nperseg=segment_length, noverlap=noverlap)

    peaks = detect_spectral_peaks(
        frequencies,
        amplitude,
        frequency_resolution_hz,
        min_snr_db=config.peak_min_snr_db,
        min_distance_hz=config.peak_min_distance_hz,
        max_peaks=10,
    )
    band_energy = _band_energy(psd_freq, psd, sample_rate_hz)
    return {
        "frequency_resolution_hz": frequency_resolution_hz,
        "dominant_peaks_hz": [peak["frequency_hz"] for peak in peaks],
        "peaks": peaks,
        "band_energy": band_energy,
        "limitations": limitations,
    }


def detect_spectral_peaks(
    frequencies: np.ndarray,
    amplitude: np.ndarray,
    frequency_resolution_hz: float,
    *,
    min_snr_db: float,
    min_distance_hz: float,
    max_peaks: int,
    exclude_below_hz: float = 5.0,
) -> list[dict[str, Any]]:
    valid = (frequencies >= exclude_below_hz) & (frequencies <= frequencies.max())
    if not np.any(valid):
        return []
    valid_frequencies = frequencies[valid]
    valid_amplitude = amplitude[valid]
    distance = max(1, int(round(min_distance_hz / max(frequency_resolution_hz, EPS))))
    candidates, _ = find_peaks(valid_amplitude, distance=distance)
    found: list[dict[str, Any]] = []
    for index in candidates:
        frequency_hz = float(valid_frequencies[index])
        amplitude_value = float(valid_amplitude[index])
        window = (
            valid
            & (np.abs(frequencies - frequency_hz) <= max(50.0, 10.0 * frequency_resolution_hz))
            & (frequencies != frequency_hz)
        )
        if np.any(window):
            noise_floor = float(np.median(amplitude[window]))
        else:
            noise_floor = float(np.median(valid_amplitude))
        snr_db = 20.0 * np.log10(amplitude_value / max(noise_floor, EPS))
        if snr_db >= min_snr_db:
            found.append(
                {
                    "frequency_hz": frequency_hz,
                    "amplitude": amplitude_value,
                    "snr_db": float(snr_db),
                    "bin": int(np.flatnonzero(frequencies == frequency_hz)[0]),
                }
            )
    found.sort(key=lambda item: item["amplitude"], reverse=True)
    selected: list[dict[str, Any]] = []
    for candidate in found:
        if all(abs(candidate["frequency_hz"] - item["frequency_hz"]) >= min_distance_hz for item in selected):
            selected.append(candidate)
        if len(selected) >= max_peaks:
            break
    return selected


def _band_energy(
    frequencies: np.ndarray, psd: np.ndarray, sample_rate_hz: int
) -> dict[str, float]:
    nyquist = sample_rate_hz / 2.0
    bands = ((5.0, 100.0), (100.0, 1_000.0), (1_000.0, 5_000.0), (5_000.0, 20_000.0))
    result: dict[str, float] = {}
    for low, high in bands:
        upper = min(high, nyquist)
        mask = (frequencies >= low) & (frequencies < upper)
        if np.any(mask):
            result[f"{int(low)}_hz_{int(high)}_hz"] = float(
                np.trapz(psd[mask], frequencies[mask])
            )
        else:
            result[f"{int(low)}_hz_{int(high)}_hz"] = 0.0
    return result
