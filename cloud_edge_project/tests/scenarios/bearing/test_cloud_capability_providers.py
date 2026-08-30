from __future__ import annotations

from pathlib import Path

from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from core.scenario_plugin import CLOUD_DIAGNOSIS, GLOBAL_ANALYSIS, MODEL_UPDATE
from core.scenario_registry import ScenarioRegistry
from scenarios.bearing.cloud.handler import BearingCloudHandler
from scenarios.bearing.cloud.model_update import provider as model_update_module
from scenarios.bearing.cloud.model_update.provider import BearingModelUpdateProvider
from scenarios.bearing.plugin import BearingScenarioPlugin


def _registry() -> ScenarioRegistry:
    registry = ScenarioRegistry()
    registry.register(BearingScenarioPlugin())
    return registry


def test_plugin_resolves_cloud_capability_providers() -> None:
    registry = _registry()

    diagnosis = registry.require_provider("bearing", CLOUD_DIAGNOSIS)
    analysis = registry.require_provider("bearing", GLOBAL_ANALYSIS)
    model_update = registry.require_provider("bearing", MODEL_UPDATE)

    assert diagnosis.scenario_id == "bearing"
    assert analysis.scenario_id == "bearing"
    assert model_update.scenario_id == "bearing"


def test_cloud_diagnosis_provider_wraps_existing_handler(tmp_path: Path) -> None:
    provider = _registry().require_provider("bearing", CLOUD_DIAGNOSIS)

    handler = provider.build_handler(tmp_path / "cloud.db")

    assert isinstance(handler, BearingCloudHandler)
    assert handler.database_path == tmp_path / "cloud.db"


def test_global_analysis_provider_preserves_existing_analyzer_contract() -> None:
    provider = _registry().require_provider("bearing", GLOBAL_ANALYSIS)
    analyzers = provider.build_analyzers()
    config = GlobalAnalysisConfig()

    assert set(analyzers) == {
        "analyze_bearing_risk",
        "analyze_cloud_bearing_review",
        "maintenance_recommendations",
    }
    assert analyzers["analyze_bearing_risk"]({}, config)["status"] == "insufficient_data"
    assert analyzers["analyze_cloud_bearing_review"]({}, config) == {
        "status": "not_available",
        "bearing_review_count": 0,
    }
    assert analyzers["maintenance_recommendations"]({}, None) == []


def test_model_update_provider_delegates_moment_activation(monkeypatch) -> None:
    settings = object()
    calls: list[tuple] = []
    monkeypatch.setattr(
        model_update_module,
        "activate_moment_candidate",
        lambda actual, artifact, version: calls.append(
            ("candidate", actual, artifact, version)
        ),
    )
    monkeypatch.setattr(
        model_update_module,
        "activate_moment_version",
        lambda actual, version: calls.append(("version", actual, version)),
    )
    provider = BearingModelUpdateProvider()

    provider.activate_candidate(settings, Path("candidate.pt"), "v2")
    provider.activate_version(settings, "v1")

    assert calls == [
        ("candidate", settings, Path("candidate.pt"), "v2"),
        ("version", settings, "v1"),
    ]
