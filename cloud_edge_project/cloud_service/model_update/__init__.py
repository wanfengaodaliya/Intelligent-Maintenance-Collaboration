"""Cloud-side model update lifecycle."""

from __future__ import annotations

from typing import Any


__all__ = ["ModelUpdateError", "ModelUpdateService"]


def __getattr__(name: str) -> Any:
    """Avoid coupling independent contracts to service import-time dependencies."""

    if name in __all__:
        from .service import ModelUpdateError, ModelUpdateService

        return {
            "ModelUpdateError": ModelUpdateError,
            "ModelUpdateService": ModelUpdateService,
        }[name]
    raise AttributeError(name)
