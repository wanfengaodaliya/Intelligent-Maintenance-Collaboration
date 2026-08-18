"""Inference boundary for a future concrete candidate model format."""

from __future__ import annotations

from typing import Any, Protocol


class CandidateModelAdapter(Protocol):
    def predict(self, features: dict[str, Any]) -> str: ...
