"""Bearing cloud diagnosis provider."""

from __future__ import annotations

from typing import Any


__all__ = ["BearingCloudDiagnosisProvider"]


def __getattr__(name: str) -> Any:
    """Avoid loading cloud orchestration when importing runtime submodules."""

    if name == "BearingCloudDiagnosisProvider":
        from .provider import BearingCloudDiagnosisProvider

        return BearingCloudDiagnosisProvider
    raise AttributeError(name)
