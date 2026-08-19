"""The frozen 19-dimensional physical-feature transform for H5."""

import numpy as np
from scipy import stats as scipy_stats
from scipy.signal import hilbert
from scipy.signal.windows import hann


def _compute_single(x: np.ndarray, sample_rate: int = 64_000) -> np.ndarray:
    """Compute the training-time 19D physical feature vector for one window."""
    eps = 1e-10
    x = np.asarray(x, dtype=np.float64)
    rms = np.sqrt(np.mean(x ** 2))
    std = np.std(x)
    max_abs = float(np.max(np.abs(x)))
    mean_abs = float(np.mean(np.abs(x)))
    centered = x - np.mean(x)
    spectrum = np.abs(np.fft.rfft(centered * hann(len(centered))))
    frequencies = np.fft.rfftfreq(len(centered), d=1.0 / sample_rate)
    power = spectrum ** 2
    total_power = np.sum(power)
    if total_power > eps:
        centroid = float(np.sum(frequencies * power) / total_power)
        spread = float(np.sqrt(np.sum(((frequencies - centroid) ** 2) * power) / total_power))
    else:
        centroid = spread = 0.0
    normalized_power = power / (total_power + eps)
    low = (frequencies >= 0) & (frequencies < 4_000)
    middle = (frequencies >= 4_000) & (frequencies < 12_000)
    high = (frequencies >= 12_000) & (frequencies <= 32_000)
    envelope = np.abs(hilbert(centered))
    envelope_spectrum = np.abs(np.fft.rfft(envelope - np.mean(envelope)))
    envelope_frequencies = np.fft.rfftfreq(len(envelope), d=1.0 / sample_rate)
    features = np.array(
        [
            rms, std, float(np.ptp(x)), float(scipy_stats.kurtosis(x, fisher=True)),
            float(scipy_stats.skew(x)), max_abs / (rms + eps),
            max_abs / (mean_abs + eps), rms / (mean_abs + eps), centroid, spread,
            float(-np.sum(normalized_power * np.log(normalized_power + eps))),
            float(frequencies[int(np.argmax(spectrum))]),
            float(np.sum(power[low]) / (total_power + eps)),
            float(np.sum(power[middle]) / (total_power + eps)),
            float(np.sum(power[high]) / (total_power + eps)),
            float(np.sqrt(np.mean(envelope ** 2))),
            float(scipy_stats.kurtosis(envelope, fisher=True)), float(np.max(envelope)),
            float(envelope_frequencies[int(np.argmax(envelope_spectrum))]),
        ],
        dtype=np.float32,
    )
    return np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)


def normalize_features(features: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return ((features - mean) / std).astype(np.float32)
