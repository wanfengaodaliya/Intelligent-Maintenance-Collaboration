from types import SimpleNamespace

import cloud_service.app as app_module


def _analysis_result() -> dict:
    return {
        "analysis_id": "ga_auto_001",
        "scenario_type": "bearing",
        "subject_id": "machine_01",
        "problem_candidates": [
            {
                "problem_id": "problem_update_001",
                "suggested_action": "model_update",
            },
            {
                "problem_id": "problem_observe_001",
                "suggested_action": "arbitration_rule_review",
            },
        ],
    }


class _AnalysisService:
    def __init__(self, *_args, **_kwargs):
        pass

    def analyze(self, *_args, **_kwargs):
        return _analysis_result()


class _UpdateService:
    def __init__(self):
        self.requests = []

    def create(
        self, request, *, reuse_active=False, use_llm_suggestion=True
    ):
        self.requests.append((request, reuse_active, use_llm_suggestion))
        return {"decision": "create_update", "update": {"update_id": "update_001"}}


def test_manual_global_analysis_creates_only_suggested_model_update(monkeypatch):
    updates = _UpdateService()
    monkeypatch.setattr(app_module, "GlobalAnalysisService", _AnalysisService)
    monkeypatch.setattr(
        app_module,
        "load_cloud_settings",
        lambda: SimpleNamespace(database_path="unused.db"),
    )
    monkeypatch.setattr(app_module, "_model_update_service", lambda: updates)

    response = app_module.global_analysis({
        "scenario_type": "bearing",
        "subject_id": "machine_01",
    })

    assert response["success"] is True
    assert updates.requests == [({
        "analysis_id": "ga_auto_001",
        "problem_id": "problem_update_001",
    }, True, False)]


def test_periodic_global_analysis_passes_auto_create_callback(monkeypatch):
    captured = {}
    runtime = object()
    provider = SimpleNamespace(
        scenario_id="bearing",
        build_runtime=lambda database_path: (runtime, database_path),
    )

    def fake_run_all(*_args, **kwargs):
        captured.update(kwargs)
        return ["machine_01"]

    monkeypatch.setattr(app_module, "run_periodic_global_analysis", fake_run_all)
    monkeypatch.setattr(app_module, "_scenario_provider", lambda *_args: provider)

    assert app_module._run_periodic_global_analysis_once("cloud.db") == ["machine_01"]
    assert captured["scenario_type"] == "bearing"
    assert captured["runtime_factory"]("runtime.db") == (runtime, "runtime.db")
    assert captured.get("on_result") is not None
