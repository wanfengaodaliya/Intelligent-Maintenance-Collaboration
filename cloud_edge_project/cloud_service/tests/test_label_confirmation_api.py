from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import cloud_service.app as app_module
from cloud_service.config import load_cloud_settings
from cloud_service.storage.database import initialize_database


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    settings = replace(
        load_cloud_settings(), backend="mock", database_path=database_path
    )
    monkeypatch.setattr(app_module, "load_cloud_settings", lambda: settings)
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as test_client:
        yield test_client


def test_human_label_confirmation_roundtrip(client) -> None:
    response = client.post(
        "/cloud/model-update/label-confirmations/packet_api_1",
        json={
            "confirmed_label": "inner_ring_damage",
            "confirmed_risk_level": "fault",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["packet_id"] == "packet_api_1"
    assert body["confirmed_label"] == "inner_ring_damage"
    assert body["label_source"] == "human_confirmed"
    assert body["confirmed_risk_level"] == "fault"

    fetched = client.get(
        "/cloud/model-update/label-confirmations/packet_api_1"
    )
    assert fetched.status_code == 200
    assert fetched.json()["confirmed_label"] == "inner_ring_damage"


def test_human_label_requires_valid_label(client) -> None:
    response = client.post(
        "/cloud/model-update/label-confirmations/packet_api_bad",
        json={"confirmed_label": "   "},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_HUMAN_LABEL"


def test_human_label_rejects_unordered_risk_level(client) -> None:
    response = client.post(
        "/cloud/model-update/label-confirmations/packet_api_risk",
        json={"confirmed_label": "healthy", "confirmed_risk_level": "loud"},
    )
    assert response.status_code == 200
    assert response.json()["confirmed_risk_level"] is None


def test_missing_label_confirmation_returns_404(client) -> None:
    response = client.get(
        "/cloud/model-update/label-confirmations/packet_api_missing"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "LABEL_CONFIRMATION_NOT_FOUND"