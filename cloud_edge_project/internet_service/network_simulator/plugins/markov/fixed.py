"""Fixed-state model used by deterministic experiments."""

from __future__ import annotations

from domain.enums import NetworkState


class FixedNetworkModel:
    def __init__(self, state: NetworkState) -> None:
        if not isinstance(state, NetworkState):
            raise ValueError("fixed state must be a NetworkState value")
        self._state = state

    def next_state(self, current_state: NetworkState) -> NetworkState:
        del current_state
        return self._state
