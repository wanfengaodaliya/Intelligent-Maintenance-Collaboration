"""Legacy V1.2 scenario defaults kept outside the generic core."""

from __future__ import annotations


BEARING_SCENARIO_TYPE = "bearing"


def normalize_legacy_scenario_type(value: object) -> str:
    """Preserve the V1.2 missing-scenario behavior at the compatibility edge."""

    if value is None:
        return BEARING_SCENARIO_TYPE
    scenario_type = str(value).strip().lower()
    return scenario_type or BEARING_SCENARIO_TYPE
