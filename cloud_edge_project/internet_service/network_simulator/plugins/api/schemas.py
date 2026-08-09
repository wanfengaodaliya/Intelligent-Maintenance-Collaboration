"""Independent API response schemas; internal runtime objects never escape."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import DisconnectMode, ExperimentMode, LinkProtocol, LinkType, NetworkState


class ApiResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkParametersResponse(ApiResponseModel):
    state: NetworkState
    latency_ms: int | None = Field(default=None, ge=0)
    jitter_ms: int | None = Field(default=None, ge=0)
    bandwidth_kbps: int | None = Field(default=None, ge=0)
    packet_loss_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    disconnect_mode: DisconnectMode
    packet_loss_applied: bool


class LinkResponse(ApiResponseModel):
    link_id: str
    link_type: LinkType
    sender_id: str | None
    edge_id: str | None
    protocol: LinkProtocol
    proxy_name: str
    listen: str
    advertised_host: str
    advertised_port: int = Field(ge=1, le=65535)
    upstream: str
    current_state: NetworkState
    previous_state: NetworkState
    state_since_ns: int = Field(ge=0)
    applied_state_since_ns: int | None = Field(default=None, ge=0)
    seed: int = Field(ge=0)
    desired_parameters: NetworkParametersResponse | None
    applied_parameters: NetworkParametersResponse | None
    link_reliability_score: float = Field(ge=0, le=100, allow_inf_nan=False)
    score_components: dict[
        str,
        Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)],
    ]
    available: bool
    last_apply_success: bool
    last_apply_timestamp_ns: int | None = Field(default=None, ge=0)
    consecutive_apply_failures: int = Field(ge=0)
    error: str | None
    report_enabled: bool


class RuntimeResponse(ApiResponseModel):
    experiment_id: str
    mode: ExperimentMode
    tick: int = Field(ge=0)
    generated_at_ns: int = Field(ge=0)
    update_interval_seconds: float = Field(gt=0, allow_inf_nan=False)
    link_count: int = Field(ge=0)
    available_link_count: int = Field(ge=0)


class HealthResponse(ApiResponseModel):
    status: str
    toxiproxy_available: bool
    scheduler_reporter_healthy: bool
    link_count: int = Field(ge=0)
    available_link_count: int = Field(ge=0)
    last_tick: int = Field(ge=0)
