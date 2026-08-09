"""Read services that translate immutable runtime snapshots to API schemas."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from controller.config_loader import ApplicationConfig
from controller.runtime_store import RuntimeStore
from domain.enums import NetworkState
from domain.models import LinkSnapshot, NetworkParameters
from plugins.logger.jsonl_writer import sanitize_for_log

from .schemas import (
    HealthResponse,
    LinkResponse,
    NetworkParametersResponse,
    RuntimeResponse,
)


HealthProvider = Callable[[], Mapping[str, Any]]


class ApiReadService:
    def __init__(
        self,
        runtime_store: RuntimeStore,
        config: ApplicationConfig,
        health_provider: HealthProvider,
    ) -> None:
        self._runtime_store = runtime_store
        self._config = config
        self._health_provider = health_provider

    def list_links(
        self,
        *,
        sender_id: str | None = None,
        edge_id: str | None = None,
        state: NetworkState | None = None,
        available: bool | None = None,
    ) -> list[LinkResponse]:
        snapshot = self._runtime_store.snapshot()
        return [
            self._link_response(link)
            for link in snapshot.links
            if (sender_id is None or link.sender_id == sender_id)
            and (edge_id is None or link.edge_id == edge_id)
            and (state is None or link.current_state is state)
            and (available is None or link.available is available)
        ]

    def get_link(self, link_id: str) -> LinkResponse:
        return self._link_response(self._runtime_store.get_link(link_id))

    def runtime(self) -> RuntimeResponse:
        snapshot = self._runtime_store.snapshot()
        return RuntimeResponse(
            experiment_id=self._config.experiment.experiment_id,
            mode=self._config.experiment.mode,
            tick=snapshot.tick,
            generated_at_ns=snapshot.generated_at_ns,
            update_interval_seconds=self._config.controller.update_interval_seconds,
            link_count=len(snapshot.links),
            available_link_count=sum(link.available for link in snapshot.links),
        )

    def health(self) -> HealthResponse:
        return HealthResponse.model_validate(dict(self._health_provider()))

    @staticmethod
    def _link_response(link: LinkSnapshot) -> LinkResponse:
        return LinkResponse(
            link_id=link.link_id,
            link_type=link.link_type,
            sender_id=link.sender_id,
            edge_id=link.edge_id,
            protocol=link.protocol,
            proxy_name=link.proxy_name,
            listen=link.listen,
            advertised_host=link.advertised_host,
            advertised_port=link.advertised_port,
            upstream=link.upstream,
            current_state=link.current_state,
            previous_state=link.previous_state,
            state_since_ns=link.state_since_ns,
            applied_state_since_ns=link.applied_state_since_ns,
            seed=link.seed,
            desired_parameters=ApiReadService._parameters(link.desired_parameters),
            applied_parameters=ApiReadService._parameters(link.applied_parameters),
            link_reliability_score=link.link_reliability_score,
            score_components=dict(link.score_components),
            available=link.available,
            last_apply_success=link.last_apply_success,
            last_apply_timestamp_ns=link.last_apply_timestamp_ns,
            consecutive_apply_failures=link.consecutive_apply_failures,
            error=(
                None
                if link.last_error is None
                else str(sanitize_for_log(link.last_error))
            ),
            report_enabled=link.report_enabled,
        )

    @staticmethod
    def _parameters(
        parameters: NetworkParameters | None,
    ) -> NetworkParametersResponse | None:
        if parameters is None:
            return None
        return NetworkParametersResponse(
            state=parameters.state,
            latency_ms=parameters.latency_ms,
            jitter_ms=parameters.jitter_ms,
            bandwidth_kbps=parameters.bandwidth_kbps,
            packet_loss_percent=parameters.packet_loss_percent,
            disconnect_mode=parameters.disconnect_mode,
            packet_loss_applied=parameters.packet_loss_applied,
        )
