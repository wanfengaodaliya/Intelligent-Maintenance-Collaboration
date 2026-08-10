"""Lifecycle wrapper and per-link state-generation engines."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from domain.enums import NetworkState
from domain.models import NetworkParameters
from plugins.base import BasePlugin, PluginContext

from .mapper import NetworkStateMapper


class StateModel(Protocol):
    def next_state(self, current_state: NetworkState) -> NetworkState: ...


@dataclass(slots=True)
class LinkStateEngine:
    model: StateModel
    mapper: NetworkStateMapper
    rng: random.Random
    refresh_parameters_when_state_unchanged: bool

    def generate(
        self,
        current_state: NetworkState,
        current_parameters: NetworkParameters | None,
    ) -> NetworkParameters:
        next_state = self.model.next_state(current_state)
        if (
            next_state is current_state
            and not self.refresh_parameters_when_state_unchanged
            and current_parameters is not None
            and current_parameters.state is current_state
        ):
            return current_parameters
        return self.mapper.sample(next_state)


class MarkovPlugin(BasePlugin):
    name = "markov"
    required = True

    def __init__(self, engines: dict[str, LinkStateEngine]) -> None:
        self._engines = dict(engines)
        self._context: PluginContext | None = None
        self._started = False
        self._stopped = False

    def initialize(self, context: PluginContext) -> None:
        if self._context is not None:
            raise RuntimeError("markov plugin is already initialized")
        runtime_link_ids = {link.link_id for link in context.runtime_store.list_links()}
        if runtime_link_ids != set(self._engines):
            raise ValueError("runtime links and state engines do not match")
        self._context = context
        self._stopped = False

    def start(self) -> None:
        if self._context is None:
            raise RuntimeError("markov plugin must be initialized before start")
        if self._started:
            raise RuntimeError("markov plugin is already started")
        self._started = True
        self._stopped = False

    def stop(self) -> None:
        self._started = False
        self._stopped = True

    def health(self) -> dict[str, object]:
        if self._started:
            status = "ok"
        elif self._stopped:
            status = "stopped"
        else:
            status = "initialized" if self._context is not None else "created"
        return {"status": status, "link_count": len(self._engines)}

    def generate(self, link_id: str) -> NetworkParameters:
        if not self._started or self._context is None:
            raise RuntimeError("markov plugin must be started before generation")
        try:
            engine = self._engines[link_id]
        except KeyError as exc:
            raise KeyError(f"unknown link_id: {link_id}") from exc
        snapshot = self._context.runtime_store.get_link(link_id)
        return engine.generate(snapshot.current_state, snapshot.desired_parameters)
