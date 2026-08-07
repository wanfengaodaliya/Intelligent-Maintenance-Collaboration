"""Runtime domain models kept separate from configuration and API schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Mapping

from .enums import DisconnectMode, LinkProtocol, LinkType, NetworkState


SCORE_COMPONENT_NAMES = (
    "latency",
    "jitter",
    "bandwidth",
    "packet_loss",
    "state_prior",
)


@dataclass(frozen=True, slots=True)
class NetworkParameters:
    state: NetworkState
    latency_ms: int | None
    jitter_ms: int | None
    bandwidth_kbps: int | None
    packet_loss_percent: float
    disconnect_mode: DisconnectMode
    packet_loss_applied: bool = False

    def __post_init__(self) -> None:
        try:
            disconnect_mode = DisconnectMode(self.disconnect_mode)
        except ValueError as exc:
            raise ValueError(f"unsupported disconnect mode: {self.disconnect_mode}") from exc
        object.__setattr__(self, "disconnect_mode", disconnect_mode)

        if not math.isfinite(self.packet_loss_percent):
            raise ValueError("packet_loss_percent must be finite")
        if not 0 <= self.packet_loss_percent <= 100:
            raise ValueError("packet_loss_percent must be between 0 and 100")

        values = (self.latency_ms, self.jitter_ms, self.bandwidth_kbps)
        if any(value is not None and value < 0 for value in values):
            raise ValueError("network parameters cannot be negative")

        if disconnect_mode is DisconnectMode.NONE and any(value is None for value in values):
            raise ValueError("connected parameters require latency, jitter, and bandwidth")
        if disconnect_mode is not DisconnectMode.NONE and any(value is not None for value in values):
            raise ValueError("disconnected parameters must omit latency, jitter, and bandwidth")
        if self.state is NetworkState.DISCONNECTED and disconnect_mode is DisconnectMode.NONE:
            raise ValueError("DISCONNECTED parameters require a disconnect mode")
        if self.state is not NetworkState.DISCONNECTED and disconnect_mode is not DisconnectMode.NONE:
            raise ValueError("connected states cannot use a disconnect mode")
        if (
            self.state is NetworkState.DISCONNECTED
            and self.packet_loss_percent != 100.0
        ):
            raise ValueError("DISCONNECTED packet loss must equal 100")


@dataclass(slots=True)
class LinkRuntime:
    link_id: str
    link_type: LinkType
    sender_id: str | None
    edge_id: str | None
    protocol: LinkProtocol
    proxy_name: str
    listen: str
    advertised_host: str
    advertised_port: int
    upstream: str
    current_state: NetworkState
    previous_state: NetworkState
    state_since_ns: int
    seed: int
    applied_state_since_ns: int | None = None
    desired_parameters: NetworkParameters | None = None
    applied_parameters: NetworkParameters | None = None
    link_reliability_score: float = 0.0
    score_components: dict[str, float] = field(default_factory=dict)
    available: bool = False
    last_apply_success: bool = False
    last_apply_timestamp_ns: int | None = None
    consecutive_apply_failures: int = 0
    last_error: str | None = None
    report_enabled: bool = True

    def to_snapshot(self) -> LinkSnapshot:
        """Copy mutable runtime data into an immutable reporting object."""

        return LinkSnapshot(
            link_id=self.link_id,
            link_type=self.link_type,
            sender_id=self.sender_id,
            edge_id=self.edge_id,
            protocol=self.protocol,
            proxy_name=self.proxy_name,
            listen=self.listen,
            advertised_host=self.advertised_host,
            advertised_port=self.advertised_port,
            upstream=self.upstream,
            current_state=self.current_state,
            previous_state=self.previous_state,
            state_since_ns=self.state_since_ns,
            seed=self.seed,
            applied_state_since_ns=self.applied_state_since_ns,
            desired_parameters=self.desired_parameters,
            applied_parameters=self.applied_parameters,
            link_reliability_score=self.link_reliability_score,
            score_components=MappingProxyType(dict(self.score_components)),
            available=self.available,
            last_apply_success=self.last_apply_success,
            last_apply_timestamp_ns=self.last_apply_timestamp_ns,
            consecutive_apply_failures=self.consecutive_apply_failures,
            last_error=self.last_error,
            report_enabled=self.report_enabled,
        )


@dataclass(frozen=True, slots=True)
class LinkSnapshot:
    link_id: str
    link_type: LinkType
    sender_id: str | None
    edge_id: str | None
    protocol: LinkProtocol
    proxy_name: str
    listen: str
    advertised_host: str
    advertised_port: int
    upstream: str
    current_state: NetworkState
    previous_state: NetworkState
    state_since_ns: int
    seed: int
    applied_state_since_ns: int | None
    desired_parameters: NetworkParameters | None
    applied_parameters: NetworkParameters | None
    link_reliability_score: float
    score_components: Mapping[str, float]
    available: bool
    last_apply_success: bool
    last_apply_timestamp_ns: int | None
    consecutive_apply_failures: int
    last_error: str | None
    report_enabled: bool


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    tick: int
    generated_at_ns: int
    links: tuple[LinkSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    link_id: str
    success: bool
    applied_parameters: NetworkParameters | None
    timestamp_ns: int
    error: str | None
    packet_loss_applied: bool = False

    def __post_init__(self) -> None:
        if self.timestamp_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")
        if self.success and self.applied_parameters is None:
            raise ValueError("successful apply requires applied parameters")
        if not self.success and self.applied_parameters is not None:
            raise ValueError("failed apply cannot include applied parameters")
        if self.success and self.error is not None:
            raise ValueError("successful apply cannot include an error")
        if self.packet_loss_applied:
            raise ValueError("Toxiproxy v2.12.0 does not apply packet loss")
        if (
            self.applied_parameters is not None
            and self.applied_parameters.packet_loss_applied
        ):
            raise ValueError("applied parameters cannot claim packet loss was applied")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    available: bool
    components: Mapping[str, float]
    weights: Mapping[str, float]
    reason: str
    packet_loss_applied: bool

    def __post_init__(self) -> None:
        if not math.isfinite(self.score) or not 0 <= self.score <= 100:
            raise ValueError("score must be a finite value between 0 and 100")
        if any(
            not math.isfinite(value) or not 0 <= value <= 100
            for value in self.components.values()
        ):
            raise ValueError("score components must be finite values from 0 to 100")
        if any(
            not math.isfinite(value) or not 0 <= value <= 1
            for value in self.weights.values()
        ):
            raise ValueError("score weights must be finite values from 0 to 1")
        if set(self.components) != set(SCORE_COMPONENT_NAMES):
            raise ValueError("score components must define every component exactly once")
        if set(self.weights) != set(SCORE_COMPONENT_NAMES):
            raise ValueError("score weights must define every component exactly once")
        if not math.isclose(
            sum(self.weights.values()),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("score weights must sum to 1")
        if not self.reason:
            raise ValueError("score reason cannot be empty")
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))


@dataclass(frozen=True, slots=True)
class ScoreCalculationOutcome:
    link_id: str
    success: bool
    result: ScoreResult | None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if not self.link_id:
            raise ValueError("link_id cannot be empty")
        if self.success:
            if self.result is None or self.error_type is not None:
                raise ValueError("successful score outcome requires only a result")
        elif self.result is not None or not self.error_type:
            raise ValueError("failed score outcome requires only an error type")
