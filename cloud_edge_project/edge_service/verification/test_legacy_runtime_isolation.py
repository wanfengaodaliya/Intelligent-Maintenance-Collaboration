from pathlib import Path

import edge_runtime.factory as runtime_factory
from edge_runtime.config import (
    EdgeRuntimeConfig,
    RawSampleCaptureConfig,
    V12RuntimeConfig,
    WindowTransferConfig,
)


class _Ingress:
    config = type("IngressConfig", (), {"edge_node_id": "edge_01"})()


class _Pipeline:
    on_packet_completed = None


class _MqttIngress:
    def __init__(self, config, on_packet):
        self.config = config
        self.on_packet = on_packet
        self.client = object()


def _config(tmp_path: Path, *, legacy_enabled: bool) -> EdgeRuntimeConfig:
    return EdgeRuntimeConfig(
        window_transfer=WindowTransferConfig(
            cache_directory=tmp_path / "legacy_windows",
            hard_limit_bytes=1024,
            warning_bytes=512,
            reserved_free_bytes=0,
        ),
        v12=V12RuntimeConfig(
            enabled=False,
            legacy_realtime_aggregation=legacy_enabled,
        ),
        raw_sample_capture=RawSampleCaptureConfig(enabled=False),
        cloud_node_urls={"cloud_01": "http://127.0.0.1:8004"},
    )


def test_default_runtime_does_not_construct_legacy_aggregation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime_factory, "MqttIngress", _MqttIngress)

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("legacy realtime aggregation was constructed")

    monkeypatch.setattr(runtime_factory, "WindowReviewStore", fail_if_constructed)
    monkeypatch.setattr(runtime_factory, "WindowReviewDispatcher", fail_if_constructed)
    monkeypatch.setattr(runtime_factory, "BearingAggregationWorkflow", fail_if_constructed)

    assembly = runtime_factory.build_edge_runtime(
        config=_config(tmp_path, legacy_enabled=False),
        ingress=_Ingress(),
        cache=object(),
        pipeline=_Pipeline(),
        enable_heartbeat=False,
    )

    assert assembly.window_review_store is None
    assert assembly.service.window_dispatcher is None
    assert assembly.coordinator.aggregation_workflow is None


def test_explicit_legacy_flag_keeps_aggregation_available(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime_factory, "MqttIngress", _MqttIngress)

    assembly = runtime_factory.build_edge_runtime(
        config=_config(tmp_path, legacy_enabled=True),
        ingress=_Ingress(),
        cache=object(),
        pipeline=_Pipeline(),
        enable_heartbeat=False,
    )

    assert assembly.window_review_store is not None
    assert assembly.service.window_dispatcher is not None
    assert assembly.coordinator.aggregation_workflow is not None


def test_legacy_and_v12_realtime_chains_cannot_be_enabled_together() -> None:
    config = EdgeRuntimeConfig(
        v12=V12RuntimeConfig(
            enabled=True,
            legacy_realtime_aggregation=True,
        )
    )

    assert "v12 and legacy realtime aggregation cannot both be enabled" in config.validate()
