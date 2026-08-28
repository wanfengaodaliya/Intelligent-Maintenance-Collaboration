"""Backward-compatible ASGI entry for the bearing summary plugin."""

from scenarios.bearing.summary_service.app import (
    app,
    health,
    ingest_bearing_result,
    list_window_results,
    metrics,
)

__all__ = [
    "app",
    "health",
    "ingest_bearing_result",
    "list_window_results",
    "metrics",
]
