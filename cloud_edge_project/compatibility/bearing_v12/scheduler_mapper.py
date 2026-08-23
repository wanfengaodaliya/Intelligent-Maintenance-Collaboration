"""Translate bearing V1.2 scheduler payloads to a generic in-memory shape."""

from __future__ import annotations

from typing import Any, Mapping


EDGE_INFERENCE = "edge_inference"
LEGACY_EDGE_INFERENCE = "BEARING_EDGE_INFERENCE"


class SchedulerMappingError(ValueError):
    pass


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


def device_request_to_domain(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _replace_alias(payload, "unit_results", "bearing_results")
    result = _replace_alias(result, "expected_unit_count", "expected_bearing_count")
    result = _replace_alias(result, "received_unit_count", "received_bearing_count")
    if "bearing_result_ids" in result or "unit_result_ids" in result:
        result = _replace_alias(result, "unit_result_ids", "bearing_result_ids")
    unit_results = result.get("unit_results")
    if isinstance(unit_results, list):
        result["unit_results"] = [
            _unit_result_to_domain(item) if isinstance(item, Mapping) else item
            for item in unit_results
        ]
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
