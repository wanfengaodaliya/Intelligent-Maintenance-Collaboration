"""Compatibility exports for bearing summary persistence."""

from scenarios.bearing.summary_service.repository import (
    BearingResultConflictError,
    SCHEMA_V2,
    SCHEMA_VERSION,
    SummaryRepository,
    _sync_projection,
)

__all__ = [
    "BearingResultConflictError",
    "SCHEMA_V2",
    "SCHEMA_VERSION",
    "SummaryRepository",
]
