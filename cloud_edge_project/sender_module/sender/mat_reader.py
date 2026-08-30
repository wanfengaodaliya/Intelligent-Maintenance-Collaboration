"""Compatibility shim for the scenario-owned bearing MAT reader."""

from compatibility.bearing_v12.ingestion_exports import (
    MatDataError,
    MatRecord,
    PACKET_UNITS,
    SOURCE_TO_PACKET,
    TEMPERATURE_SOURCE,
    SignalSeries,
    SignalWindow,
    load_mat_record,
)

__all__ = [
    "MatDataError",
    "SignalSeries",
    "SignalWindow",
    "MatRecord",
    "SOURCE_TO_PACKET",
    "PACKET_UNITS",
    "TEMPERATURE_SOURCE",
    "load_mat_record",
]
