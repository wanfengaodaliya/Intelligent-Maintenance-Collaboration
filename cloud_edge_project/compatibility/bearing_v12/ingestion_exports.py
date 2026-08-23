"""Legacy bearing-ingestion exports backed by scenario-owned implementations."""

from scenarios.bearing.ingestion.mat_reader import (
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
