"""Preprocessing for validated cloud review packets."""

from __future__ import annotations

from math import cos, pi
from typing import Any


_SIGNAL_NAMES = ("vibration", "phase_current_1", "phase_current_2")


def preprocess_packet(request: dict[str, Any]) -> dict[str, Any]:
    """Copy validated signals and prepare time- and frequency-domain inputs.

    ``request`` is expected to have passed ``validate_cloud_review_request``.
    The original payload is not mutated.
    """

    raw_packet = request["cloud_raw_packet"]
    prepared_signals: dict[str, dict[str, Any]] = {}
    for name in _SIGNAL_NAMES:
        signal = raw_packet["signals"][name]
        time_domain = [float(value) for value in signal["values"]]
        dc_offset = sum(time_domain) / len(time_domain)
        frequency_domain = [
            (value - dc_offset) * _hann_weight(index, len(time_domain))
            for index, value in enumerate(time_domain)
        ]
        prepared_signals[name] = {
            "sample_rate_hz": signal["sample_rate_hz"],
            "sample_count": signal["sample_count"],
            "time_domain": time_domain,
            "dc_offset": dc_offset,
            "frequency_domain": frequency_domain,
        }

    return {
        "edge_perception_result": request["edge_perception_result"],
        "cloud_raw_packet": {
            key: value for key, value in raw_packet.items() if key != "signals"
        },
        "signals": prepared_signals,
    }


def _hann_weight(index: int, size: int) -> float:
    if size <= 1:
        return 1.0
    return 0.5 - 0.5 * cos(2.0 * pi * index / (size - 1))
