"""API contract validation helpers.

The public JSON shapes are defined by docs/api.md. This module keeps validation
small and explicit so the core demo can run without web-framework dependencies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


DATA_TYPE = "bearing_timeseries"
SAMPLE_RATE_HZ = 16000
SAMPLE_COUNT = 800
DURATION_MS = 50
ROUTES = {"edge", "cloud", "fallback_edge"}
LABELS = {"normal", "abnormal"}
RISK_LEVELS = {"low", "medium", "high"}


class ContractError(ValueError):
    """Raised when an API payload does not match docs/api.md."""

    def __init__(self, code: str, message: str, packet_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.packet_id = packet_id


def error_response(error: ContractError) -> dict[str, Any]:
    return {
        "success": False,
        "packet_id": error.packet_id,
        "error_code": error.code,
        "message": error.message,
    }


def require_mapping(value: Any, name: str, packet_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("INVALID_JSON", f"{name} must be an object", packet_id)
    return value


def require_field(payload: dict[str, Any], field: str, packet_id: str | None = None) -> Any:
    if field not in payload:
        raise ContractError("MISSING_FIELD", f"missing field: {field}", packet_id)
    return payload[field]


def require_number(value: Any, field: str, packet_id: str | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError("INVALID_PACKET", f"{field} must be a number", packet_id)
    return float(value)


def require_int(value: Any, field: str, packet_id: str | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError("INVALID_PACKET", f"{field} must be an integer", packet_id)
    return value


def require_non_empty_string(value: Any, field: str, packet_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("INVALID_PACKET", f"{field} must be a non-empty string", packet_id)
    return value


def require_confidence(value: Any, field: str, packet_id: str | None = None) -> float:
    confidence = require_number(value, field, packet_id)
    if not 0 <= confidence <= 1:
        raise ContractError("INVALID_PACKET", f"{field} must be between 0 and 1", packet_id)
    return confidence


def validate_sensor_packet(payload: dict[str, Any]) -> dict[str, Any]:
    packet_id = payload.get("packet_id") if isinstance(payload, dict) else None
    packet = require_mapping(payload, "SensorPacket", packet_id)
    packet_id = require_non_empty_string(require_field(packet, "packet_id", packet_id), "packet_id", packet_id)
    require_non_empty_string(require_field(packet, "device_id", packet_id), "device_id", packet_id)
    require_non_empty_string(require_field(packet, "sensor_id", packet_id), "sensor_id", packet_id)
    sequence_number = require_int(require_field(packet, "sequence_number", packet_id), "sequence_number", packet_id)
    if sequence_number < 1:
        raise ContractError("INVALID_PACKET", "sequence_number must be greater than 0", packet_id)

    start_ns = require_int(require_field(packet, "start_timestamp_ns", packet_id), "start_timestamp_ns", packet_id)
    end_ns = require_int(require_field(packet, "end_timestamp_ns", packet_id), "end_timestamp_ns", packet_id)
    if end_ns <= start_ns:
        raise ContractError("INVALID_PACKET", "end_timestamp_ns must be greater than start_timestamp_ns", packet_id)
    duration_ms = require_int(require_field(packet, "duration_ms", packet_id), "duration_ms", packet_id)
    if duration_ms != DURATION_MS:
        raise ContractError("INVALID_PACKET", f"duration_ms must be {DURATION_MS}", packet_id)

    data = require_mapping(require_field(packet, "data", packet_id), "data", packet_id)
    if require_field(data, "data_type", packet_id) != DATA_TYPE:
        raise ContractError("INVALID_PACKET", f"data.data_type must be {DATA_TYPE}", packet_id)
    if require_int(require_field(data, "vibration_sample_rate_hz", packet_id), "data.vibration_sample_rate_hz", packet_id) != SAMPLE_RATE_HZ:
        raise ContractError("INVALID_PACKET", f"data.vibration_sample_rate_hz must be {SAMPLE_RATE_HZ}", packet_id)
    sample_count = require_int(require_field(data, "vibration_sample_count", packet_id), "data.vibration_sample_count", packet_id)
    if sample_count != SAMPLE_COUNT:
        raise ContractError("INVALID_PACKET", f"data.vibration_sample_count must be {SAMPLE_COUNT}", packet_id)
    vibration = require_field(data, "vibration", packet_id)
    if not isinstance(vibration, list):
        raise ContractError("INVALID_PACKET", "data.vibration must be an array", packet_id)
    if len(vibration) != sample_count:
        raise ContractError("INVALID_PACKET", "vibration length does not match vibration_sample_count", packet_id)
    for index, item in enumerate(vibration):
        require_number(item, f"data.vibration[{index}]", packet_id)
    require_number(require_field(data, "current", packet_id), "data.current", packet_id)
    require_number(require_field(data, "temperature", packet_id), "data.temperature", packet_id)
    require_number(require_field(data, "speed", packet_id), "data.speed", packet_id)
    load = require_number(require_field(data, "load", packet_id), "data.load", packet_id)
    if not 0 <= load <= 1:
        raise ContractError("INVALID_PACKET", "data.load must be between 0 and 1", packet_id)
    return packet


def validate_edge_result(payload: dict[str, Any], packet_id: str | None = None) -> dict[str, Any]:
    result = require_mapping(payload, "EdgeResult", packet_id)
    result_packet_id = require_non_empty_string(require_field(result, "packet_id", packet_id), "packet_id", packet_id)
    if packet_id is not None and result_packet_id != packet_id:
        raise ContractError("INVALID_PACKET", "edge result packet_id does not match request", packet_id)
    require_non_empty_string(require_field(result, "device_id", result_packet_id), "device_id", result_packet_id)
    require_non_empty_string(require_field(result, "edge_node_id", result_packet_id), "edge_node_id", result_packet_id)
    require_non_empty_string(require_field(result, "model_name", result_packet_id), "model_name", result_packet_id)
    label = require_field(result, "label", result_packet_id)
    if label not in LABELS:
        raise ContractError("INVALID_PACKET", "label must be normal or abnormal", result_packet_id)
    require_confidence(require_field(result, "confidence", result_packet_id), "confidence", result_packet_id)
    if require_field(result, "risk_level", result_packet_id) not in RISK_LEVELS:
        raise ContractError("INVALID_PACKET", "risk_level must be low, medium, or high", result_packet_id)
    if not isinstance(require_field(result, "need_cloud", result_packet_id), bool):
        raise ContractError("INVALID_PACKET", "need_cloud must be boolean", result_packet_id)
    if require_number(require_field(result, "edge_latency_ms", result_packet_id), "edge_latency_ms", result_packet_id) < 0:
        raise ContractError("INVALID_PACKET", "edge_latency_ms must be greater than or equal to 0", result_packet_id)
    return result


def validate_schedule_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = require_mapping(payload, "ScheduleRequest")
    packet = require_mapping(require_field(request, "packet"), "packet")
    packet_id = require_non_empty_string(require_field(packet, "packet_id"), "packet.packet_id")
    require_non_empty_string(require_field(packet, "device_id", packet_id), "packet.device_id", packet_id)
    require_non_empty_string(require_field(packet, "sensor_id", packet_id), "packet.sensor_id", packet_id)
    require_int(require_field(packet, "sequence_number", packet_id), "packet.sequence_number", packet_id)
    if require_int(require_field(packet, "duration_ms", packet_id), "packet.duration_ms", packet_id) != DURATION_MS:
        raise ContractError("INVALID_PACKET", f"packet.duration_ms must be {DURATION_MS}", packet_id)
    if require_field(packet, "data_type", packet_id) != DATA_TYPE:
        raise ContractError("INVALID_PACKET", f"packet.data_type must be {DATA_TYPE}", packet_id)
    if require_int(require_field(packet, "vibration_sample_count", packet_id), "packet.vibration_sample_count", packet_id) != SAMPLE_COUNT:
        raise ContractError("INVALID_PACKET", f"packet.vibration_sample_count must be {SAMPLE_COUNT}", packet_id)
    if require_number(require_field(packet, "payload_size_kb", packet_id), "packet.payload_size_kb", packet_id) <= 0:
        raise ContractError("INVALID_PACKET", "packet.payload_size_kb must be greater than 0", packet_id)

    edge_result = require_mapping(require_field(request, "edge_result", packet_id), "edge_result", packet_id)
    if require_field(edge_result, "label", packet_id) not in LABELS:
        raise ContractError("INVALID_PACKET", "edge_result.label must be normal or abnormal", packet_id)
    require_confidence(require_field(edge_result, "confidence", packet_id), "edge_result.confidence", packet_id)
    if require_field(edge_result, "risk_level", packet_id) not in RISK_LEVELS:
        raise ContractError("INVALID_PACKET", "edge_result.risk_level must be low, medium, or high", packet_id)
    if not isinstance(require_field(edge_result, "need_cloud", packet_id), bool):
        raise ContractError("INVALID_PACKET", "edge_result.need_cloud must be boolean", packet_id)
    if require_number(require_field(edge_result, "edge_latency_ms", packet_id), "edge_result.edge_latency_ms", packet_id) < 0:
        raise ContractError("INVALID_PACKET", "edge_result.edge_latency_ms must be greater than or equal to 0", packet_id)

    network_state = require_mapping(require_field(request, "network_state", packet_id), "network_state", packet_id)
    require_number(require_field(network_state, "latency_ms", packet_id), "network_state.latency_ms", packet_id)
    require_number(require_field(network_state, "bandwidth_mbps", packet_id), "network_state.bandwidth_mbps", packet_id)
    packet_loss = require_number(require_field(network_state, "packet_loss", packet_id), "network_state.packet_loss", packet_id)
    if not 0 <= packet_loss <= 1:
        raise ContractError("INVALID_PACKET", "network_state.packet_loss must be between 0 and 1", packet_id)
    if not isinstance(require_field(network_state, "cloud_available", packet_id), bool):
        raise ContractError("INVALID_PACKET", "network_state.cloud_available must be boolean", packet_id)

    node_state = require_mapping(require_field(request, "node_state", packet_id), "node_state", packet_id)
    require_number(require_field(node_state, "edge_cpu_usage", packet_id), "node_state.edge_cpu_usage", packet_id)
    require_number(require_field(node_state, "edge_memory_usage", packet_id), "node_state.edge_memory_usage", packet_id)
    require_int(require_field(node_state, "cloud_queue_length", packet_id), "node_state.cloud_queue_length", packet_id)
    return request


def validate_schedule_decision(payload: dict[str, Any], packet_id: str | None = None) -> dict[str, Any]:
    decision = require_mapping(payload, "ScheduleDecision", packet_id)
    result_packet_id = require_non_empty_string(require_field(decision, "packet_id", packet_id), "packet_id", packet_id)
    if packet_id is not None and result_packet_id != packet_id:
        raise ContractError("INVALID_PACKET", "schedule decision packet_id does not match request", packet_id)
    route = require_field(decision, "route", result_packet_id)
    if route not in ROUTES:
        raise ContractError("INVALID_PACKET", "route must be edge, cloud, or fallback_edge", result_packet_id)
    require_non_empty_string(require_field(decision, "target_node", result_packet_id), "target_node", result_packet_id)
    if not isinstance(require_field(decision, "upload_required", result_packet_id), bool):
        raise ContractError("INVALID_PACKET", "upload_required must be boolean", result_packet_id)
    require_non_empty_string(require_field(decision, "reason", result_packet_id), "reason", result_packet_id)
    if require_number(require_field(decision, "estimated_total_latency_ms", result_packet_id), "estimated_total_latency_ms", result_packet_id) < 0:
        raise ContractError("INVALID_PACKET", "estimated_total_latency_ms must be greater than or equal to 0", result_packet_id)
    return decision


def validate_cloud_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = require_mapping(payload, "CloudRequest")
    packet = validate_sensor_packet(require_mapping(require_field(request, "packet"), "packet"))
    packet_id = packet["packet_id"]
    edge_result = require_mapping(require_field(request, "edge_result", packet_id), "edge_result", packet_id)
    if require_field(edge_result, "label", packet_id) not in LABELS:
        raise ContractError("INVALID_PACKET", "edge_result.label must be normal or abnormal", packet_id)
    require_confidence(require_field(edge_result, "confidence", packet_id), "edge_result.confidence", packet_id)
    if require_field(edge_result, "risk_level", packet_id) not in RISK_LEVELS:
        raise ContractError("INVALID_PACKET", "edge_result.risk_level must be low, medium, or high", packet_id)
    return request


def validate_task_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace = require_mapping(payload, "TaskTrace")
    packet_id = require_non_empty_string(require_field(trace, "packet_id"), "packet_id")
    require_non_empty_string(require_field(trace, "device_id", packet_id), "device_id", packet_id)
    require_non_empty_string(require_field(trace, "sensor_id", packet_id), "sensor_id", packet_id)
    require_int(require_field(trace, "sequence_number", packet_id), "sequence_number", packet_id)
    if require_field(trace, "data_type", packet_id) != DATA_TYPE:
        raise ContractError("INVALID_PACKET", f"data_type must be {DATA_TYPE}", packet_id)
    if require_field(trace, "route", packet_id) not in ROUTES:
        raise ContractError("INVALID_PACKET", "route must be edge, cloud, or fallback_edge", packet_id)
    if require_field(trace, "edge_label", packet_id) not in LABELS:
        raise ContractError("INVALID_PACKET", "edge_label must be normal or abnormal", packet_id)
    require_confidence(require_field(trace, "edge_confidence", packet_id), "edge_confidence", packet_id)
    cloud_confidence = trace.get("cloud_confidence")
    if cloud_confidence is not None:
        require_confidence(cloud_confidence, "cloud_confidence", packet_id)
    if require_field(trace, "final_label", packet_id) not in LABELS:
        raise ContractError("INVALID_PACKET", "final_label must be normal or abnormal", packet_id)
    require_confidence(require_field(trace, "final_confidence", packet_id), "final_confidence", packet_id)
    if require_field(trace, "risk_level", packet_id) not in RISK_LEVELS:
        raise ContractError("INVALID_PACKET", "risk_level must be low, medium, or high", packet_id)
    for field in ("edge_latency_ms", "total_latency_ms"):
        if require_number(require_field(trace, field, packet_id), field, packet_id) < 0:
            raise ContractError("INVALID_PACKET", f"{field} must be greater than or equal to 0", packet_id)
    for field in ("network_latency_ms", "cloud_latency_ms"):
        value = trace.get(field)
        if value is not None and require_number(value, field, packet_id) < 0:
            raise ContractError("INVALID_PACKET", f"{field} must be greater than or equal to 0", packet_id)
    if not isinstance(require_field(trace, "success", packet_id), bool):
        raise ContractError("INVALID_PACKET", "success must be boolean", packet_id)
    log_timestamp = require_non_empty_string(require_field(trace, "log_timestamp", packet_id), "log_timestamp", packet_id)
    try:
        datetime.fromisoformat(log_timestamp)
    except ValueError as exc:
        raise ContractError("INVALID_PACKET", "log_timestamp must be ISO 8601", packet_id) from exc
    return trace


def compact_packet_for_scheduler(packet: dict[str, Any], payload_size_kb: float) -> dict[str, Any]:
    data = packet["data"]
    return {
        "packet_id": packet["packet_id"],
        "device_id": packet["device_id"],
        "sensor_id": packet["sensor_id"],
        "sequence_number": packet["sequence_number"],
        "duration_ms": packet["duration_ms"],
        "data_type": data["data_type"],
        "vibration_sample_count": data["vibration_sample_count"],
        "payload_size_kb": payload_size_kb,
    }

