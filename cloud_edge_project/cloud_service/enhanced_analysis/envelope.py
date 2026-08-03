"""Envelope-demodulation spectrum analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.signal import butter, hilbert, sosfiltfilt

from .config import AnalysisConfig
from .contracts import BearingMetadata, limitation
from .spectrum import detect_spectral_peaks


def analyze_envelope_spectrum(
    x2: np.ndarray,
    sample_rate_hz: int,
    bearing: BearingMetadata | None,
    config: AnalysisConfig,
) -> dict[str, Any]:
    values = np.asarray(x2, dtype=np.float64)
    sample_count = values.size
    frequency_resolution_hz = sample_rate_hz / sample_count
    nyquist = sample_rate_hz / 2.0
    if bearing is not None and bearing.resonance_low_hz is not None and bearing.resonance_high_hz is not None:
        band = (float(bearing.resonance_low_hz), float(bearing.resonance_high_hz))
    else:
        band = config.default_resonance_band_hz
    low_hz, high_hz = band
    if low_hz <= 0.0 or high_hz <= low_hz or high_hz >= nyquist:
        return {
            "frequency_resolution_hz": frequency_resolution_hz,
            "dominant_peaks_hz": [],
            "peaks": [],
            "limitations": [
                limitation("INVALID_ENVELOPE_BAND", "configured envelope band is invalid")
            ],
        }

    sos = butter(4, [low_hz, high_hz], btype="bandpass", fs=sample_rate_hz, output="sos")
    try:
        filtered = sosfiltfilt(sos, values)
    except ValueError:
        return {
            "frequency_resolution_hz": frequency_resolution_hz,
            "dominant_peaks_hz": [],
            "peaks": [],
            "limitations": [
                limitation("insufficient_length_for_envelope", "window is too short for envelope filtering")
            ],
        }
    envelope = np.abs(hilbert(filtered))
    envelope = envelope - np.mean(envelope)
    hann = np.hanning(envelope.size)
    amplitude = 2.0 * np.abs(np.fft.rfft(envelope * hann)) / max(float(np.sum(hann)), 1e-12)
    frequencies = np.fft.rfftfreq(envelope.size, d=1.0 / sample_rate_hz)
    peaks = detect_spectral_peaks(
        frequencies,
        amplitude,
        frequency_resolution_hz,
        min_snr_db=config.peak_min_snr_db,
        min_distance_hz=config.peak_min_distance_hz,
        max_peaks=10,
    )
    return {
        "frequency_resolution_hz": frequency_resolution_hz,
        "dominant_peaks_hz": [peak["frequency_hz"] for peak in peaks],
        "peaks": peaks,
        "limitations": [],
    }
