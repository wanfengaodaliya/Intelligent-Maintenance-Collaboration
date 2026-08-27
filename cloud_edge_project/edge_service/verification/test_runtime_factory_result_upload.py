from __future__ import annotations

from types import SimpleNamespace

from edge_runtime import factory
from edge_runtime.config import (
    EdgeRuntimeConfig,
    MaintenanceConfig,
    RawSampleCaptureConfig,
    V12RuntimeConfig,
)


def test_factory_targets_cloud_and_does_not_add_transport_identity_to_v12_body(
    monkeypatch, tmp_path
) -> None:
    """The factory must keep strict V1.2 bodies intact while targeting Cloud directly."""
    captured: dict[str, object] = {}

    class CapturingResultUploader:
        def __init__(self, database_path, post, **kwargs):
            captured["database_path"] = database_path
            captured["post"] = post
            captured["kwargs"] = kwargs

        def enqueue_bearing(self, _result) -> bool:
            return True

        def enqueue_device(self, _result, *, connection=None) -> bool:
            return True

    class FakeMqttIngress:
        def __init__(self, *_args, **_kwargs):
            self.client = object()
            self.on_packet = None

    class FakePublisher:
        def __init__(self, *_args, **_kwargs):
            pass

        def publish(self, _payload):
            return None

    class FakeDeviceResultOutbox:
        def __init__(self, *_args, **_kwargs):
            pass

        def enqueue(self, _result, *, connection=None) -> bool:
            return True

    class FakeCoordinator:
        def __init__(self, **_kwargs):
            self.receive_raw_packet = lambda _packet: None

    class FakeService:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(factory, "ResultUploader", CapturingResultUploader)
    monkeypatch.setattr(factory, "MqttIngress", FakeMqttIngress)
    monkeypatch.setattr(factory, "MqttJsonPublisher", FakePublisher)
    monkeypatch.setattr(factory, "DeviceResultOutbox", FakeDeviceResultOutbox)
    monkeypatch.setattr(factory, "EdgeRuntimeCoordinator", FakeCoordinator)
    monkeypatch.setattr(factory, "EdgeRuntimeService", FakeService)

    cloud_url = "http://cloud.test:18021"
    config = EdgeRuntimeConfig(
        v12=V12RuntimeConfig(database_path=tmp_path / "edge.db"),
        raw_sample_capture=RawSampleCaptureConfig(enabled=False),
        maintenance=MaintenanceConfig(enabled=False),
        cloud_node_urls={"cloud_01": cloud_url},
    )
    ingress = SimpleNamespace(config=SimpleNamespace(edge_node_id="edge_01"))
    pipeline = SimpleNamespace()

    factory.build_edge_runtime(
        config=config,
        ingress=ingress,
        cache=object(),
        pipeline=pipeline,
        enable_heartbeat=False,
    )

    post = captured["post"]
    assert post.__self__.base_url == cloud_url
    assert "payload_enricher" not in captured["kwargs"]
