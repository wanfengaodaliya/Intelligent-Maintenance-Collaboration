from common import schemas
from compatibility.bearing_v12 import legacy_exports
from core.diagnosis_contracts import EdgeBearingResult
from scenarios.bearing.cloud.context import signal_contracts


def test_signal_contracts_keep_callable_identity_through_legacy_paths() -> None:
    assert schemas.validate_sensor_packet is signal_contracts.validate_sensor_packet
    assert legacy_exports.validate_sensor_packet is signal_contracts.validate_sensor_packet
    assert schemas.validate_edge_feature_summary is signal_contracts.validate_edge_feature_summary


def test_signal_constants_keep_legacy_values() -> None:
    assert schemas.DATA_TYPE == legacy_exports.DATA_TYPE == "bearing_timeseries"
    assert schemas.SAMPLE_RATE_HZ == legacy_exports.SAMPLE_RATE_HZ == 16000
    assert schemas.SAMPLE_COUNT == legacy_exports.SAMPLE_COUNT == 800


def test_diagnosis_types_are_reexported_without_redefinition() -> None:
    assert legacy_exports.EdgeBearingResult is EdgeBearingResult
