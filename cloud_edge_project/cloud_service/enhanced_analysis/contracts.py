"""Contracts shared by the enhanced-analysis submodules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


REQUIRED_CHANNELS = ("vibration", "phase_current_1_A", "phase_current_2_A")
REQUIRED_NPZ_KEYS = REQUIRED_CHANNELS + ("relative_positions", "packet_start_samples", "sample_rate_hz")


class EnhancedAnalysisError(ValueError):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


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


@dataclass(frozen=True)
class LoadedWindow:
    channels: dict[str, Any]
    relative_positions: tuple[int, ...]
    packet_start_samples: tuple[int, ...]
    sample_rate_hz: int
    start_timestamp_ns: int | None
    speed_rpm: float | None
    radial_load_n: float | None
    bearing: BearingMetadata | None
    limitations: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedWindow:
    x0: dict[str, Any]
    x2: dict[str, Any]
    x3: dict[str, Any]
    available: dict[str, bool]
    limitations: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisContext:
    review_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    anchor_packet_id: str
    aggregation_result_id: str
    context_status: str
    preprocessed_window_path: str
    preprocessed_window_sha256: str
    sample_rate_hz: int
    sample_count: int
    window_duration_ms: int
    frequency_resolution_hz: float
    relative_positions: tuple[int, ...]
    packet_start_samples: tuple[int, ...]
    start_timestamp_ns: int | None
    speed_rpm: float | None
    radial_load_n: float | None
    bearing: BearingMetadata | None
    limitations: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class EnhancedAnalysisResult:
    producer: str
    review_id: str
    status: str
    context_status: str
    algorithm_version: str
    config_version: str
    input: dict[str, Any]
    data_quality: dict[str, Any]
    signal_evidence: dict[str, Any]
    history_evidence: dict[str, Any]
    model_evidence: dict[str, Any]
    operating_conditions: dict[str, Any]
    limitations: list[dict[str, str]]
    created_at_ns: int
    suggested_review_required: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnhancedAnalysisResult":
        return cls(**dict(value))
