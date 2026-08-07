"""Validated Markov transition model with a caller-owned random source."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

from domain.enums import NetworkState


class MarkovNetworkModel:
    def __init__(
        self,
        states: Sequence[NetworkState],
        transition_matrix: Sequence[Sequence[float]],
        rng: random.Random,
    ) -> None:
        self._states = tuple(states)
        self._matrix = tuple(tuple(row) for row in transition_matrix)
        self._rng = rng
        self._validate()

    @property
    def rng(self) -> random.Random:
        return self._rng

    def next_state(self, current_state: NetworkState) -> NetworkState:
        try:
            current_index = self._states.index(current_state)
        except ValueError as exc:
            raise ValueError(f"unknown current state: {current_state}") from exc

        threshold = self._rng.random()
        cumulative = 0.0
        for state, probability in zip(
            self._states, self._matrix[current_index], strict=True
        ):
            cumulative += probability
            if threshold < cumulative:
                return state
        return self._states[-1]

    def _validate(self) -> None:
        if not self._states:
            raise ValueError("transition states must not be empty")
        if len(set(self._states)) != len(self._states):
            raise ValueError("transition states must be unique")
        if any(not isinstance(state, NetworkState) for state in self._states):
            raise ValueError("transition states must be NetworkState values")

        size = len(self._states)
        if len(self._matrix) != size:
            raise ValueError("transition matrix must be square")
        for row_index, row in enumerate(self._matrix):
            if len(row) != size:
                raise ValueError("transition matrix must be square")
            for probability in row:
                if (
                    isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                    or not math.isfinite(probability)
                ):
                    raise ValueError("transition probabilities must be finite numbers")
                if probability < 0:
                    raise ValueError("transition probabilities cannot be negative")
            if not math.isclose(sum(row), 1.0, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(
                    f"transition matrix row {row_index} probabilities must sum to 1"
                )

