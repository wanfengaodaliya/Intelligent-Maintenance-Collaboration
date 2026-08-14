import importlib
import json
from pathlib import Path

from cloud_service.config import CloudSettings, load_cloud_settings


cloud_app = importlib.import_module("cloud_service.app")


def _settings(tmp_path: Path, *, legacy_enabled: bool) -> CloudSettings:
    return CloudSettings(
        backend="mock",
        vllm_url="",
        vllm_model_name="",
        vllm_api_key="",
        vllm_timeout_seconds=1.0,
        database_path=tmp_path / "cloud.db",
        legacy_bearing_window_review_enabled=legacy_enabled,
        legacy_context_enhanced_pipeline_enabled=False,
    )


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_legacy_cloud_workflows_are_disabled_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cloud_app,
        "load_cloud_settings",
        lambda: _settings(tmp_path, legacy_enabled=False),
    )

    response = cloud_app.bearing_task_results({})

    assert response.status_code == 410
    assert _body(response) == {"error_code": "LEGACY_BEARING_WORKFLOW_DISABLED"}

    context_response = cloud_app.raw_context_batches({})
    assert context_response.status_code == 410
    assert _body(context_response) == {"error_code": "LEGACY_CONTEXT_PIPELINE_DISABLED"}


def test_explicit_cloud_legacy_flag_allows_legacy_result_ingestion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cloud_app,
        "load_cloud_settings",
        lambda: _settings(tmp_path, legacy_enabled=True),
    )
    payload = {
        "device_id": "device_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "edge_state": "warning",
        "edge_confidence": 0.6,
        "packet_count": 20,
        "source_packet_manifest": [{"packet_id": "packet_01", "sequence_number": 1}],
        "bearing_state": "warning",
        "result_source": "edge",
    }

    assert cloud_app.bearing_task_results(payload)["status"] == "accepted"


def test_cloud_legacy_flag_is_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("CLOUD_LEGACY_BEARING_WINDOW_REVIEW_ENABLED", raising=False)
    monkeypatch.delenv("CLOUD_LEGACY_CONTEXT_ENHANCED_PIPELINE_ENABLED", raising=False)
    assert load_cloud_settings().legacy_bearing_window_review_enabled is False
    assert load_cloud_settings().legacy_context_enhanced_pipeline_enabled is False

    monkeypatch.setenv("CLOUD_LEGACY_BEARING_WINDOW_REVIEW_ENABLED", "true")
    monkeypatch.setenv("CLOUD_LEGACY_CONTEXT_ENHANCED_PIPELINE_ENABLED", "true")
    assert load_cloud_settings().legacy_bearing_window_review_enabled is True
    assert load_cloud_settings().legacy_context_enhanced_pipeline_enabled is True
