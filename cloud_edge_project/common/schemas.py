"""API contract validation helpers.

The public JSON shapes are defined by docs/api.md. This module keeps validation
small and explicit so the core demo can run without web-framework dependencies.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from typing import Any


DATA_TYPE = "bearing_timeseries"
SAMPLE_RATE_HZ = 16000
SAMPLE_COUNT = 800
DURATION_MS = 50
ROUTES = {"edge", "cloud", "fallback_edge"}
LABELS = {"normal", "abnormal"}
RISK_LEVELS = {"low", "medium", "high"}
PROCESSING_STATUSES = {"perception_completed", "perception_rejected"}
PERCEPTION_QUALITY_STATUSES = {"good", "warning"}
EDGE_RESULTS = {"normal", "warning", "abnormal"}
QUALITY_FLAGS = {
    "LOW_CURRENT_VARIATION", "WAVEFORM_CLIPPING", "MISSING_SAMPLES",
    "NONFINITE_SAMPLE", "DC_OFFSET_PRESENT", "CONTEXT_UNSTABLE",
}
PERCEPTION_ERROR_CODES = {"INVALID_SAMPLE_COUNT"}


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


def validate_cloud_review_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the cloud perception review request without accepting legacy fields."""

    from cloud_service.perception.validator import validate_cloud_review_request as validate_request

    return validate_request(payload)


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


def canonical_summary_sha256(summary: dict[str, Any]) -> str:
    """Return the documented SHA-256 for one summary, excluding its envelope."""

    serialized = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_edge_feature_summary_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete edge-summary batch for callers outside the HTTP route."""

    batch = _validate_edge_feature_summary_envelope(payload)
    seen_summary_ids: set[str] = set()
    for summary in batch["summaries"]:
        summary_id = validate_edge_feature_summary(summary, batch["edge_node_id"])["summary_id"]
        if summary_id in seen_summary_ids:
            raise ContractError("INVALID_IDENTIFIER", "summary_id must be unique within a batch")
        seen_summary_ids.add(summary_id)
    return batch


def validate_edge_feature_summary_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate only the batch fields; item failures remain independently recoverable."""

    return _validate_edge_feature_summary_envelope(payload)


def _validate_edge_feature_summary_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    batch = require_mapping(payload, "EdgeFeatureSummaryBatch")
    require_non_empty_string(require_field(batch, "batch_id"), "batch_id")
    require_non_empty_string(require_field(batch, "edge_node_id"), "edge_node_id")
    sent_at_ns = require_int(require_field(batch, "sent_at_ns"), "sent_at_ns")
    if sent_at_ns <= 0:
        raise ContractError("INVALID_TIMESTAMP", "sent_at_ns must be positive")
    item_count = require_int(require_field(batch, "item_count"), "item_count")
    summaries = require_field(batch, "summaries")
    if not isinstance(summaries, list) or not 1 <= item_count <= 20 or item_count != len(summaries):
        raise ContractError("INVALID_PACKET", "item_count must equal 1 to 20 summaries")
    return batch


def validate_edge_feature_summary(summary: dict[str, Any], batch_edge_node_id: str) -> dict[str, Any]:
    """Validate one item and raise its documented rejection code on failure."""

    item = require_mapping(summary, "EdgeFeatureSummary")
    for field in ("summary_id", "task_id", "packet_id", "sender_id", "edge_node_id"):
        try:
            require_non_empty_string(require_field(item, field), field)
        except ContractError as error:
            raise ContractError("INVALID_IDENTIFIER", error.message) from error
    sequence_number = _item_int(item, "sequence_number", "INVALID_IDENTIFIER")
    if sequence_number < 1:
        raise ContractError("INVALID_IDENTIFIER", "sequence_number must be greater than 0")
    if item["edge_node_id"] != batch_edge_node_id:
        raise ContractError("INVALID_IDENTIFIER", "summary edge_node_id must match batch edge_node_id")
    end_timestamp_ns = _item_int(item, "end_timestamp_ns", "INVALID_TIMESTAMP")
    if end_timestamp_ns <= 0:
        raise ContractError("INVALID_TIMESTAMP", "end_timestamp_ns must be positive")
    processing_status = item.get("processing_status")
    if processing_status not in PROCESSING_STATUSES:
        raise ContractError("INVALID_QUALITY", "processing_status is invalid")
    if processing_status == "perception_rejected":
        _validate_perception_rejected(item)
    else:
        _validate_perception_completed(item, end_timestamp_ns)
    return item


def _item_int(item: dict[str, Any], field: str, code: str) -> int:
    try:
        return require_int(require_field(item, field), field)
    except ContractError as error:
        raise ContractError(code, error.message) from error


def _finite_number(item: dict[str, Any], field: str, code: str) -> float:
    try:
        value = require_number(require_field(item, field), field)
    except ContractError as error:
        raise ContractError(code, error.message) from error
    if not math.isfinite(value):
        raise ContractError(code, f"{field} must be finite")
    return value


def _required_mapping(item: dict[str, Any], field: str, code: str) -> dict[str, Any]:
    try:
        return require_mapping(require_field(item, field), field)
    except ContractError as error:
        raise ContractError(code, error.message) from error


def _validate_perception_rejected(item: dict[str, Any]) -> None:
    codes = item.get("perception_error_codes")
    if not isinstance(codes, list) or not codes or any(code not in PERCEPTION_ERROR_CODES for code in codes):
        raise ContractError("INVALID_QUALITY", "perception_error_codes must contain supported string codes")


def _validate_perception_completed(item: dict[str, Any], end_timestamp_ns: int) -> None:
    generated_at_ns = _item_int(item, "summary_generated_at_ns", "INVALID_TIMESTAMP")
    if generated_at_ns < end_timestamp_ns:
        raise ContractError("INVALID_TIMESTAMP", "summary_generated_at_ns must not precede end_timestamp_ns")
    _validate_quality(_required_mapping(item, "perception_quality", "INVALID_QUALITY"))
    _validate_features(_required_mapping(item, "features", "INVALID_FEATURE_VALUE"))
    _validate_inference(_required_mapping(item, "edge_inference", "INVALID_EDGE_INFERENCE"))
    try:
        require_non_empty_string(require_field(item, "edge_model_version"), "edge_model_version")
    except ContractError as error:
        raise ContractError("INVALID_EDGE_INFERENCE", error.message) from error


def _validate_quality(quality: dict[str, Any]) -> None:
    status = quality.get("status")
    flags = quality.get("flags")
    if status not in PERCEPTION_QUALITY_STATUSES or not isinstance(flags, list) or any(flag not in QUALITY_FLAGS for flag in flags):
        raise ContractError("INVALID_QUALITY", "perception_quality is invalid")
    if (status == "good" and flags) or (status == "warning" and not flags):
        raise ContractError("INVALID_QUALITY", "perception_quality status and flags disagree")


def _validate_features(features: dict[str, Any]) -> None:
    vibration = _required_mapping(features, "vibration", "INVALID_FEATURE_VALUE")
    _validate_channel(vibration, "vibration", "mm/s", ("rms", "absolute_peak", "kurtosis", "dominant_frequency_hz", "band_power_ratio_500_2000", "spectral_entropy"))
    current_1 = _required_mapping(features, "phase_current_1", "INVALID_FEATURE_VALUE")
    current_2 = _required_mapping(features, "phase_current_2", "INVALID_FEATURE_VALUE")
    _validate_channel(current_1, "phase_current_1", "A", ("rms_a", "absolute_peak_a"))
    _validate_channel(current_2, "phase_current_2", "A", ("rms_a", "absolute_peak_a"))
    relationship = _required_mapping(features, "current_relationship", "INVALID_FEATURE_VALUE")
    _finite_number(relationship, "current_imbalance_ratio", "INVALID_FEATURE_VALUE")
    context = _required_mapping(features, "operating_context", "INVALID_OPERATING_CONTEXT")
    for field in ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n"):
        statistics = _required_mapping(context, field, "INVALID_OPERATING_CONTEXT")
        for name in ("mean", "last", "minimum", "maximum", "standard_deviation"):
            _finite_number(statistics, name, "INVALID_OPERATING_CONTEXT")
    _finite_number(context, "bearing_module_temperature_c", "INVALID_OPERATING_CONTEXT")


def _validate_channel(channel: dict[str, Any], name: str, unit: str, values: tuple[str, ...]) -> None:
    if channel.get("source_sample_rate_hz") != 64000 or channel.get("analysis_sample_rate_hz") != 16000 or channel.get("unit") != unit:
        raise ContractError("INVALID_FEATURE_METADATA", f"{name} metadata is invalid")
    for field in values:
        _finite_number(channel, field, "INVALID_FEATURE_VALUE")


def _validate_inference(inference: dict[str, Any]) -> None:
    if inference.get("edge_result") not in EDGE_RESULTS or inference.get("edge_risk_level") not in RISK_LEVELS:
        raise ContractError("INVALID_EDGE_INFERENCE", "edge inference enum is invalid")
    confidence = _finite_number(inference, "confidence", "INVALID_EDGE_INFERENCE")
    if not 0 <= confidence <= 1:
        raise ContractError("INVALID_EDGE_INFERENCE", "confidence must be between 0 and 1")

