"""Thread-safe single source of truth for current link runtime state."""

from __future__ import annotations

from copy import deepcopy
import math
from threading import RLock
import time
from typing import Iterable

from domain.enums import NetworkState
from domain.models import (
    SCORE_COMPONENT_NAMES,
    LinkRuntime,
    LinkSnapshot,
    NetworkParameters,
    RuntimeSnapshot,
)


class RuntimeStore:
    def __init__(self, links: Iterable[LinkRuntime]) -> None:
        self._lock = RLock()
        self._links: dict[str, LinkRuntime] = {}
        self._generation_by_link: dict[str, int] = {}
        self._applied_generation_by_link: dict[str, int] = {}
        for link in links:
            if link.link_id in self._links:
                raise ValueError(f"duplicate runtime link_id: {link.link_id}")
            self._links[link.link_id] = deepcopy(link)
            self._generation_by_link[link.link_id] = 0
            self._applied_generation_by_link[link.link_id] = (
                0 if link.applied_parameters is not None else -1
            )
        self._last_tick = 0
        self._last_tick_timestamp_ns: int | None = None

    @property
    def last_tick(self) -> int:
        with self._lock:
            return self._last_tick

    @property
    def last_tick_timestamp_ns(self) -> int | None:
        with self._lock:
            return self._last_tick_timestamp_ns

    def get_link(self, link_id: str) -> LinkSnapshot:
        with self._lock:
            return self._require_link(link_id).to_snapshot()

    def list_links(self) -> tuple[LinkSnapshot, ...]:
        with self._lock:
            return tuple(link.to_snapshot() for link in self._links.values())

    def update_generated_state(
        self,
        link_id: str,
        state: NetworkState,
        parameters: NetworkParameters,
        generated_at_ns: int,
        generation: int,
    ) -> LinkSnapshot:
        if parameters.state is not state:
            raise ValueError("generated state must match desired parameters state")
        if generated_at_ns < 0:
            raise ValueError("generated_at_ns cannot be negative")
        if generation <= 0:
            raise ValueError("generation must be greater than zero")

        with self._lock:
            link = self._require_link(link_id)
            if generation <= self._generation_by_link[link_id]:
                raise ValueError("generated state generation must increase")
            old_state = link.current_state
            link.previous_state = old_state
            link.current_state = state
            if state is not old_state:
                link.state_since_ns = generated_at_ns
            link.desired_parameters = parameters
            self._generation_by_link[link_id] = generation
            return link.to_snapshot()

    def update_generation_failure(
        self,
        link_id: str,
        *,
        generation: int,
        error: str,
    ) -> LinkSnapshot:
        if generation <= 0:
            raise ValueError("generation must be greater than zero")
        if not error:
            raise ValueError("generation failure error cannot be empty")

        with self._lock:
            link = self._require_link(link_id)
            if generation <= self._generation_by_link[link_id]:
                raise ValueError("generated state generation must increase")
            self._generation_by_link[link_id] = generation
            link.link_reliability_score = 0.0
            link.score_components = {
                name: 0.0 for name in SCORE_COMPONENT_NAMES
            }
            link.available = False
            link.last_apply_success = False
            link.last_error = error
            return link.to_snapshot()

    def update_apply_result(
        self,
        link_id: str,
        *,
        success: bool,
        applied_parameters: NetworkParameters | None,
        timestamp_ns: int,
        generation: int,
        error: str | None = None,
    ) -> LinkSnapshot:
        if timestamp_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")
        if success and applied_parameters is None:
            raise ValueError("successful apply requires applied_parameters")
        if not success and applied_parameters is not None:
            raise ValueError("failed apply must not replace applied_parameters")
        if success and error is not None:
            raise ValueError("successful apply cannot include an error")

        with self._lock:
            link = self._require_link(link_id)
            if generation != self._generation_by_link[link_id]:
                raise ValueError("stale apply result generation")
            if (
                success
                and applied_parameters is not None
                and applied_parameters.state is not link.current_state
            ):
                raise ValueError("applied parameters state must match current state")
            link.last_apply_success = success
            link.last_apply_timestamp_ns = timestamp_ns
            if success:
                previous_applied = link.applied_parameters
                link.applied_parameters = applied_parameters
                if (
                    link.applied_state_since_ns is None
                    or previous_applied is None
                    or previous_applied.state is not applied_parameters.state
                ):
                    link.applied_state_since_ns = timestamp_ns
                link.consecutive_apply_failures = 0
                link.last_error = None
            else:
                link.consecutive_apply_failures += 1
                link.last_error = error or "Toxiproxy apply failed"
            self._applied_generation_by_link[link_id] = generation
            return link.to_snapshot()

    def update_score(
        self,
        link_id: str,
        *,
        score: float,
        components: dict[str, float],
        available: bool,
        generation: int,
    ) -> LinkSnapshot:
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("score must be a finite value between 0 and 100")
        if any(
            not math.isfinite(value) or not 0 <= value <= 100
            for value in components.values()
        ):
            raise ValueError("score components must be finite values from 0 to 100")

        with self._lock:
            link = self._require_link(link_id)
            if generation != self._generation_by_link[link_id]:
                raise ValueError("stale score result generation")
            if generation != self._applied_generation_by_link[link_id]:
                raise ValueError("score requires an apply result for the generation")
            link.link_reliability_score = score
            link.score_components = dict(components)
            link.available = available
            return link.to_snapshot()

    def complete_tick(self, tick: int, timestamp_ns: int) -> None:
        if tick <= 0:
            raise ValueError("tick must be greater than zero")
        if timestamp_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")
        with self._lock:
            if tick <= self._last_tick:
                raise ValueError("tick must increase monotonically")
            self._last_tick = tick
            self._last_tick_timestamp_ns = timestamp_ns

    def snapshot(self, generated_at_ns: int | None = None) -> RuntimeSnapshot:
        timestamp_ns = time.time_ns() if generated_at_ns is None else generated_at_ns
        if timestamp_ns < 0:
            raise ValueError("generated_at_ns cannot be negative")
        with self._lock:
            return RuntimeSnapshot(
                tick=self._last_tick,
                generated_at_ns=timestamp_ns,
                links=tuple(link.to_snapshot() for link in self._links.values()),
            )

    def _require_link(self, link_id: str) -> LinkRuntime:
        try:
            return self._links[link_id]
        except KeyError as exc:
            raise KeyError(f"unknown link_id: {link_id}") from exc
