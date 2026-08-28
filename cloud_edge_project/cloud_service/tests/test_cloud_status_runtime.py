from __future__ import annotations

from pathlib import Path

import cloud_service.app as app_module
from cloud_service.config import CloudSettings
from cloud_service.moment_light_adapt import MomentLightAdaptRunner
from cloud_service.runtime_status import CloudRuntimeState


class _Response:
    def raise_for_status(self) -> None:
        return None


class _LoadedRunner:
    loaded = True
    model_version = "moment_candidate_v2"
    gpu_available = False


def _settings(tmp_path: Path) -> CloudSettings:
    return CloudSettings(
        backend="moment_light_adapt",
        vllm_url="",
        vllm_model_name="",
        vllm_api_key="",
        vllm_timeout_seconds=1,
        database_path=tmp_path / "cloud.db",
        moment_checkpoint_path=tmp_path / "checkpoint.pt",
        moment_condition_norm_path=tmp_path / "condition_norm.json",
        moment_pretrained_path=tmp_path / "pretrained",
        moment_deployment_dir=tmp_path / "deployment",
    )


def test_runtime_state_reports_inflight_work_and_latest_activity() -> None:
    timestamps = iter((101, 202))
    state = CloudRuntimeState(clock_ns=lambda: next(timestamps))

    state.begin_inference()
    during = state.snapshot()
    state.finish_inference()
    after = state.snapshot()

    assert during.queue_length == 1
    assert during.last_task_activity_ns == 101
    assert after.queue_length == 0
    assert after.last_task_activity_ns == 202


def test_moment_runner_reports_actual_cuda_availability(tmp_path: Path) -> None:
    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class _Torch:
        cuda = _Cuda()

    runner = MomentLightAdaptRunner(_settings(tmp_path))
    runner._torch = _Torch()

    assert runner.gpu_available is True


def test_application_status_reporter_emits_runtime_model_gpu_queue_and_activity(
    tmp_path: Path, monkeypatch
) -> None:
    state = CloudRuntimeState(clock_ns=lambda: 123)
    state.begin_inference()
    settings = _settings(tmp_path)
    captured: list[dict] = []
    monkeypatch.setattr(app_module, "cloud_runtime_state", state, raising=False)
    monkeypatch.setattr(app_module, "load_cloud_settings", lambda: settings)
    monkeypatch.setattr(app_module, "get_moment_runner", lambda _: _LoadedRunner())

    reporter = app_module.build_cloud_status_reporter()
    reporter.http_post = lambda _url, *, json, timeout: (
        captured.append(json) or _Response()
    )

    assert reporter.report_once() is True
    assert captured[0]["resources"]["gpu_available"] is False
    assert captured[0]["resources"]["queue_length"] == 1
    assert captured[0]["models"] == [
        {"model_version": "moment_candidate_v2", "model_load_status": "LOADED"}
    ]
    assert captured[0]["last_task_activity_ns"] == 123


def test_health_payload_identifies_reference_model(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(app_module, "get_moment_runner", lambda _: _LoadedRunner())
    payload = app_module._health_payload(settings, "ok")
    assert payload["model_version"] == "moment_candidate_v2"
    assert payload["model_device"] == "auto"


def test_cloud_infer_tracks_inflight_work_until_handler_returns(
    tmp_path: Path, monkeypatch
) -> None:
    state = CloudRuntimeState(clock_ns=lambda: 456)
    monkeypatch.setattr(app_module, "cloud_runtime_state", state, raising=False)
    monkeypatch.setattr(app_module, "load_cloud_settings", lambda: _settings(tmp_path))

    class _Handler:
        def infer(self, request: dict) -> dict:
            snapshot = state.snapshot()
            assert snapshot.queue_length == 1
            assert snapshot.last_task_activity_ns == 456
            return {"success": True, "request": request}

    monkeypatch.setattr(app_module, "get_scenario_handler", lambda *_args, **_kwargs: _Handler())

    assert app_module.cloud_infer({"scenario_type": "bearing"})["success"] is True
    assert state.snapshot().queue_length == 0
