"""Health plugin that safely aggregates runtime and dependency status."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from plugins.base import BasePlugin, PluginContext


HealthProvider = Callable[[], Mapping[str, Any]]


class HealthPlugin(BasePlugin):
    name = "health"
    dependencies = ()
    required = False

    def __init__(
        self,
        *,
        toxiproxy_health: HealthProvider | None = None,
        reporter_health: HealthProvider | None = None,
    ) -> None:
        self._toxiproxy_health = toxiproxy_health
        self._reporter_health = reporter_health
        self._context: PluginContext | None = None
        self._started = False
        self._stopped = False

    def initialize(self, context: PluginContext) -> None:
        if self._context is not None:
            raise RuntimeError("Health plugin is already initialized")
        self._context = context
        self._stopped = False

    def start(self) -> None:
        if self._context is None:
            raise RuntimeError("Health plugin must be initialized before start")
        if self._started:
            raise RuntimeError("Health plugin is already started")
        if self._stopped:
            raise RuntimeError("stopped Health plugin cannot be restarted")
        self._started = True

    def stop(self) -> None:
        self._started = False
        self._stopped = True

    def health(self) -> dict[str, object]:
        if self._context is None:
            return {
                "status": "created",
                "toxiproxy_available": False,
                "scheduler_reporter_healthy": False,
                "link_count": 0,
                "available_link_count": 0,
                "last_tick": 0,
            }
        if self._stopped:
            lifecycle_status = "stopped"
        elif not self._started:
            lifecycle_status = "initialized"
        else:
            lifecycle_status = None
        try:
            runtime_snapshot = self._context.runtime_store.snapshot()
        except Exception:
            return {
                "status": "unhealthy",
                "toxiproxy_available": False,
                "scheduler_reporter_healthy": False,
                "link_count": 0,
                "available_link_count": 0,
                "last_tick": 0,
            }
        toxiproxy_available = self._provider_is_ok(self._toxiproxy_health)
        reporter_enabled = bool(self._context.config.reporter.enabled)
        reporter_healthy = (
            not reporter_enabled
            or self._provider_is_ok(self._reporter_health)
        )
        if lifecycle_status is not None:
            status = lifecycle_status
        elif not toxiproxy_available:
            status = "unhealthy"
        elif not reporter_healthy:
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "toxiproxy_available": toxiproxy_available,
            "scheduler_reporter_healthy": reporter_healthy,
            "scheduler_reporter": self._reporter_detail(),
            "link_count": len(runtime_snapshot.links),
            "available_link_count": sum(
                link.available for link in runtime_snapshot.links
            ),
            "last_tick": runtime_snapshot.tick,
        }

    def _reporter_detail(self) -> dict[str, object] | None:
        """AUD-13: expose reporter observability fields for debugging."""
        if not self._reporter_health or not self._context:
            return None
        try:
            detail = dict(self._reporter_health())
        except Exception:
            return None
        fields = (
            "status",
            "last_error",
            "last_success_ns",
            "last_failure_ns",
            "consecutive_failures",
            "last_rejected_count",
            "last_outcome",
            "partial_failure_count",
        )
        return {name: detail.get(name) for name in fields}

    @staticmethod
    def _provider_is_ok(provider: HealthProvider | None) -> bool:
        if provider is None:
            return False
        try:
            return provider().get("status") == "ok"
        except Exception:
            return False
