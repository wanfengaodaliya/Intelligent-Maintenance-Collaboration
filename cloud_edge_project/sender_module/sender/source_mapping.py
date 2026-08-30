"""Compatibility shim for scenario-owned bearing source mapping."""

from compatibility.bearing_v12.ingestion_exports import (
    PADERBORN_FILENAME,
    PacketSourceMappingStore,
    extract_paderborn_bearing_code,
)

__all__ = [
    "PADERBORN_FILENAME",
    "extract_paderborn_bearing_code",
    "PacketSourceMappingStore",
]
