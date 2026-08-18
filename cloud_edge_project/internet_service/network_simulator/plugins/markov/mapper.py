"""Map a V3 network state to one sampled set of desired parameters."""

from __future__ import annotations

import random
from collections.abc import Mapping

from controller.config_loader import FloatRange, IntegerRange, StateConfig
from domain.enums import DisconnectMode, NetworkState
from domain.models import NetworkParameters


class NetworkStateMapper:
    def __init__(
        self,
        state_configs: Mapping[NetworkState, StateConfig],
        rng: random.Random,
    ) -> None:
        self._state_configs = state_configs
        self._rng = rng

    def sample(self, state: NetworkState) -> NetworkParameters:
        try:
            config = self._state_configs[state]
        except KeyError as exc:
            raise ValueError(f"unknown network state: {state}") from exc

        disconnect_mode = DisconnectMode(config.disconnect_mode)
        if disconnect_mode is not DisconnectMode.NONE:
            return NetworkParameters(
                state=state,
                latency_ms=None,
                jitter_ms=None,
                bandwidth_kbps=None,
                packet_loss_percent=float(config.packet_loss_percent),
                disconnect_mode=disconnect_mode,
                packet_loss_applied=False,
            )

        latency = self._require_integer_range(config.latency_ms, state, "latency")
        jitter = self._require_integer_range(config.jitter_ms, state, "jitter")
        bandwidth = self._require_integer_range(
            config.bandwidth_kbps, state, "bandwidth"
        )
        loss = config.packet_loss_percent
        if not isinstance(loss, FloatRange):
            raise ValueError(f"connected state {state.value} has invalid packet loss")

        return NetworkParameters(
            state=state,
            latency_ms=self._rng.randint(latency.min, latency.max),
            jitter_ms=self._rng.randint(jitter.min, jitter.max),
            bandwidth_kbps=self._rng.randint(bandwidth.min, bandwidth.max),
            packet_loss_percent=self._rng.uniform(loss.min, loss.max),
            disconnect_mode=DisconnectMode.NONE,
            packet_loss_applied=False,
        )

    @staticmethod
    def _require_integer_range(
        value: IntegerRange | None,
        state: NetworkState,
        name: str,
    ) -> IntegerRange:
        if value is None:
            raise ValueError(f"connected state {state.value} has no {name} range")
        return value

