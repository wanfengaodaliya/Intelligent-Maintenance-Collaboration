"""Compatibility helpers for the existing bearing V1.2 contracts."""

from compatibility.bearing_v12.config_mapper import resolve_unit_id
from compatibility.bearing_v12.scenario_mapper import (
    BEARING_SCENARIO_TYPE,
    normalize_legacy_scenario_type,
)

__all__ = [
    "BEARING_SCENARIO_TYPE",
    "normalize_legacy_scenario_type",
    "resolve_unit_id",
]
