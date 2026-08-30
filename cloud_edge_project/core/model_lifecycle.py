"""Scenario-neutral model identity and lifecycle metadata contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class ModelDescriptor:
    model_id: str
    family: str
    default_version: str
    description: str
    checksum_algorithm: str = "sha256"
    compatibility: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("model_id", "family", "default_version", "description"):
            _require_identifier(name, getattr(self, name))
        if self.checksum_algorithm != "sha256":
            raise ValueError("checksum_algorithm must be sha256")
        compatibility = dict(self.compatibility)
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in compatibility.items()
        ):
            raise ValueError("compatibility must contain non-empty string pairs")
        object.__setattr__(self, "compatibility", MappingProxyType(compatibility))


@dataclass(frozen=True)
class ModelCatalog:
    scenario_id: str
    default_model_id: str
    models: Mapping[str, ModelDescriptor]

    def __post_init__(self) -> None:
        _require_identifier("scenario_id", self.scenario_id)
        _require_identifier("default_model_id", self.default_model_id)
        models = dict(self.models)
        if not models:
            raise ValueError("models must not be empty")
        if any(key != descriptor.model_id for key, descriptor in models.items()):
            raise ValueError("model keys must match descriptor model_id values")
        if self.default_model_id not in models:
            raise ValueError("default_model_id must reference a catalog model")
        object.__setattr__(self, "models", MappingProxyType(models))

    def require(self, model_id: object) -> ModelDescriptor:
        try:
            return self.models[model_id]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise ValueError(f"UNSUPPORTED_MODEL_TYPE={model_id}") from error
