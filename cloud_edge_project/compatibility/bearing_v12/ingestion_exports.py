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
from scenarios.bearing.ingestion.packet import (
    ARRAY_SIGNALS,
    BEARING_ID_PATTERN,
    SENDER_ID_PATTERN,
    TASK_ID_PATTERN,
    TEMPERATURE_SIGNAL,
    PacketValidationError,
    build_sensor_packet,
    serialize_packet,
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
    "PacketValidationError",
    "ARRAY_SIGNALS",
    "TEMPERATURE_SIGNAL",
    "TASK_ID_PATTERN",
    "SENDER_ID_PATTERN",
    "BEARING_ID_PATTERN",
    "build_sensor_packet",
    "serialize_packet",
]
