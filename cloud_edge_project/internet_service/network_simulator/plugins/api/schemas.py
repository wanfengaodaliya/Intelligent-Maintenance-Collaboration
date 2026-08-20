"""Independent API response schemas; internal runtime objects never escape."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import DisconnectMode, ExperimentMode, LinkProtocol, LinkType, NetworkState


class ApiResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkParametersResponse(ApiResponseModel):
    """网络参数。``packet_loss_percent`` 是 Markov Simulator 生成的模拟值；Toxiproxy
    当前不真正施加（见 ``packet_loss_applied``），不得视为实测丢包率。"""

    state: NetworkState
    latency_ms: int | None = Field(default=None, ge=0)
    jitter_ms: int | None = Field(default=None, ge=0)
    bandwidth_kbps: int | None = Field(default=None, ge=0)
    packet_loss_percent: float = Field(
        ge=0,
        le=100,
        allow_inf_nan=False,
        description="Markov 模拟生成的 packet loss（%）；Toxiproxy 未真实施加。"
        "如需实测风险，请改用 packet_loss_applied 判断。",
    )
    disconnect_mode: DisconnectMode
    packet_loss_applied: bool = Field(
        description="Toxiproxy v2.12.0 是否已把 packet loss 施加到数据面。当前恒为 false。"
    )


class LinkResponse(ApiResponseModel):
    """链路当前状态。``current_state`` 是目标/desired 状态，``applied_parameters``
    是数据面真正生效状态（NET-2）。消费方如需实际生效状态应优先读取
    ``applied_parameters`` + ``last_apply_success``。"""

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


class SchedulerReporterDetail(ApiResponseModel):
    """AUD-13: reporter observability fields exposed via /health."""

    status: str
    last_error: str | None = None
    last_success_ns: int | None = Field(default=None, ge=0)
    last_failure_ns: int | None = Field(default=None, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_rejected_count: int | None = Field(default=None, ge=0)
    # NET-1: 部分成功可观测性。
    last_outcome: str | None = None
    partial_failure_count: int | None = Field(default=None, ge=0)


class HealthResponse(ApiResponseModel):
    status: str
    toxiproxy_available: bool
    scheduler_reporter_healthy: bool
    scheduler_reporter: SchedulerReporterDetail | None = None
    link_count: int = Field(ge=0)
    available_link_count: int = Field(ge=0)
    last_tick: int = Field(ge=0)
