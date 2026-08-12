from __future__ import annotations

import pytest

from cloud_service.raw_context.transport import RoutedRawContextTransport


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"status": "accepted"}


def test_routed_transport_uses_node_specific_url(monkeypatch) -> None:
    captured: list[str] = []

    def fake_post(url, **kwargs):
        del kwargs
        captured.append(url)
        return _Response()

    monkeypatch.setattr(
        "cloud_service.raw_context.transport.requests.post",
        fake_post,
    )
    transport = RoutedRawContextTransport(
        "http://127.0.0.1:8001",
        {"edge_02": "http://192.168.56.22:8001"},
    )

    transport.send({"edge_node_id": "edge_02"})

    assert captured == [
        "http://192.168.56.22:8001/edge/raw-context-requests"
    ]


def test_routed_transport_keeps_existing_default(monkeypatch) -> None:
    captured: list[str] = []

    def fake_post(url, **kwargs):
        del kwargs
        captured.append(url)
        return _Response()

    monkeypatch.setattr(
        "cloud_service.raw_context.transport.requests.post",
        fake_post,
    )
    transport = RoutedRawContextTransport(
        "http://127.0.0.1:8001",
        {"edge_02": "http://192.168.56.22:8001"},
    )

    transport.send({})

    assert captured == ["http://127.0.0.1:8001/edge/raw-context-requests"]


def test_routed_transport_rejects_unknown_node_when_mapping_is_configured() -> None:
    transport = RoutedRawContextTransport(
        "http://127.0.0.1:8001",
        {"edge_02": "http://192.168.56.22:8001"},
    )

    with pytest.raises(ValueError, match="unknown edge_node_id"):
        transport.send({"edge_node_id": "edge_03"})
