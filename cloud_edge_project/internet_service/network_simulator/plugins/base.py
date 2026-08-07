"""Common plugin lifecycle contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event
from typing import Any


@dataclass(frozen=True, slots=True)
class PluginContext:
    config: Any
    runtime_store: Any
    event_bus: Any
    shutdown_event: Event


class BasePlugin(ABC):
    name: str
    dependencies: tuple[str, ...] = ()
    required: bool = False

    @abstractmethod
    def initialize(self, context: PluginContext) -> None:
        """Bind dependencies and validate configuration without starting tasks."""

    @abstractmethod
    def start(self) -> None:
        """Start background work owned by the plugin."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the plugin; implementations must be idempotent."""

    @abstractmethod
    def health(self) -> dict[str, object]:
        """Return health metadata without raising an exception."""
