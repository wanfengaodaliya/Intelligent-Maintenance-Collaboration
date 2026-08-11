from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from cloud_service.global_analysis.problem_detector import detect_problem_candidates


def test_repeated_packet_problem_becomes_persistent():
    candidates = detect_problem_candidates(
        device_health={"status": "succeeded"},
        bearing_risk={"status": "succeeded"},
        packet_diagnosis={
            "status": "succeeded", "reviewed_packet_count": 10,
            "cloud_correction_rate": 0.2, "risk_underestimation_rate": 0.15,
            "risk_overestimation_rate": 0.05,
        },
        bearing_aggregation={"status": "insufficient_data"},
        device_arbitration={"status": "succeeded", "conflict_rate": 0.0, "arbitration_success_rate": None},
        previous_analysis=[
            {"problem_candidates": [{"problem_layer": "packet_diagnosis", "problem_type": "risk_underestimation"}]},
            {"problem_candidates": [{"problem_layer": "packet_diagnosis", "problem_type": "risk_underestimation"}]},
        ],
        config=GlobalAnalysisConfig(),
    )
    assert candidates[0]["problem_type"] == "risk_underestimation"
    assert candidates[0]["persistence"] == "persistent"
