"""Stable import boundary for public bearing V1.2 compatibility exports.

Signal exports are resolved lazily because their implementation depends on the
generic validation primitives in :mod:`common.schemas`.
"""

from importlib import import_module

from compatibility.bearing_v12.diagnosis_contracts import (
    ActionGrade,
    BearingDecisionResult,
    BearingLifecycleStatus,
    CloudBearingResult,
    DeviceDecisionResult,
    DeviceDecisionStatus,
    EdgeBearingResult,
    PacketRoute,
    RoundClosureReason,
    RoundState,
)
from scenarios.bearing.cloud.model_update.model_catalog import BEARING_MODEL_CATALOG


_SIGNAL_EXPORTS = {
    "DATA_TYPE",
    "DURATION_MS",
    "EDGE_RESULTS",
    "PERCEPTION_ERROR_CODES",
    "PERCEPTION_QUALITY_STATUSES",
    "PROCESSING_STATUSES",
    "QUALITY_FLAGS",
    "SAMPLE_COUNT",
    "SAMPLE_RATE_HZ",
    "compact_packet_for_scheduler",
    "validate_cloud_request",
    "validate_edge_feature_summary",
    "validate_edge_feature_summary_batch",
    "validate_edge_feature_summary_envelope",
    "validate_schedule_request",
    "validate_sensor_packet",
    "validate_task_trace",
}


def __getattr__(name: str) -> object:
    if name not in _SIGNAL_EXPORTS:
        raise AttributeError(name)
    module = import_module("scenarios.bearing.cloud.context.signal_contracts")
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "ActionGrade",
    "BEARING_MODEL_CATALOG",
    "BearingDecisionResult",
    "BearingLifecycleStatus",
    "CloudBearingResult",
    "DeviceDecisionResult",
    "DeviceDecisionStatus",
    "EdgeBearingResult",
    "PacketRoute",
    "RoundClosureReason",
    "RoundState",
    *_SIGNAL_EXPORTS,
]
