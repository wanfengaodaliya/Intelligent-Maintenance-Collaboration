"""Apply desired V3 parameters in safe toxic-operation order."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from domain.enums import DisconnectMode, LinkProtocol
from domain.models import ApplyResult, NetworkParameters

from .client import ToxiproxyClient


class StateApplier:
    def __init__(self, client: ToxiproxyClient) -> None:
        self._client = client

    def apply(
        self,
        link: Any,
        desired: NetworkParameters,
        previous_applied: NetworkParameters | None,
        timestamp_ns: int,
    ) -> ApplyResult:
        try:
            applied = self._apply(link, desired, previous_applied)
        except Exception as exc:
            return ApplyResult(
                link_id=link.link_id,
                success=False,
                applied_parameters=None,
                timestamp_ns=timestamp_ns,
                error=f"{type(exc).__name__}: Toxiproxy apply failed",
                packet_loss_applied=False,
            )
        return ApplyResult(
            link_id=link.link_id,
            success=True,
            applied_parameters=applied,
            timestamp_ns=timestamp_ns,
            error=None,
            packet_loss_applied=False,
        )

    def _apply(
        self,
        link: Any,
        desired: NetworkParameters,
        previous_applied: NetworkParameters | None,
    ) -> NetworkParameters:
        if desired.packet_loss_applied:
            raise ValueError("Toxiproxy v2.12.0 cannot apply packet loss")
        if desired.disconnect_mode is not DisconnectMode.NONE:
            disconnect_mode = self._resolve_disconnect_mode(link)
            self._client.delete_toxic(
                link.proxy_name, ToxiproxyClient.DYNAMIC_LATENCY
            )
            self._client.delete_toxic(
                link.proxy_name, ToxiproxyClient.DYNAMIC_BANDWIDTH
            )
            self._client.delete_toxic(
                link.proxy_name, ToxiproxyClient.DYNAMIC_PACKET_LOSS
            )
            self._client.set_disconnected(
                link.proxy_name, disconnect_mode, link.disconnect_stream
            )
            return replace(desired, disconnect_mode=disconnect_mode)

        if previous_applied is None or (
            previous_applied.disconnect_mode is not DisconnectMode.NONE
        ):
            self._client.clear_disconnect(link.proxy_name)
        assert desired.bandwidth_kbps is not None
        assert desired.latency_ms is not None
        assert desired.jitter_ms is not None
        self._client.upsert_bandwidth(
            link.proxy_name, desired.bandwidth_kbps, link.bandwidth_stream
        )
        self._client.upsert_latency(
            link.proxy_name,
            desired.latency_ms,
            desired.jitter_ms,
            link.latency_stream,
        )
        return desired

    @staticmethod
    def _resolve_disconnect_mode(link: Any) -> DisconnectMode:
        configured_mode = getattr(link, "disconnect_mode", "auto")
        if configured_mode != "auto":
            return DisconnectMode(configured_mode)
        if link.protocol is LinkProtocol.MQTT:
            return DisconnectMode.RESET_PEER
        return DisconnectMode.TIMEOUT
