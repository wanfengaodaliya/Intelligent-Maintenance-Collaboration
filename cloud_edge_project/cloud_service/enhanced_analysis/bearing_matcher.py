"""Bearing characteristic-frequency matching and physical evidence."""

from __future__ import annotations

from math import cos, pi
from typing import Any

from .config import AnalysisConfig
from .contracts import BearingMetadata, limitation


HYPOTHESES = {
    "ftf_hz": "cage_fault",
    "bpfo_hz": "outer_race_fault",
    "bpfi_hz": "inner_race_fault",
    "bsf_hz": "rolling_element_fault",
}
HARMONIC_WEIGHTS = {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.4}


def calculate_characteristic_frequencies(
    *, speed_rpm: float, bearing: BearingMetadata
) -> dict[str, float]:
    if speed_rpm <= 0.0:
        raise ValueError("speed_rpm must be positive")
    if not bearing.valid_geometry():
        raise ValueError("bearing geometry is invalid")
    shaft_frequency_hz = speed_rpm / 60.0
    ratio = (
        bearing.rolling_element_diameter_mm / bearing.pitch_diameter_mm
    ) * cos(bearing.contact_angle_deg * pi / 180.0)
    return {
        "ftf_hz": 0.5 * shaft_frequency_hz * (1.0 - ratio),
        "bpfo_hz": 0.5 * bearing.rolling_element_count * shaft_frequency_hz * (1.0 - ratio),
        "bpfi_hz": 0.5 * bearing.rolling_element_count * shaft_frequency_hz * (1.0 + ratio),
        "bsf_hz": (
            bearing.pitch_diameter_mm
            / (2.0 * bearing.rolling_element_diameter_mm)
            * shaft_frequency_hz
            * (1.0 - ratio**2)
        ),
    }


def match_bearing_frequencies(
    *,
    envelope_peaks: list[dict[str, Any]],
    spectrum_peaks: list[dict[str, Any]],
    frequency_resolution_hz: float,
    speed_rpm: float | None,
    bearing: BearingMetadata | None,
    context_status: str,
    config: AnalysisConfig,
    quality_good: bool,
) -> dict[str, Any]:
    limitations: list[dict[str, str]] = []
    if bearing is None:
        return {
            "evidence": [],
            "limitations": [
                limitation("bearing_metadata_missing", "bearing geometry metadata is missing")
            ],
        }
    if speed_rpm is None or speed_rpm <= 0.0:
        return {
            "evidence": [],
            "limitations": [limitation("speed_unavailable", "operating speed is unavailable")],
        }
    try:
        targets = calculate_characteristic_frequencies(speed_rpm=speed_rpm, bearing=bearing)
    except ValueError:
        return {
            "evidence": [],
            "limitations": [
                limitation("bearing_metadata_missing", "bearing geometry metadata is invalid")
            ],
        }

    evidence: list[dict[str, Any]] = []
    for frequency_key, hypothesis in HYPOTHESES.items():
        target = targets[frequency_key]
        if target <= 0.0:
            continue
        matched_harmonics, matched_sidebands = _match_harmonics(
            target,
            speed_rpm,
            frequency_resolution_hz,
            envelope_peaks,
            spectrum_peaks,
            config,
        )
        if not matched_harmonics:
            continue
        base_score = _base_score(matched_harmonics, config.harmonic_max_order)
        bonus = 0.10 if _has_adjacent_harmonics(matched_harmonics) else 0.0
        score = min(1.0, base_score + bonus)
        orders = {item["order"] for item in matched_harmonics}
        if score < config.min_match_score or not (1 in orders or 2 in orders):
            continue
        if score >= 0.80 and quality_good:
            confidence_level = "high"
        elif score >= 0.60:
            confidence_level = "medium"
        else:
            confidence_level = "candidate"
        if context_status == "partial_context" and confidence_level == "high":
            confidence_level = "medium"
            limitations.append(
                limitation("partial_context_confidence_capped", "partial context caps high evidence")
            )
        evidence.append(
            {
                "evidence_id": f"bearing_match_{frequency_key.replace('_hz', '')}_{len(evidence) + 1:02d}",
                "hypothesis": hypothesis,
                "source": matched_harmonics[0]["source"],
                "target_frequency_hz": target,
                "matched_harmonics": matched_harmonics,
                "matched_sidebands": matched_sidebands,
                "score": score,
                "confidence_level": confidence_level,
                "sideband_bonus": 0.0,
            }
        )
    return {"evidence": evidence, "limitations": limitations}


def _match_harmonics(
    target: float,
    speed_rpm: float,
    frequency_resolution_hz: float,
    envelope_peaks: list[dict[str, Any]],
    spectrum_peaks: list[dict[str, Any]],
    config: AnalysisConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matched: list[dict[str, Any]] = []
    sidebands: list[dict[str, Any]] = []
    shaft_frequency_hz = speed_rpm / 60.0
    for order in range(1, config.harmonic_max_order + 1):
        expected = target * order
        tolerance = max(
            config.tolerance_resolution_multiplier * frequency_resolution_hz,
            config.tolerance_ratio * expected,
        )
        hit = _best_peak(expected, tolerance, envelope_peaks, spectrum_peaks)
        if hit is None:
            continue
        matched.append(
            {
                "order": order,
                "peak_hz": hit["frequency_hz"],
                "snr_db": hit["snr_db"],
                "source": hit["source"],
            }
        )
        sideband_hit = _best_peak(
            expected + shaft_frequency_hz,
            tolerance,
            envelope_peaks,
            spectrum_peaks,
        )
        if sideband_hit is not None:
            sidebands.append(
                {
                    "order": order,
                    "kind": "upper_sideband",
                    "peak_hz": sideband_hit["frequency_hz"],
                    "snr_db": sideband_hit["snr_db"],
                }
            )
        sideband_hit = _best_peak(
            expected - shaft_frequency_hz,
            tolerance,
            envelope_peaks,
            spectrum_peaks,
        )
        if sideband_hit is not None:
            sidebands.append(
                {
                    "order": order,
                    "kind": "lower_sideband",
                    "peak_hz": sideband_hit["frequency_hz"],
                    "snr_db": sideband_hit["snr_db"],
                }
            )
    return matched, sidebands


def _best_peak(
    expected: float,
    tolerance: float,
    envelope_peaks: list[dict[str, Any]],
    spectrum_peaks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        {**peak, "source": "envelope_spectrum"}
        for peak in envelope_peaks
        if abs(peak["frequency_hz"] - expected) <= tolerance
    ] + [
        {**peak, "source": "spectrum"}
        for peak in spectrum_peaks
        if abs(peak["frequency_hz"] - expected) <= tolerance
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (abs(item["frequency_hz"] - expected), item["source"] == "spectrum"))


def _base_score(matched_harmonics: list[dict[str, Any]], max_order: int) -> float:
    total_weight = sum(HARMONIC_WEIGHTS[order] for order in range(1, max_order + 1))
    hit_weight = sum(HARMONIC_WEIGHTS[item["order"]] for item in matched_harmonics)
    return hit_weight / max(total_weight, 1e-12)


def _has_adjacent_harmonics(matched_harmonics: list[dict[str, Any]]) -> bool:
    orders = sorted(item["order"] for item in matched_harmonics)
    return any(b - a == 1 for a, b in zip(orders, orders[1:]))
