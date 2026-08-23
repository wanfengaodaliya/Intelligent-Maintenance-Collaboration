from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_CONFIG = {
    "default_task_limit": 20,
    "min_device_task_count": 5,
    "min_packet_review_count": 10,
    "min_condition_sample_count": 5,
    "packet_correction_warning_rate": 0.15,
    "min_bearing_review_count": 5,
    "bearing_correction_warning_rate": 0.20,
    "trend_threshold": 0.30,
    "conflict_rate_target": 0.05,
    "arbitration_success_target": 0.90,
    "condition_thresholds": {
        "shaft_speed_rpm": (800.0, 1600.0),
        "load_torque_nm": (100.0, 300.0),
        "bearing_radial_load_n": (500.0, 1000.0),
        "bearing_module_temperature_c": (60.0, 80.0),
    },
}


def test_global_analysis_target_layout_exists() -> None:
    expected_paths = (
        PROJECT_ROOT / "cloud_service" / "global_analysis" / "runtime_contracts.py",
        PROJECT_ROOT
        / "scenarios"
        / "bearing"
        / "cloud"
        / "global_analysis"
        / "v12_data_source.py",
        PROJECT_ROOT
        / "scenarios"
        / "bearing"
        / "cloud"
        / "global_analysis"
        / "problem_detector.py",
        PROJECT_ROOT
        / "compatibility"
        / "bearing_v12"
        / "global_analysis_exports.py",
    )

    assert all(path.is_file() for path in expected_paths)


def test_legacy_global_analysis_exports_are_scenario_objects() -> None:
    scenario_config = importlib.import_module(
        "scenarios.bearing.cloud.global_analysis.config"
    )
    scenario_data_source = importlib.import_module(
        "scenarios.bearing.cloud.global_analysis.v12_data_source"
    )
    runtime_contracts = importlib.import_module(
        "cloud_service.global_analysis.runtime_contracts"
    )
    compatibility = importlib.import_module(
        "compatibility.bearing_v12.global_analysis_exports"
    )
    legacy_contracts = importlib.import_module(
        "cloud_service.global_analysis.contracts"
    )
    legacy_data_source = importlib.import_module(
        "cloud_service.global_analysis.v12_data_source"
    )

    assert tuple(legacy_contracts.__all__) == (
        "GlobalAnalysisConfig",
        "DEFAULT_CONFIG",
        "DEFAULT_TASK_LIMIT",
    )
    assert tuple(legacy_data_source.__all__) == ("V12GlobalAnalysisDataSource",)
    assert tuple(compatibility.__all__) == (
        "GlobalAnalysisConfig",
        "DEFAULT_CONFIG",
        "DEFAULT_TASK_LIMIT",
        "V12GlobalAnalysisDataSource",
        "build_legacy_global_analysis_runtime",
    )
    assert legacy_contracts.GlobalAnalysisConfig is scenario_config.GlobalAnalysisConfig
    assert legacy_contracts.DEFAULT_CONFIG is scenario_config.DEFAULT_CONFIG
    assert legacy_contracts.DEFAULT_TASK_LIMIT is runtime_contracts.DEFAULT_TASK_LIMIT
    assert compatibility.GlobalAnalysisConfig is scenario_config.GlobalAnalysisConfig
    assert compatibility.DEFAULT_CONFIG is scenario_config.DEFAULT_CONFIG
    assert compatibility.DEFAULT_TASK_LIMIT is runtime_contracts.DEFAULT_TASK_LIMIT
    assert legacy_data_source.V12GlobalAnalysisDataSource is (
        scenario_data_source.V12GlobalAnalysisDataSource
    )
    assert compatibility.V12GlobalAnalysisDataSource is (
        scenario_data_source.V12GlobalAnalysisDataSource
    )


def test_legacy_global_analysis_config_matches_frozen_golden() -> None:
    from cloud_service.global_analysis.contracts import (
        DEFAULT_CONFIG,
        DEFAULT_TASK_LIMIT,
        GlobalAnalysisConfig,
    )

    assert asdict(GlobalAnalysisConfig()) == EXPECTED_CONFIG
    assert asdict(DEFAULT_CONFIG) == EXPECTED_CONFIG
    assert DEFAULT_TASK_LIMIT == 20


def _bearing_data() -> dict[str, object]:
    return {
        "bearing_tasks": [
            {
                "bearing_id": "bearing_a",
                "bearing_state": "fault",
                "completed_at_ns": 1,
            },
            {
                "bearing_id": "bearing_a",
                "bearing_state": "warning",
                "completed_at_ns": 2,
            },
            {
                "bearing_id": "bearing_b",
                "bearing_state": "normal",
                "completed_at_ns": 3,
            },
        ],
        "bearing_review_pairs": [
            {
                "edge_state": "normal",
                "cloud_state": "fault",
                "aggregation_version": "agg_v1",
            }
        ],
    }


def test_legacy_provider_analyzers_match_frozen_goldens() -> None:
    from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
    from scenarios.bearing.cloud.global_analysis.provider import (
        BearingGlobalAnalysisProvider,
    )

    analyzers = BearingGlobalAnalysisProvider().build_analyzers()
    config = GlobalAnalysisConfig()
    data = _bearing_data()
    risk = analyzers["analyze_bearing_risk"](data, config)
    review = analyzers["analyze_cloud_bearing_review"](data, config)

    assert tuple(analyzers) == (
        "analyze_bearing_risk",
        "analyze_cloud_bearing_review",
        "maintenance_recommendations",
    )
    assert risk["primary_risk_bearing_id"] == "bearing_a"
    assert risk["degrading_bearing_count"] == 0
    assert risk["multi_bearing_degradation"] is False
    assert [item["bearing_id"] for item in risk["bearings"]] == [
        "bearing_a",
        "bearing_b",
    ]
    assert risk["bearings"][0]["abnormal_rate"] == 0.5
    assert review["bearing_review_count"] == 1
    assert review["bearing_correction_rate"] == 1.0
    assert review["bearing_underestimation_rate"] == 1.0
    assert analyzers["maintenance_recommendations"](
        {"trend": "degrading"}, risk
    ) == [
        "设备近期健康状态呈恶化趋势，建议重点关注。",
        "bearing_a 异常任务占比较高，建议优先检查该轴承。",
    ]


def test_problem_candidate_order_matches_frozen_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cloud_service.global_analysis.contracts import GlobalAnalysisConfig

    detector = importlib.import_module(
        "cloud_service.global_analysis.problem_detector"
    )
    monkeypatch.setattr(
        detector,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed"),
    )

    candidates = detector.detect_problem_candidates(
        device_health={"status": "succeeded"},
        bearing_risk={"status": "succeeded"},
        packet_diagnosis={
            "status": "succeeded",
            "reviewed_packet_count": 10,
            "cloud_correction_rate": 0.20,
            "risk_underestimation_rate": 0.12,
            "risk_overestimation_rate": 0.08,
        },
        cloud_bearing_review={
            "status": "succeeded",
            "bearing_review_count": 5,
            "bearing_correction_rate": 0.30,
        },
        device_arbitration={
            "conflict_rate": 0.06,
            "arbitration_count": 10,
            "arbitration_success_rate": 0.80,
        },
        previous_analysis=[],
        config=GlobalAnalysisConfig(),
    )

    assert [
        (
            item["problem_layer"],
            item["problem_type"],
            item["severity"],
            item["suggested_action"],
        )
        for item in candidates
    ] == [
        ("packet_diagnosis", "risk_underestimation", "high", "model_update"),
        (
            "cloud_bearing_review",
            "high_correction_rate",
            "medium",
            "cloud_review_policy_review",
        ),
        (
            "device_arbitration",
            "high_conflict_rate",
            "medium",
            "arbitration_rule_review",
        ),
        (
            "device_arbitration",
            "high_conflict_rate_model",
            "medium",
            "model_update",
        ),
        (
            "device_arbitration",
            "low_arbitration_success_rate",
            "high",
            "arbitration_rule_review",
        ),
    ]
    assert [item["problem_id"] for item in candidates] == [
        "problem_fixed"
    ] * 5


def test_bearing_provider_builds_complete_global_analysis_runtime(
    tmp_path: Path,
) -> None:
    from scenarios.bearing.cloud.global_analysis.provider import (
        BearingGlobalAnalysisProvider,
    )

    runtime = BearingGlobalAnalysisProvider().build_runtime(tmp_path / "cloud.db")

    assert runtime.data_source.__class__.__module__ == (
        "scenarios.bearing.cloud.global_analysis.v12_data_source"
    )
    assert runtime.data_source.database_path == tmp_path / "cloud.db"
    assert asdict(runtime.config) == EXPECTED_CONFIG
    assert callable(runtime.analyze_scenario)
    assert callable(runtime.detect_scenario_candidates)


def test_provider_runtime_scenario_analysis_matches_legacy_analyzers(
    tmp_path: Path,
) -> None:
    from scenarios.bearing.cloud.global_analysis.provider import (
        BearingGlobalAnalysisProvider,
    )

    provider = BearingGlobalAnalysisProvider()
    runtime = provider.build_runtime(tmp_path / "cloud.db")
    data = _bearing_data()
    common_results = {"device_health_analysis": {"trend": "degrading"}}

    scenario_results = runtime.analyze_scenario(
        data,
        common_results,
        runtime.config,
    )

    assert tuple(scenario_results) == (
        "bearing_risk_analysis",
        "cloud_bearing_review_analysis",
        "maintenance_recommendations",
    )
    assert scenario_results["bearing_risk_analysis"][
        "primary_risk_bearing_id"
    ] == "bearing_a"
    assert scenario_results["cloud_bearing_review_analysis"][
        "reviewed_bearing_count"
    ] == 1
    assert scenario_results["maintenance_recommendations"] == [
        "设备近期健康状态呈恶化趋势，建议重点关注。",
        "bearing_a 异常任务占比较高，建议优先检查该轴承。",
    ]


def test_formal_global_analysis_paths_use_provider_runtime() -> None:
    app = importlib.import_module("cloud_service.app")

    endpoint_source = inspect.getsource(app.global_analysis)
    periodic_source = inspect.getsource(app._run_periodic_global_analysis_once)

    assert ".build_runtime(" in endpoint_source
    assert "runtime=" in endpoint_source
    assert ".build_runtime(" in periodic_source
    assert "runtime_factory=" in periodic_source


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(PROJECT_ROOT),))
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "entry_import",
    (
        "cloud_service.global_analysis.contracts",
        "cloud_service.global_analysis.v12_data_source",
        "scenarios.bearing.cloud.global_analysis.config",
        "scenarios.bearing.cloud.global_analysis.v12_data_source",
        "compatibility.bearing_v12.global_analysis_exports",
    ),
)
def test_global_analysis_modules_support_cold_import_orders(
    entry_import: str,
) -> None:
    code = f"""
import importlib

importlib.import_module({entry_import!r})
legacy_contracts = importlib.import_module(
    "cloud_service.global_analysis.contracts"
)
legacy_data_source = importlib.import_module(
    "cloud_service.global_analysis.v12_data_source"
)
scenario_config = importlib.import_module(
    "scenarios.bearing.cloud.global_analysis.config"
)
scenario_data_source = importlib.import_module(
    "scenarios.bearing.cloud.global_analysis.v12_data_source"
)
assert legacy_contracts.GlobalAnalysisConfig is scenario_config.GlobalAnalysisConfig
assert legacy_contracts.DEFAULT_CONFIG is scenario_config.DEFAULT_CONFIG
assert legacy_data_source.V12GlobalAnalysisDataSource is scenario_data_source.V12GlobalAnalysisDataSource
"""

    completed = _run_isolated(code)

    assert completed.returncode == 0, completed.stderr
