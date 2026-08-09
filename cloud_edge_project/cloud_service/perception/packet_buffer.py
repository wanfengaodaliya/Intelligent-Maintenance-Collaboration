"""Bounded, per-sender packet context and current-harmonic calculations."""

from __future__ import annotations

from math import cos, isfinite, pi, sqrt
from typing import Any

from cloud_service.perception.feature_extractor import _fft, _interpolated_peak_frequency
from cloud_service.perception.trend_analyzer import summarize_trends


_MAX_PACKETS_PER_SENDER = 200
_MIN_THD_WINDOW_MS = 200.0
_MIN_FUNDAMENTAL_WINDOW_MS = 1_000.0
_NANOSECONDS_PER_MILLISECOND = 1_000_000


class SenderPacketBuffer:
    """Keep recent packets per sender and expose only their continuous suffix.

    ``add`` accepts a packet with ``sender_id``, ``sequence_number``,
    ``preprocessed`` and ``single_packet_features``.  A top-level
    ``temperature`` is optional; otherwise it is read from the preprocessed
    edge operating context.
    """

    def __init__(self) -> None:
        self._max_packets_per_sender = _MAX_PACKETS_PER_SENDER
        self._packets_by_sender: dict[str, list[dict[str, Any]]] = {}
        self._continuous_start: dict[str, int] = {}

    def add(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Store one valid packet and return its continuous-context result."""

        normalized = _normalize_packet(packet)
        sender_id = normalized["sender_id"]
        packets = self._packets_by_sender.setdefault(sender_id, [])

        if packets:
            previous = packets[-1]
            if (
                previous["device_id"] != normalized["device_id"]
                or previous["bearing_id"] != normalized["bearing_id"]
            ):
                packets.clear()
                packets.append(normalized)
                self._continuous_start[sender_id] = 0
                return self._result("identity_changed", packets, aggregate=False)
            previous_sequence = previous["sequence_number"]
            sequence_number = normalized["sequence_number"]
            if sequence_number == previous_sequence:
                return self._result("duplicate", [normalized], aggregate=False)
            if sequence_number < previous_sequence:
                return self._result("out_of_order", [normalized], aggregate=False)
            timestamp_status = _timestamp_continuity_status(previous, normalized)
            if timestamp_status == "out_of_order":
                return self._result("out_of_order", [normalized], aggregate=False)
            if timestamp_status == "timestamp_overlap":
                return self._result(
                    "timestamp_overlap", [normalized], aggregate=False
                )
            if sequence_number > previous_sequence + 1:
                packets.append(normalized)
                self._continuous_start[sender_id] = len(packets) - 1
                self._trim(sender_id)
                return self._result(
                    "sequence_gap", self._current_window(sender_id), aggregate=False
                )
            if timestamp_status == "timestamp_gap":
                packets.append(normalized)
                self._continuous_start[sender_id] = len(packets) - 1
                self._trim(sender_id)
                return self._result(
                    "timestamp_gap", self._current_window(sender_id), aggregate=False
                )

        packets.append(normalized)
        self._continuous_start.setdefault(sender_id, len(packets) - 1)
        self._trim(sender_id)
        window = self._current_window(sender_id)
        status = "continuous" if _window_duration_ms(window) >= _MIN_THD_WINDOW_MS else "insufficient_context"
        return self._result(status, window, aggregate=status == "continuous")

    def _trim(self, sender_id: str) -> None:
        packets = self._packets_by_sender[sender_id]
        excess = len(packets) - self._max_packets_per_sender
        if excess <= 0:
            return
        del packets[:excess]
        self._continuous_start[sender_id] = max(0, self._continuous_start[sender_id] - excess)

    def _current_window(self, sender_id: str) -> list[dict[str, Any]]:
        return self._packets_by_sender[sender_id][self._continuous_start[sender_id] :]

    def _result(
        self, status: str, window: list[dict[str, Any]], *, aggregate: bool
    ) -> dict[str, Any]:
        return {
            "context_status": status,
            "packet_count": len(window),
            "start_timestamp_ns": window[0]["start_timestamp_ns"],
            "end_timestamp_ns": window[-1]["end_timestamp_ns"],
            "aggregated_features": summarize_trends(window) if aggregate else None,
            "thd": _calculate_harmonics(window) if aggregate else None,
        }


def _normalize_packet(packet: dict[str, Any]) -> dict[str, Any]:
    preprocessed = packet["preprocessed"]
    features = packet["single_packet_features"]
    raw_packet = preprocessed["cloud_raw_packet"]
    edge_context = preprocessed["edge_perception_result"]["features"]["operating_context"]
    temperature = packet.get("temperature", edge_context.get("bearing_module_temperature_c"))
    return {
        "device_id": packet["device_id"],
        "bearing_id": packet["bearing_id"],
        "sender_id": packet["sender_id"],
        "sequence_number": packet["sequence_number"],
        "start_timestamp_ns": raw_packet["start_timestamp_ns"],
        "end_timestamp_ns": raw_packet["end_timestamp_ns"],
        "temperature": temperature,
        "vibration": features["vibration"],
        "signals": preprocessed["signals"],
    }


def _window_duration_ms(window: list[dict[str, Any]]) -> float:
    return (
        window[-1]["end_timestamp_ns"] - window[0]["start_timestamp_ns"]
    ) / _NANOSECONDS_PER_MILLISECOND


def _timestamp_continuity_status(
    previous: dict[str, Any], current: dict[str, Any]
) -> str:
    if (
        current["start_timestamp_ns"] < previous["start_timestamp_ns"]
        or current["end_timestamp_ns"] <= previous["end_timestamp_ns"]
    ):
        return "out_of_order"
    if current["start_timestamp_ns"] < previous["end_timestamp_ns"]:
        return "timestamp_overlap"
    if current["start_timestamp_ns"] > previous["end_timestamp_ns"]:
        return "timestamp_gap"
    return "continuous"


def _calculate_harmonics(window: list[dict[str, Any]]) -> dict[str, dict[str, float | None]] | None:
    if _window_duration_ms(window) < _MIN_THD_WINDOW_MS:
        return None
    result = {
        "phase_current_1": _current_harmonics(window, "phase_current_1"),
        "phase_current_2": _current_harmonics(window, "phase_current_2"),
    }
    return result if any(value is not None for value in result.values()) else None


def _current_harmonics(window: list[dict[str, Any]], signal_name: str) -> dict[str, float | None] | None:
    signals = [packet["signals"][signal_name] for packet in window]
    sample_rates = {float(signal["sample_rate_hz"]) for signal in signals}
    if len(sample_rates) != 1:
        return None
    samples = [float(value) for signal in signals for value in signal["time_domain"]]
    if len(samples) < 2:
        return None
    mean = sum(samples) / len(samples)
    windowed = [
        (value - mean) * _hann_weight(index, len(samples))
        for index, value in enumerate(samples)
    ]
    spectrum = _fft([complex(value) for value in windowed])
    sample_rate = sample_rates.pop()
    half_size = len(samples) // 2
    positive_indices = range(1, half_size + 1)
    powers = [abs(spectrum[index]) ** 2 for index in positive_indices]
    frequencies = [
        sample_rate * index / len(samples) for index in positive_indices
    ]
    fundamental_frequency = _interpolated_peak_frequency(
        frequencies, powers, 40.0, 60.0
    )
    if fundamental_frequency is None:
        return None
    bin_width_hz = sample_rate / len(samples)
    fundamental_power = _band_power(
        frequencies, powers, fundamental_frequency, bin_width_hz
    )
    if fundamental_power <= 0.0 or not isfinite(fundamental_power):
        return None
    harmonic_power = 0.0
    available_harmonic_count = 0
    nyquist_hz = sample_rate / 2.0
    for harmonic in range(2, 11):
        target_frequency = harmonic * fundamental_frequency
        if target_frequency > nyquist_hz:
            continue
        band_power = _band_power(
            frequencies, powers, target_frequency, bin_width_hz
        )
        if not isfinite(band_power):
            return None
        harmonic_power += band_power
        available_harmonic_count += 1
    if available_harmonic_count == 0:
        return None
    thd = sqrt(harmonic_power / fundamental_power)
    if not isfinite(thd):
        return None
    result = {"total_harmonic_distortion": thd}
    if _window_duration_ms(window) >= _MIN_FUNDAMENTAL_WINDOW_MS:
        result["fundamental_frequency_hz"] = fundamental_frequency
    return result


def _hann_weight(index: int, size: int) -> float:
    if size <= 1:
        return 1.0
    return 0.5 - 0.5 * cos(2.0 * pi * index / (size - 1))


def _band_power(
    frequencies: list[float],
    powers: list[float],
    center_hz: float,
    half_width_hz: float,
) -> float:
    return sum(
        power
        for frequency, power in zip(frequencies, powers)
        if abs(frequency - center_hz) <= half_width_hz
    )
