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
        cloud_bearing_review={"status": "insufficient_data"},
        device_arbitration={"status": "succeeded", "conflict_rate": 0.0, "arbitration_success_rate": None},
        previous_analysis=[
            {"problem_candidates": [{"problem_layer": "packet_diagnosis", "problem_type": "risk_underestimation"}]},
            {"problem_candidates": [{"problem_layer": "packet_diagnosis", "problem_type": "risk_underestimation"}]},
        ],
        config=GlobalAnalysisConfig(),
    )
    assert candidates[0]["problem_type"] == "risk_underestimation"
    assert candidates[0]["persistence"] == "persistent"


def test_arbitration_model_update_uses_complete_windows_as_evidence_count():
    candidates = detect_problem_candidates(
        device_health={"status": "succeeded"},
        bearing_risk={"status": "succeeded"},
        packet_diagnosis={"status": "insufficient_data"},
        cloud_bearing_review={"status": "insufficient_data"},
        device_arbitration={
            "status": "succeeded",
            "complete_window_count": 20,
            "conflict_rate": 0.10,
            "arbitration_count": 2,
            "arbitration_success_rate": 1.0,
        },
        previous_analysis=[
            {"problem_candidates": [{
                "problem_layer": "device_arbitration",
                "problem_type": "high_conflict_rate_model",
            }]},
            {"problem_candidates": [{
                "problem_layer": "device_arbitration",
                "problem_type": "high_conflict_rate_model",
            }]},
        ],
        config=GlobalAnalysisConfig(),
    )

    update = next(
        item for item in candidates
        if item["problem_type"] == "high_conflict_rate_model"
    )
    assert update["evidence"]["sample_count"] == 20
    assert update["evidence"]["arbitration_count"] == 2
