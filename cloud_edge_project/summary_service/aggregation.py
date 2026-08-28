"""Compatibility exports for bearing summary aggregation."""

from scenarios.bearing.summary_service.aggregation import (
    build_arbitration_request,
    build_incomplete_window_result,
    build_window_result,
)

__all__ = [
    "build_arbitration_request",
    "build_incomplete_window_result",
    "build_window_result",
]
