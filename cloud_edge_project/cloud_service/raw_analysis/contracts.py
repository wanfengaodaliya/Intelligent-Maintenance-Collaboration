"""Minimal shared contracts for the raw_analysis signal operators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def limitation(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


@dataclass(frozen=True)
class BearingMetadata:
    rolling_element_count: int
    rolling_element_diameter_mm: float
    pitch_diameter_mm: float
    contact_angle_deg: float
    metadata_version: str
    resonance_low_hz: float | None = None
    resonance_high_hz: float | None = None

    def valid_geometry(self) -> bool:
        return (
            self.rolling_element_count >= 1
            and self.rolling_element_diameter_mm > 0.0
            and self.pitch_diameter_mm > self.rolling_element_diameter_mm
            and 0.0 <= self.contact_angle_deg < 90.0
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BearingMetadata":
        return cls(
            rolling_element_count=int(value["rolling_element_count"]),
            rolling_element_diameter_mm=float(value["rolling_element_diameter_mm"]),
            pitch_diameter_mm=float(value["pitch_diameter_mm"]),
            contact_angle_deg=float(value["contact_angle_deg"]),
            metadata_version=str(value.get("metadata_version", value.get("configuration_version", "unknown"))),
            resonance_low_hz=(
                float(value["resonance_low_hz"])
                if value.get("resonance_low_hz") is not None
                else None
            ),
            resonance_high_hz=(
                float(value["resonance_high_hz"])
                if value.get("resonance_high_hz") is not None
                else None
            ),
        )