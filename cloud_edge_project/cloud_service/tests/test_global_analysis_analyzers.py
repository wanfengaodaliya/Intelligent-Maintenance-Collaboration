from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from cloud_service.global_analysis.device_health_analyzer import analyze_device_health
from cloud_service.global_analysis.packet_model_analyzer import analyze_packet_model
from scenarios.bearing.cloud.global_analysis.bearing_aggregation_analyzer import (
    analyze_bearing_aggregation,
)
from scenarios.bearing.cloud.global_analysis.bearing_risk_analyzer import analyze_bearing_risk


def test_device_health_reports_counts_recent_risk_and_trend():
    rows = [
        {"final_state": state, "completed_at_ns": index}
        for index, state in enumerate(["normal", "normal", "warning", "abnormal", "abnormal"])
    ]
    result = analyze_device_health(rows, GlobalAnalysisConfig())
    assert result["status"] == "succeeded"
    assert result["risk_task_count"] == 3
    assert result["recent_risk_rate"] == 0.6
    assert result["consecutive_abnormal_tasks"] == 2
    assert result["trend"] == "degrading"


def test_device_health_uses_null_rates_when_there_are_no_valid_rows():
    result = analyze_device_health([], GlobalAnalysisConfig())
    assert result["status"] == "insufficient_data"
    assert result["normal_rate"] is None
    assert result["recent_risk_rate"] is None


def test_bearing_risk_is_dynamic_and_detects_multiple_degrading_bearings():
    states = ["normal", "normal", "warning", "abnormal", "abnormal"]
    rows = [
        {"bearing_id": bearing, "bearing_state": state, "completed_at_ns": index}
        for bearing in ("bearing_01", "bearing_02")
        for index, state in enumerate(states)
    ]
    result = analyze_bearing_risk(rows, GlobalAnalysisConfig())
    assert result["status"] == "succeeded"
    assert result["multi_bearing_degradation"] is True
    assert result["primary_risk_bearing_id"] == "bearing_01"


def test_bearing_risk_is_insufficient_until_one_bearing_has_enough_history():
    result = analyze_bearing_risk(
        [{"bearing_id": "bearing_01", "bearing_state": "warning", "completed_at_ns": 1}],
        GlobalAnalysisConfig(),
    )
    assert result["status"] == "insufficient_data"


def test_packet_model_reports_direction_version_and_condition_weakness():
    rows = [
        {
            "edge_label": "normal", "cloud_label": "abnormal", "edge_model_version": "v1",
            "operating_context": {"load_torque_nm": 500},
        },
        {
            "edge_label": "abnormal", "cloud_label": "abnormal", "edge_model_version": "v1",
            "operating_context": {"load_torque_nm": 500},
        },
        {
            "edge_label": "warning", "cloud_label": "normal", "edge_model_version": "v2",
            "operating_context": {"load_torque_nm": 500},
        },
        {
            "edge_label": "normal", "cloud_label": "normal", "edge_model_version": "v2",
            "operating_context": {"load_torque_nm": 20},
        },
    ]
    config = GlobalAnalysisConfig(min_packet_review_count=1, min_condition_sample_count=1)
    result = analyze_packet_model(rows, config)
    assert result["status"] == "succeeded"
    assert result["risk_underestimation_count"] == 1
    assert result["risk_overestimation_count"] == 1
    assert result["by_model_version"][0]["model_version"] == "v1"
    assert result["condition_weakness"]["condition"] == "load_torque_nm"
    assert result["condition_weakness"]["bucket"] == "high"


def test_packet_model_marks_absent_upstream_history_not_available():
    result = analyze_packet_model([], GlobalAnalysisConfig(), available=False)
    assert result["status"] == "not_available"
    assert result["cloud_correction_rate"] is None


def test_bearing_aggregation_reports_not_available_trigger_analysis():
    rows = [{"edge_state": "normal", "cloud_state": "abnormal", "aggregation_version": "v1"}]
    result = analyze_bearing_aggregation(rows, GlobalAnalysisConfig(min_bearing_review_count=1))
    assert result["status"] == "succeeded"
    assert result["bearing_underestimation_count"] == 1
    assert result["review_trigger_analysis"]["status"] == "not_available"


def test_arbitration_does_not_fabricate_success_rate():
    device_rows = [{"has_conflict": index == 0} for index in range(20)]
    result = analyze_device_arbitration(device_rows, [], GlobalAnalysisConfig())
    assert result["conflict_rate"] == 0.05
    assert result["arbitration_success_rate"] is None
    assert result["conflict_target_met"] is True
