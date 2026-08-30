"""Map legacy bearing sender configuration onto the generic unit identity."""

from __future__ import annotations

from typing import Any, Mapping


def _optional_text(raw: Mapping[str, Any], field: str, prefix: str) -> str | None:
    if field not in raw:
        return None
    value = raw[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}.{field} must be a non-empty string")
    return value.strip()


def resolve_unit_id(raw: Mapping[str, Any], prefix: str) -> str:
    """Resolve new ``unit_id`` and legacy ``bearing_id`` without ambiguity."""

    unit_id = _optional_text(raw, "unit_id", prefix)
    bearing_id = _optional_text(raw, "bearing_id", prefix)
    if unit_id is None and bearing_id is None:
        raise ValueError(f"{prefix} missing fields: unit_id or bearing_id")
    if unit_id is not None and bearing_id is not None and unit_id != bearing_id:
        raise ValueError(f"{prefix}.unit_id and bearing_id must match")
    if unit_id is not None:
        return unit_id
    assert bearing_id is not None
    return bearing_id
