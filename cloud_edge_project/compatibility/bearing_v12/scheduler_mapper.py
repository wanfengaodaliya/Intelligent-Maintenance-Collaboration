"""Translate bearing V1.2 scheduler payloads to a generic in-memory shape."""

from __future__ import annotations

from typing import Any, Mapping


EDGE_INFERENCE = "edge_inference"
LEGACY_EDGE_INFERENCE = "BEARING_EDGE_INFERENCE"

_FIELD_ALIASES = (
    ("low_confidence_unit_count", "low_confidence_bearing_count"),
    ("provisional_unit_count", "provisional_bearing_count"),
    ("expected_unit_count", "expected_bearing_count"),
    ("received_unit_count", "received_bearing_count"),
    ("unit_result_ids", "bearing_result_ids"),
    ("unit_result_id", "bearing_result_id"),
    ("unit_results", "bearing_results"),
    ("unit_id", "bearing_id"),
)
_GENERIC_FIELDS = frozenset(generic for generic, _legacy in _FIELD_ALIASES)


class SchedulerMappingError(ValueError):
    pass


def uses_generic_scheduler_fields(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(_GENERIC_FIELDS.intersection(value)) or any(
            uses_generic_scheduler_fields(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(uses_generic_scheduler_fields(item) for item in value)
    return False


def legacy_scheduler_error_message(message: str) -> str:
    result = message.replace("unit result", "bearing result")
    for generic, legacy in _FIELD_ALIASES:
        result = result.replace(generic, legacy)
        result = result.replace(f"{legacy} or {legacy}", legacy)
    return result


def _domain_alias(
    payload: Mapping[str, Any],
    generic: str,
    legacy: str,
) -> Any:
    has_generic = generic in payload
    has_legacy = legacy in payload
    if not has_generic and not has_legacy:
        raise SchedulerMappingError(f"missing fields: {generic} or {legacy}")
    if has_generic and has_legacy and payload[generic] != payload[legacy]:
        raise SchedulerMappingError(f"{generic} and {legacy} must match")
    return payload[generic] if has_generic else payload[legacy]


def _replace_alias(
    payload: Mapping[str, Any],
    generic: str,
    legacy: str,
) -> dict[str, Any]:
    result = dict(payload)
    result[generic] = _domain_alias(payload, generic, legacy)
    result.pop(legacy, None)
    return result


def _legacy_alias(
    payload: Mapping[str, Any],
    generic: str,
    legacy: str,
) -> dict[str, Any]:
    result = dict(payload)
    if generic in result:
        result[legacy] = result.pop(generic)
    return result


def assignment_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _replace_alias(payload, "unit_id", "bearing_id")


def assignment_to_legacy(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _legacy_alias(payload, "unit_id", "bearing_id")


def assignment_row_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _replace_alias(payload, "unit_id", "bearing_id")


def packet_result_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _replace_alias(payload, "unit_id", "bearing_id")
    input_ref = result.get("input_ref")
    if isinstance(input_ref, Mapping):
        result["input_ref"] = _replace_alias(input_ref, "unit_id", "bearing_id")
    return result


def packet_result_to_legacy(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _legacy_alias(payload, "unit_id", "bearing_id")
    input_ref = result.get("input_ref")
    if isinstance(input_ref, Mapping):
        result["input_ref"] = _legacy_alias(input_ref, "unit_id", "bearing_id")
    return result


def packet_decision_to_legacy(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _legacy_alias(payload, "unit_id", "bearing_id")


def packet_decision_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _replace_alias(payload, "unit_id", "bearing_id")


def _unit_result_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _replace_alias(payload, "unit_id", "bearing_id")
    if "bearing_result_id" in result or "unit_result_id" in result:
        result = _replace_alias(result, "unit_result_id", "bearing_result_id")
    return result


def _unit_result_to_legacy(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _legacy_alias(payload, "unit_id", "bearing_id")
    return _legacy_alias(result, "unit_result_id", "bearing_result_id")


def _unit_results_to_domain(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [
        _unit_result_to_domain(item) if isinstance(item, Mapping) else item
        for item in value
    ]


def device_request_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    has_generic_results = "unit_results" in payload
    has_legacy_results = "bearing_results" in payload
    if not has_generic_results and not has_legacy_results:
        raise SchedulerMappingError("missing fields: unit_results or bearing_results")
    generic_results = (
        _unit_results_to_domain(payload["unit_results"])
        if has_generic_results
        else None
    )
    legacy_results = (
        _unit_results_to_domain(payload["bearing_results"])
        if has_legacy_results
        else None
    )
    if (
        has_generic_results
        and has_legacy_results
        and generic_results != legacy_results
    ):
        raise SchedulerMappingError("unit_results and bearing_results must match")
    result = dict(payload)
    result["unit_results"] = generic_results if has_generic_results else legacy_results
    result.pop("bearing_results", None)
    result = _replace_alias(result, "expected_unit_count", "expected_bearing_count")
    result = _replace_alias(result, "received_unit_count", "received_bearing_count")
    if "bearing_result_ids" in result or "unit_result_ids" in result:
        result = _replace_alias(result, "unit_result_ids", "bearing_result_ids")
    comparison = result.get("comparison")
    if isinstance(comparison, Mapping):
        domain_comparison = _replace_alias(
            comparison,
            "low_confidence_unit_count",
            "low_confidence_bearing_count",
        )
        domain_comparison = _replace_alias(
            domain_comparison,
            "provisional_unit_count",
            "provisional_bearing_count",
        )
        result["comparison"] = domain_comparison
    return result


def device_payload_to_legacy(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _legacy_alias(payload, "unit_results", "bearing_results")
    result = _legacy_alias(result, "expected_unit_count", "expected_bearing_count")
    result = _legacy_alias(result, "received_unit_count", "received_bearing_count")
    result = _legacy_alias(result, "unit_result_ids", "bearing_result_ids")
    bearing_results = result.get("bearing_results")
    if isinstance(bearing_results, list):
        result["bearing_results"] = [
            _unit_result_to_legacy(item) if isinstance(item, Mapping) else item
            for item in bearing_results
        ]
    comparison = result.get("comparison")
    if isinstance(comparison, Mapping):
        legacy_comparison = _legacy_alias(
            comparison,
            "low_confidence_unit_count",
            "low_confidence_bearing_count",
        )
        legacy_comparison = _legacy_alias(
            legacy_comparison,
            "provisional_unit_count",
            "provisional_bearing_count",
        )
        result["comparison"] = legacy_comparison
    return result


def capability_to_domain(value: Any) -> Any:
    return EDGE_INFERENCE if value == LEGACY_EDGE_INFERENCE else value


def capability_to_legacy(value: Any) -> Any:
    return LEGACY_EDGE_INFERENCE if value == EDGE_INFERENCE else value
