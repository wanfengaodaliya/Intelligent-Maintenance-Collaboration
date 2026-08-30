from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.scenario_plugin import (
    EDGE_INFERENCE,
    MODEL_PROVIDER,
    EdgeModelRuntimeClient,
    EdgeInferenceRuntimeRequest,
)
from core.scenario_registry import ScenarioRegistry
from scenarios.bearing.edge_inference import BearingEdgeInferenceProvider
from scenarios.bearing.plugin import BearingScenarioPlugin


class _Client:
    cfg = SimpleNamespace(readiness_probe_interval_s=5.0)

    def build_evidence(self, payload: dict) -> dict:
        return {"payload": payload}

    def readiness(self):  # noqa: ANN201
        return SimpleNamespace(
            ok=True,
            model_version="frozen-h5-version",
            version_mismatch=False,
            detail="ready",
        )

    def infer_task(self, task, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return {"task": task, "kwargs": kwargs}

    def activate_version(self, target_version: str) -> dict[str, str]:
        return {"runtime_version": target_version}


class _ModelProvider:
    required_window_ms = 50
    pipeline_backend = "local_h5"
    deployment_status = "local_distilled_h5"
    default_model_version = "frozen-h5-version"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def build_client(self, **request):  # noqa: ANN003, ANN201
        self.requests.append(request)
        return _Client()


def _runtime_request(window_ms: int = 50) -> EdgeInferenceRuntimeRequest:
    return EdgeInferenceRuntimeRequest(
        model_root=Path("model-root"),
        bundled_model_root=Path("bundled-model-root"),
        pinned_model_version=None,
        observation_window_ms=window_ms,
        lifecycle_enabled=True,
    )


def test_plugin_resolves_edge_inference_and_model_providers() -> None:
    registry = ScenarioRegistry()
    registry.register(BearingScenarioPlugin())

    inference = registry.require_provider("bearing", EDGE_INFERENCE)
    model = registry.require_provider("bearing", MODEL_PROVIDER)

    assert inference.model_provider is model
    assert inference.metadata.backend_id == "local_h5"
    assert model.model_metadata()["observation_window_ms"] == 50


def test_provider_builds_existing_pipeline_contract_without_model_changes() -> None:
    model_provider = _ModelProvider()
    provider = BearingEdgeInferenceProvider(model_provider)  # type: ignore[arg-type]

    runtime = provider.build_runtime(_runtime_request())

    assert runtime.pipeline_backend == "local_h5"
    assert runtime.model_client.__class__ is _Client
    assert isinstance(runtime.model_client, EdgeModelRuntimeClient)
    assert runtime.evidence_builder({"packet": 1}) == {"payload": {"packet": 1}}
    assert provider.metadata.default_model_version == "frozen-h5-version"
    assert provider.metadata.feature_extractor_version == "frozen-h5-version"
    assert provider.metadata.deployment_status == "local_distilled_h5"
    assert model_provider.requests == [
        {
            "model_root": Path("model-root"),
            "bundled_model_root": Path("bundled-model-root"),
            "pinned_model_version": None,
        }
    ]


def test_provider_rejects_non_50ms_window_before_loading_model() -> None:
    model_provider = _ModelProvider()
    provider = BearingEdgeInferenceProvider(model_provider)  # type: ignore[arg-type]

    with pytest.raises(
        ValueError,
        match=r"local_h5 requires v12\.diagnosis_window_ms=50, got 100",
    ):
        provider.build_runtime(_runtime_request(window_ms=100))

    assert model_provider.requests == []


def test_legacy_v01_inference_is_delegated_without_behavior_change() -> None:
    from edge_service.model import infer_edge_v01

    payload = {
        "task_id": "task_0001",
        "scenario": "industrial",
        "source_node": "edge_1",
        "task_type": "fault_detection",
        "timestamp": "2026-06-20 10:00:00",
        "deadline_ms": 200,
        "priority": 0.8,
        "data_size_kb": 128,
        "data": {
            "device_id": "machine_01",
            "temperature": 78.5,
            "vibration": 0.63,
            "current": 13.2,
            "load": 0.76,
        },
    }

    assert BearingEdgeInferenceProvider().infer_compatible(payload) == infer_edge_v01(
        payload
    )


def test_standard_inference_is_delegated_without_behavior_change() -> None:
    from edge_service.model import infer_edge

    payload = {
        "packet_id": "packet_0001",
        "task_id": "legacy_task_0001",
        "device_id": "machine_01",
        "sensor_id": "sensor_01",
        "sequence_number": 1,
        "start_timestamp_ns": 1_000_000,
        "end_timestamp_ns": 1_050_000,
        "duration_ms": 50,
        "data": {
            "data_type": "bearing_timeseries",
            "vibration_sample_rate_hz": 16_000,
            "vibration_sample_count": 800,
            "vibration": [0.01] * 800,
            "current": 0.8,
            "temperature": 30.0,
            "speed": 1_200.0,
            "load": 0.4,
        },
    }

    expected = infer_edge(payload)
    actual = BearingEdgeInferenceProvider().infer_compatible(payload)

    for timing_field in ("edge_latency_ms",):
        expected.pop(timing_field)
        actual.pop(timing_field)
    assert actual == expected
