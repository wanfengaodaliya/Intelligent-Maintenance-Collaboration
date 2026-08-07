"""Strict wire schemas shared by Reporter and the local Fake Scheduler."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.enums import LinkProtocol, NetworkState


class StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkLinkReport(StrictWireModel):
    link_id: str = Field(min_length=1)
    sender_id: str | None
    edge_id: str | None
    protocol: LinkProtocol
    current_state: NetworkState
    state_since_ns: int = Field(ge=0)
    state_duration_ms: int = Field(ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    jitter_ms: int | None = Field(default=None, ge=0)
    bandwidth_kbps: int | None = Field(default=None, ge=0)
    packet_loss_percent: float | None = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    link_reliability_score: float = Field(ge=0, le=100, allow_inf_nan=False)
    available: bool
    last_apply_success: bool
    last_apply_timestamp_ns: int | None = Field(default=None, ge=0)
    consecutive_apply_failures: int = Field(ge=0)
    error: str | None


class NetworkReport(StrictWireModel):
    schema_version: str = Field(pattern=r"^1\.0$")
    experiment_id: str = Field(min_length=1)
    reporter_id: str = Field(min_length=1)
    report_sequence: int = Field(ge=1)
    generated_at: datetime
    generated_at_ns: int = Field(ge=0)
    update_interval_seconds: float = Field(gt=0, allow_inf_nan=False)
    links: tuple[NetworkLinkReport, ...]

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value


class ReportAcknowledgement(StrictWireModel):
    accepted: bool
    report_sequence: int = Field(ge=1)
