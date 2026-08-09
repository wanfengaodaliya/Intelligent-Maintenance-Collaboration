"""Strict multi-file configuration loader for the V3 controller."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from copy import deepcopy
from enum import Enum
import math
import os
from pathlib import Path
import re
from types import UnionType
from typing import Any, Literal, Mapping, Union, get_args, get_origin
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic import field_validator, model_validator
import yaml

from domain.enums import ExperimentMode, LinkProtocol, LinkType, NetworkState
from domain.exceptions import ConfigurationError


LINK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ENTITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_TOXIPROXY_URL = "TOXIPROXY_API_BASE_URL"
ENV_SCHEDULER_URL = "NETWORK_SCHEDULER_URL"


def _validate_native_yaml_type(
    value: Any,
    annotation: Any,
    location: str,
) -> None:
    if annotation is Any:
        return
    if value is None:
        return

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {Union, UnionType}:
        failures: list[ValueError] = []
        for candidate in arguments:
            if candidate is type(None):
                continue
            try:
                _validate_native_yaml_type(value, candidate, location)
            except ValueError as exc:
                failures.append(exc)
            else:
                return
        if failures:
            raise failures[-1]
        return

    if origin is Literal:
        if any(
            type(value) is type(candidate) and value == candidate
            for candidate in arguments
        ):
            return
        candidate_types = {type(candidate) for candidate in arguments}
        if len(candidate_types) == 1:
            _validate_native_yaml_type(value, next(iter(candidate_types)), location)
        if len(arguments) == 1:
            raise ValueError(f"{location}: Input should be {arguments[0]!r}")
        expected = ", ".join(repr(candidate) for candidate in arguments)
        raise ValueError(f"{location} must be one of {expected}")

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if isinstance(value, annotation):
            return
        if annotation.__members__:
            sample = next(iter(annotation)).value
            if type(value) is type(sample):
                return
        raise ValueError(f"{location} must be a YAML string")

    if annotation is bool:
        if type(value) is not bool:
            raise ValueError(f"{location} must be a YAML boolean")
        return
    if annotation is int:
        if type(value) is not int:
            raise ValueError(f"{location} must be a YAML integer")
        return
    if annotation is float:
        if type(value) not in {int, float}:
            raise ValueError(f"{location} must be a YAML number")
        return
    if annotation is str:
        if type(value) is not str:
            raise ValueError(f"{location} must be a YAML string")
        return

    if origin in {tuple, list}:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{location} must be a YAML sequence")
        if not arguments:
            return
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            for index, item in enumerate(value):
                _validate_native_yaml_type(
                    item,
                    arguments[0],
                    f"{location}[{index}]",
                )
        else:
            for index, (item, item_type) in enumerate(
                zip(value, arguments, strict=False)
            ):
                _validate_native_yaml_type(
                    item,
                    item_type,
                    f"{location}[{index}]",
                )
        return

    if origin in {dict, MappingABC}:
        if not isinstance(value, MappingABC):
            raise ValueError(f"{location} must be a YAML mapping")
        if len(arguments) == 2:
            key_type, item_type = arguments
            for key, item in value.items():
                _validate_native_yaml_type(key, key_type, f"{location} key")
                _validate_native_yaml_type(
                    item,
                    item_type,
                    f"{location}[{key}]",
                )
        return

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, (annotation, MappingABC)):
            raise ValueError(f"{location} must be a YAML mapping")



class FrozenDict(dict[Any, Any]):
    """A serializable dictionary whose contents cannot change after creation."""

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("configuration mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        del memo
        return self


class StrictFrozenModel(BaseModel):
    """Base for configuration objects that reject misspelled fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    @model_validator(mode="before")
    @classmethod
    def validate_native_yaml_types(cls, value: Any) -> Any:
        if isinstance(value, cls) or not isinstance(value, MappingABC):
            return value
        for field_name, field in cls.model_fields.items():
            if field_name in value:
                _validate_native_yaml_type(
                    value[field_name],
                    field.annotation,
                    field_name,
                )
        return value


def _validate_identifier(value: str, location: str) -> str:
    if not LINK_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{location} may contain only letters, numbers, underscores, and hyphens"
        )
    return value


def _parse_endpoint(value: str, location: str) -> tuple[str, int]:
    if ":" not in value:
        raise ValueError(f"{location} must use host:port format")
    host, raw_port = value.rsplit(":", 1)
    if not host:
        raise ValueError(f"{location} host cannot be empty")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"{location} port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{location} port must be between 1 and 65535")
    return host, port


def _validate_http_url(value: str, location: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError(f"{location} must be an absolute http or https URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{location} must not contain embedded credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{location} contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{location} port must be between 1 and 65535")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{location} must not contain params, query, or fragment")
    return value.rstrip("/")


class SenderConfig(StrictFrozenModel):
    sender_id: str
    mqtt_proxy_host: str
    base_listen_port: int = Field(ge=1, le=65535)

    @field_validator("sender_id")
    @classmethod
    def validate_sender_id(cls, value: str) -> str:
        return _validate_identifier(value, "sender_id")

    @field_validator("mqtt_proxy_host")
    @classmethod
    def validate_proxy_host(cls, value: str) -> str:
        if not value:
            raise ValueError("mqtt_proxy_host cannot be empty")
        return value


class EdgeConfig(StrictFrozenModel):
    edge_id: str
    broker_upstream: str

    @field_validator("edge_id")
    @classmethod
    def validate_edge_id(cls, value: str) -> str:
        return _validate_identifier(value, "edge_id")

    @field_validator("broker_upstream")
    @classmethod
    def validate_broker_upstream(cls, value: str) -> str:
        _parse_endpoint(value, "broker_upstream")
        return value


class EntitiesConfig(StrictFrozenModel):
    senders: tuple[SenderConfig, ...]
    edges: tuple[EdgeConfig, ...]

    @model_validator(mode="after")
    def validate_unique_entities(self) -> EntitiesConfig:
        _ensure_unique(
            (sender.sender_id for sender in self.senders), "sender_id"
        )
        _ensure_unique((edge.edge_id for edge in self.edges), "edge_id")
        return self


class ToxiproxyConfig(StrictFrozenModel):
    api_base_url: str
    connect_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    read_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    retry_count: int = Field(ge=0, le=20)
    backoff_base_seconds: float = Field(ge=0, allow_inf_nan=False)
    startup_wait_seconds: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        return _validate_http_url(value, "toxiproxy.api_base_url")


class ControllerConfig(StrictFrozenModel):
    update_interval_seconds: float = Field(gt=0, allow_inf_nan=False)
    initial_state: NetworkState
    refresh_parameters_when_state_unchanged: bool
    clear_toxics_on_exit: bool
    max_parallel_updates: int = Field(ge=1, le=128)


class AvailabilityConfig(StrictFrozenModel):
    disconnected_is_unavailable: bool
    max_consecutive_apply_failures: int = Field(ge=1)
    stale_after_seconds: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("disconnected_is_unavailable")
    @classmethod
    def require_disconnected_unavailable(cls, value: bool) -> bool:
        if not value:
            raise ValueError("disconnected_is_unavailable must be true")
        return value


class LinkGenerationConfig(StrictFrozenModel):
    mode: Literal["explicit", "cartesian"]
    protocol: LinkProtocol = LinkProtocol.MQTT
    id_template: str = "{sender_id}__to__{edge_id}__mqtt"
    proxy_name_template: str = "{sender_id}__to__{edge_id}__mqtt"
    listen_host: str = "0.0.0.0"
    seed_offset_start: int = Field(default=1, ge=0)
    latency_stream: Literal["upstream", "downstream"] = "upstream"
    bandwidth_stream: Literal["upstream", "downstream"] = "upstream"
    disconnect_stream: Literal["upstream", "downstream"] = "upstream"
    disconnect_mode: Literal["auto", "timeout", "reset_peer"] = "auto"
    report_enabled: bool = True

    @model_validator(mode="after")
    def validate_cartesian_settings(self) -> LinkGenerationConfig:
        if self.mode == "cartesian" and self.protocol is not LinkProtocol.MQTT:
            raise ValueError("cartesian link generation currently supports mqtt only")
        for name, template in (
            ("id_template", self.id_template),
            ("proxy_name_template", self.proxy_name_template),
        ):
            if "{sender_id}" not in template or "{edge_id}" not in template:
                raise ValueError(f"{name} must contain sender_id and edge_id placeholders")
        if not self.listen_host:
            raise ValueError("listen_host cannot be empty")
        return self


class LinkDefinition(StrictFrozenModel):
    link_id: str
    link_type: LinkType
    sender_id: str | None = None
    edge_id: str | None = None
    protocol: LinkProtocol
    proxy_name: str | None = None
    listen: str
    advertised_host: str
    advertised_port: int = Field(ge=1, le=65535)
    upstream: str
    seed_offset: int | None = Field(default=None, ge=0)
    latency_stream: Literal["upstream", "downstream"] = "upstream"
    bandwidth_stream: Literal["upstream", "downstream"] = "upstream"
    disconnect_stream: Literal["upstream", "downstream"] = "upstream"
    disconnect_mode: Literal["auto", "timeout", "reset_peer"] = "auto"
    report_enabled: bool = True

    @field_validator("link_id")
    @classmethod
    def validate_link_id(cls, value: str) -> str:
        return _validate_identifier(value, "link_id")

    @field_validator("proxy_name")
    @classmethod
    def validate_proxy_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "proxy_name")

    @field_validator("sender_id", "edge_id")
    @classmethod
    def validate_entity_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "entity reference")

    @field_validator("listen", "upstream")
    @classmethod
    def validate_endpoint(cls, value: str, info: Any) -> str:
        _parse_endpoint(value, info.field_name)
        return value

    @model_validator(mode="after")
    def validate_link_shape(self) -> LinkDefinition:
        _, listen_port = _parse_endpoint(self.listen, "listen")
        if listen_port != self.advertised_port:
            raise ValueError("advertised_port must match the configured listen port")

        if self.link_type is LinkType.SENDER_TO_EDGE:
            if self.sender_id is None or self.edge_id is None:
                raise ValueError(
                    "sender_to_edge links require both sender_id and edge_id"
                )
            if self.protocol is not LinkProtocol.MQTT:
                raise ValueError("sender_to_edge links must use mqtt")
        elif self.protocol is not LinkProtocol.HTTP:
            raise ValueError("all non-sender_to_edge links must use http")
        elif self.link_type in {
            LinkType.SENDER_TO_SCHEDULER,
            LinkType.SCHEDULER_TO_SENDER,
        } and self.sender_id is None:
            raise ValueError(f"{self.link_type.value} links require sender_id")
        elif self.link_type in {
            LinkType.EDGE_TO_SCHEDULER,
            LinkType.SCHEDULER_TO_EDGE,
            LinkType.EDGE_TO_CLOUD,
        } and self.edge_id is None:
            raise ValueError(f"{self.link_type.value} links require edge_id")
        return self


class LinksDocument(StrictFrozenModel):
    toxiproxy: ToxiproxyConfig
    controller: ControllerConfig
    availability: AvailabilityConfig
    link_generation: LinkGenerationConfig
    links: tuple[LinkDefinition, ...] = ()

    @model_validator(mode="after")
    def validate_mode(self) -> LinksDocument:
        if self.link_generation.mode == "explicit" and not self.links:
            raise ValueError("explicit mode requires at least one configured link")
        if self.link_generation.mode == "cartesian" and any(
            link.protocol is not LinkProtocol.HTTP for link in self.links
        ):
            raise ValueError(
                "cartesian mode supplemental links must use http"
            )
        return self


class IntegerRange(StrictFrozenModel):
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> IntegerRange:
        if self.min > self.max:
            raise ValueError("range min must be less than or equal to max")
        return self


class FloatRange(StrictFrozenModel):
    min: float = Field(ge=0, le=100, allow_inf_nan=False)
    max: float = Field(ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> FloatRange:
        if self.min > self.max:
            raise ValueError("range min must be less than or equal to max")
        return self


class StateConfig(StrictFrozenModel):
    latency_ms: IntegerRange | None
    jitter_ms: IntegerRange | None
    bandwidth_kbps: IntegerRange | None
    packet_loss_percent: FloatRange | float
    disconnect_mode: Literal["none", "timeout", "reset_peer"]

    @model_validator(mode="after")
    def validate_connected_shape(self) -> StateConfig:
        ranges = (self.latency_ms, self.jitter_ms, self.bandwidth_kbps)
        if self.disconnect_mode == "none":
            if any(value is None for value in ranges):
                raise ValueError("connected states require all three parameter ranges")
            if not isinstance(self.packet_loss_percent, FloatRange):
                raise ValueError("connected states require a packet loss range")
            assert self.bandwidth_kbps is not None
            if self.bandwidth_kbps.min <= 0:
                raise ValueError("connected bandwidth must be greater than zero")
        else:
            if any(value is not None for value in ranges):
                raise ValueError("disconnected states must use null network ranges")
            if isinstance(self.packet_loss_percent, FloatRange):
                raise ValueError("disconnected packet loss must be a scalar")
            if not math.isfinite(self.packet_loss_percent):
                raise ValueError("packet loss must be finite")
            if self.packet_loss_percent != 100.0:
                raise ValueError("disconnected packet loss must equal 100")
        return self


class NetworkStatesDocument(StrictFrozenModel):
    states: Mapping[NetworkState, StateConfig]

    @model_validator(mode="after")
    def freeze_states(self) -> NetworkStatesDocument:
        object.__setattr__(self, "states", FrozenDict(self.states))
        return self


class TransitionConfig(StrictFrozenModel):
    states: tuple[NetworkState, ...]
    matrix: tuple[tuple[float, ...], ...]

    @model_validator(mode="after")
    def validate_matrix(self) -> TransitionConfig:
        count = len(self.states)
        if count == 0 or len(set(self.states)) != count:
            raise ValueError("transition states must be non-empty and unique")
        if len(self.matrix) != count:
            raise ValueError("transition matrix must be square")
        for index, row in enumerate(self.matrix):
            if len(row) != count:
                raise ValueError("transition matrix must be square")
            if any(not math.isfinite(value) or value < 0 for value in row):
                raise ValueError(f"transition row {index} has invalid probabilities")
            if not math.isclose(sum(row), 1.0, abs_tol=1e-9):
                raise ValueError(f"transition row {index} must sum to 1")
        return self


class ScoreWeights(StrictFrozenModel):
    latency: float = Field(ge=0, le=1, allow_inf_nan=False)
    jitter: float = Field(ge=0, le=1, allow_inf_nan=False)
    bandwidth: float = Field(ge=0, le=1, allow_inf_nan=False)
    packet_loss: float = Field(ge=0, le=1, allow_inf_nan=False)
    state_prior: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_sum(self) -> ScoreWeights:
        total = (
            self.latency
            + self.jitter
            + self.bandwidth
            + self.packet_loss
            + self.state_prior
        )
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("score weights must sum to 1")
        return self


class ScoreNormalization(StrictFrozenModel):
    latency_best_ms: float = Field(ge=0, allow_inf_nan=False)
    latency_worst_ms: float = Field(ge=0, allow_inf_nan=False)
    jitter_best_ms: float = Field(ge=0, allow_inf_nan=False)
    jitter_worst_ms: float = Field(ge=0, allow_inf_nan=False)
    bandwidth_best_kbps: float = Field(gt=0, allow_inf_nan=False)
    bandwidth_worst_kbps: float = Field(gt=0, allow_inf_nan=False)
    packet_loss_best_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    packet_loss_worst_percent: float = Field(ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> ScoreNormalization:
        if self.latency_best_ms >= self.latency_worst_ms:
            raise ValueError("latency best must be lower than latency worst")
        if self.jitter_best_ms >= self.jitter_worst_ms:
            raise ValueError("jitter best must be lower than jitter worst")
        if self.bandwidth_best_kbps <= self.bandwidth_worst_kbps:
            raise ValueError("bandwidth best must be greater than bandwidth worst")
        if self.packet_loss_best_percent >= self.packet_loss_worst_percent:
            raise ValueError("packet loss best must be lower than packet loss worst")
        return self


class ScoreFailurePolicy(StrictFrozenModel):
    unavailable_after_consecutive_failures: int = Field(ge=1)
    max_score_when_apply_failed: float = Field(ge=0, le=20, allow_inf_nan=False)


class ScoreConfig(StrictFrozenModel):
    source: Literal["applied"]
    precision: Literal[1]
    weights: ScoreWeights
    state_prior: Mapping[NetworkState, float]
    normalization: ScoreNormalization
    failure_policy: ScoreFailurePolicy

    @model_validator(mode="after")
    def validate_state_priors(self) -> ScoreConfig:
        if set(self.state_prior) != set(NetworkState):
            raise ValueError("score state_prior must define every V3 state exactly once")
        if any(
            not math.isfinite(value) or not 0 <= value <= 100
            for value in self.state_prior.values()
        ):
            raise ValueError("score state priors must be finite values from 0 to 100")
        object.__setattr__(
            self, "state_prior", FrozenDict(self.state_prior)
        )
        return self


class ScoreDocument(StrictFrozenModel):
    score: ScoreConfig


class PluginSwitch(StrictFrozenModel):
    enabled: bool
    required: bool


class ApiPluginConfig(PluginSwitch):
    host: str
    port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value:
            raise ValueError("api host cannot be empty")
        return value


class PluginsConfig(StrictFrozenModel):
    logger: PluginSwitch
    toxiproxy: PluginSwitch
    markov: PluginSwitch
    score: PluginSwitch
    reporter: PluginSwitch
    api: ApiPluginConfig
    health: PluginSwitch

    @model_validator(mode="after")
    def validate_core_plugins(self) -> PluginsConfig:
        for name in ("logger", "toxiproxy", "markov", "score"):
            plugin = getattr(self, name)
            if not plugin.enabled or not plugin.required:
                raise ValueError(f"core plugin {name} must be enabled and required")
        return self


class LoggingConfig(StrictFrozenModel):
    max_bytes: int = Field(gt=0)
    backup_count: int = Field(ge=0)


class PluginsDocument(StrictFrozenModel):
    plugins: PluginsConfig
    logging: LoggingConfig


class ReporterAuthConfig(StrictFrozenModel):
    mode: Literal["none", "bearer"]
    token_env: str

    @field_validator("token_env")
    @classmethod
    def validate_token_env(cls, value: str) -> str:
        if value != "NETWORK_REPORT_TOKEN":
            raise ValueError("token_env must be NETWORK_REPORT_TOKEN")
        return value


class ReporterConfig(StrictFrozenModel):
    enabled: bool
    reporter_id: str
    scheduler_url: str
    report_interval_seconds: float = Field(gt=0, allow_inf_nan=False)
    connect_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    read_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    retry_count: int = Field(ge=0, le=20)
    backoff_base_seconds: float = Field(ge=0, allow_inf_nan=False)
    queue_capacity: int = Field(ge=1)
    queue_overflow_policy: Literal["drop_oldest"]
    report_only_enabled_links: bool
    auth: ReporterAuthConfig

    @field_validator("reporter_id")
    @classmethod
    def validate_reporter_id(cls, value: str) -> str:
        return _validate_identifier(value, "reporter_id")

    @field_validator("scheduler_url")
    @classmethod
    def validate_scheduler_url(cls, value: str) -> str:
        return _validate_http_url(value, "reporter.scheduler_url")


class ReporterDocument(StrictFrozenModel):
    reporter: ReporterConfig


class FixedStateConfig(StrictFrozenModel):
    default: NetworkState
    overrides: Mapping[str, NetworkState] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def validate_override_ids(
        cls, value: Mapping[str, NetworkState]
    ) -> Mapping[str, NetworkState]:
        for link_id in value:
            _validate_identifier(link_id, "fixed_state override link_id")
        return FrozenDict(value)


class ExperimentConfig(StrictFrozenModel):
    experiment_id: str
    mode: ExperimentMode
    global_seed: int = Field(ge=0)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    timezone: str
    save_ground_truth: bool

    @field_validator("experiment_id")
    @classmethod
    def validate_experiment_id(cls, value: str) -> str:
        return _validate_identifier(value, "experiment_id")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value


class ExperimentDocument(StrictFrozenModel):
    experiment: ExperimentConfig
    fixed_state: FixedStateConfig | None = None

    @model_validator(mode="after")
    def validate_mode_config(self) -> ExperimentDocument:
        if self.experiment.mode is ExperimentMode.FIXED and self.fixed_state is None:
            raise ValueError("fixed experiment mode requires fixed_state configuration")
        return self


class ResolvedLinkConfig(StrictFrozenModel):
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
    seed_offset: int | None
    latency_stream: Literal["upstream", "downstream"]
    bandwidth_stream: Literal["upstream", "downstream"]
    disconnect_stream: Literal["upstream", "downstream"]
    disconnect_mode: Literal["auto", "timeout", "reset_peer"]
    report_enabled: bool

    @field_validator("link_id", "proxy_name")
    @classmethod
    def validate_names(cls, value: str, info: Any) -> str:
        return _validate_identifier(value, info.field_name)

    @field_validator("listen", "upstream")
    @classmethod
    def validate_endpoints(cls, value: str, info: Any) -> str:
        _parse_endpoint(value, info.field_name)
        return value


class ApplicationConfig(StrictFrozenModel):
    entities: EntitiesConfig
    toxiproxy: ToxiproxyConfig
    controller: ControllerConfig
    availability: AvailabilityConfig
    links: tuple[ResolvedLinkConfig, ...]
    network_states: Mapping[NetworkState, StateConfig]
    transition: TransitionConfig
    score: ScoreConfig
    plugins: PluginsConfig
    logging: LoggingConfig
    reporter: ReporterConfig
    experiment: ExperimentConfig
    fixed_state: FixedStateConfig | None
    report_auth_token: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def freeze_nested_mappings(self) -> ApplicationConfig:
        object.__setattr__(
            self,
            "network_states",
            FrozenDict(self.network_states),
        )
        return self

    def sanitized_summary(self) -> dict[str, object]:
        """Return safe startup metadata without authentication material."""

        return {
            "experiment_id": self.experiment.experiment_id,
            "mode": self.experiment.mode.value,
            "link_count": len(self.links),
            "sender_count": len(self.entities.senders),
            "edge_count": len(self.entities.edges),
            "toxiproxy_api_base_url": self.toxiproxy.api_base_url,
            "scheduler_url": self.reporter.scheduler_url,
            "reporter_enabled": self.reporter.enabled,
            "report_auth_configured": self.report_auth_token is not None,
        }


class ConfigLoader:
    """Load, validate, and resolve all V3 configuration files."""

    REQUIRED_FILES = {
        "entities": "entities.yaml",
        "links": "links.yaml",
        "network_states": "network_states.yaml",
        "transition": "transition_matrix.yaml",
        "score": "score.yaml",
        "plugins": "plugins.yaml",
        "reporter": "reporter.yaml",
        "experiment": "experiment.yaml",
    }

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def load(self, config_dir: str | Path) -> ApplicationConfig:
        directory = Path(config_dir)
        documents = {
            name: self._load_yaml(directory / filename)
            for name, filename in self.REQUIRED_FILES.items()
        }
        self._apply_environment_overrides(documents)

        try:
            entities = EntitiesConfig.model_validate(documents["entities"])
            links_document = LinksDocument.model_validate(documents["links"])
            network_states_document = NetworkStatesDocument.model_validate(
                documents["network_states"]
            )
            transition = TransitionConfig.model_validate(documents["transition"])
            score_document = ScoreDocument.model_validate(documents["score"])
            plugins_document = PluginsDocument.model_validate(documents["plugins"])
            reporter_document = ReporterDocument.model_validate(documents["reporter"])
            experiment_document = ExperimentDocument.model_validate(
                documents["experiment"]
            )
            resolved_links = self._resolve_links(entities, links_document)
            config = ApplicationConfig(
                entities=entities,
                toxiproxy=links_document.toxiproxy,
                controller=links_document.controller,
                availability=links_document.availability,
                links=resolved_links,
                network_states=network_states_document.states,
                transition=transition,
                score=score_document.score,
                plugins=plugins_document.plugins,
                logging=plugins_document.logging,
                reporter=reporter_document.reporter,
                experiment=experiment_document.experiment,
                fixed_state=experiment_document.fixed_state,
                report_auth_token=self._load_report_token(reporter_document.reporter),
            )
            self.validate_cross_file_rules(config)
            return config
        except (ValidationError, ValueError) as exc:
            raise ConfigurationError(f"invalid V3 configuration: {exc}") from exc

    def validate_cross_file_rules(self, config: ApplicationConfig) -> None:
        expected_states = set(NetworkState)
        configured_states = set(config.network_states)
        transition_states = set(config.transition.states)
        if configured_states != expected_states:
            raise ConfigurationError(
                "network_states.yaml must define GOOD, MEDIUM, BAD, and DISCONNECTED"
            )
        if transition_states != configured_states:
            raise ConfigurationError(
                "transition matrix states must match network state names"
            )
        if config.controller.initial_state not in configured_states:
            raise ConfigurationError("controller initial_state is not configured")

        disconnected = config.network_states[NetworkState.DISCONNECTED]
        if disconnected.disconnect_mode == "none":
            raise ConfigurationError("DISCONNECTED must use a disconnect mode")
        for state in (
            NetworkState.GOOD,
            NetworkState.MEDIUM,
            NetworkState.BAD,
        ):
            if config.network_states[state].disconnect_mode != "none":
                raise ConfigurationError(f"{state.value} must be a connected state")

        sender_ids = {sender.sender_id for sender in config.entities.senders}
        edge_ids = {edge.edge_id for edge in config.entities.edges}
        for link in config.links:
            if link.sender_id is not None and link.sender_id not in sender_ids:
                raise ConfigurationError(
                    f"link {link.link_id} references unknown sender {link.sender_id}"
                )
            if link.edge_id is not None and link.edge_id not in edge_ids:
                raise ConfigurationError(
                    f"link {link.link_id} references unknown edge {link.edge_id}"
                )

        _ensure_unique((link.link_id for link in config.links), "link_id")
        _ensure_unique((link.proxy_name for link in config.links), "proxy_name")
        _ensure_unique((link.listen for link in config.links), "listen")
        _ensure_unique(
            (
                f"{link.advertised_host}:{link.advertised_port}"
                for link in config.links
            ),
            "advertised endpoint",
        )

        if config.plugins.reporter.enabled != config.reporter.enabled:
            raise ConfigurationError(
                "plugins.yaml reporter.enabled must match reporter.yaml reporter.enabled"
            )
        if (
            config.availability.max_consecutive_apply_failures
            != config.score.failure_policy.unavailable_after_consecutive_failures
        ):
            raise ConfigurationError(
                "availability and score consecutive failure thresholds must match"
            )

        if config.fixed_state is not None:
            link_ids = {link.link_id for link in config.links}
            unknown = set(config.fixed_state.overrides) - link_ids
            if unknown:
                raise ConfigurationError(
                    f"fixed_state overrides reference unknown links: {sorted(unknown)}"
                )

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = yaml.safe_load(handle)
        except FileNotFoundError as exc:
            raise ConfigurationError(f"missing configuration file: {path}") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot read configuration file {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ConfigurationError(f"configuration file must contain a mapping: {path}")
        return document

    def _apply_environment_overrides(
        self, documents: dict[str, dict[str, Any]]
    ) -> None:
        toxiproxy_url = self._environ.get(ENV_TOXIPROXY_URL)
        if toxiproxy_url:
            toxiproxy = documents["links"].get("toxiproxy")
            if not isinstance(toxiproxy, dict):
                raise ConfigurationError("links.yaml toxiproxy must be a mapping")
            toxiproxy["api_base_url"] = toxiproxy_url
        scheduler_url = self._environ.get(ENV_SCHEDULER_URL)
        if scheduler_url:
            reporter = documents["reporter"].get("reporter")
            if not isinstance(reporter, dict):
                raise ConfigurationError("reporter.yaml reporter must be a mapping")
            reporter["scheduler_url"] = scheduler_url

    def _load_report_token(self, reporter: ReporterConfig) -> SecretStr | None:
        token_name = reporter.auth.token_env
        if not reporter.enabled or reporter.auth.mode == "none":
            return None
        if urlparse(reporter.scheduler_url).scheme != "https":
            raise ConfigurationError(
                "reporter bearer auth requires an https scheduler_url"
            )
        token = self._environ.get(token_name)
        if token is None or not token.strip():
            raise ConfigurationError(
                f"reporter bearer auth requires environment variable {token_name}"
            )
        return SecretStr(token.strip())

    def _resolve_links(
        self,
        entities: EntitiesConfig,
        document: LinksDocument,
    ) -> tuple[ResolvedLinkConfig, ...]:
        links: list[ResolvedLinkConfig] = []
        generation = document.link_generation
        if generation.mode == "cartesian":
            if not entities.senders or not entities.edges:
                raise ValueError("cartesian mode requires at least one sender and one edge")
            offset = generation.seed_offset_start
            for sender in entities.senders:
                for edge_index, edge in enumerate(entities.edges):
                    port = sender.base_listen_port + edge_index
                    if port > 65535:
                        raise ValueError(
                            f"generated listen port exceeds 65535 for {sender.sender_id}"
                        )
                    fields = {
                        "sender_id": sender.sender_id,
                        "edge_id": edge.edge_id,
                    }
                    try:
                        link_id = generation.id_template.format(**fields)
                        proxy_name = generation.proxy_name_template.format(**fields)
                    except (KeyError, ValueError) as exc:
                        raise ValueError(f"invalid cartesian link template: {exc}") from exc
                    links.append(
                        ResolvedLinkConfig(
                            link_id=link_id,
                            link_type=LinkType.SENDER_TO_EDGE,
                            sender_id=sender.sender_id,
                            edge_id=edge.edge_id,
                            protocol=generation.protocol,
                            proxy_name=proxy_name,
                            listen=f"{generation.listen_host}:{port}",
                            advertised_host=sender.mqtt_proxy_host,
                            advertised_port=port,
                            upstream=edge.broker_upstream,
                            seed_offset=offset,
                            latency_stream=generation.latency_stream,
                            bandwidth_stream=generation.bandwidth_stream,
                            disconnect_stream=generation.disconnect_stream,
                            disconnect_mode=generation.disconnect_mode,
                            report_enabled=generation.report_enabled,
                        )
                    )
                    offset += 1

        links.extend(self._resolve_explicit_link(link) for link in document.links)
        return tuple(links)

    @staticmethod
    def _resolve_explicit_link(link: LinkDefinition) -> ResolvedLinkConfig:
        return ResolvedLinkConfig(
            link_id=link.link_id,
            link_type=link.link_type,
            sender_id=link.sender_id,
            edge_id=link.edge_id,
            protocol=link.protocol,
            proxy_name=link.proxy_name or link.link_id,
            listen=link.listen,
            advertised_host=link.advertised_host,
            advertised_port=link.advertised_port,
            upstream=link.upstream,
            seed_offset=link.seed_offset,
            latency_stream=link.latency_stream,
            bandwidth_stream=link.bandwidth_stream,
            disconnect_stream=link.disconnect_stream,
            disconnect_mode=link.disconnect_mode,
            report_enabled=link.report_enabled,
        )


def _ensure_unique(values: Any, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


def load_config(
    config_dir: str | Path,
    environ: Mapping[str, str] | None = None,
) -> ApplicationConfig:
    """Convenience entry point used by the application bootstrap."""

    # A copy prevents the caller's mapping from changing during validation.
    environment = None if environ is None else deepcopy(dict(environ))
    return ConfigLoader(environment).load(config_dir)
