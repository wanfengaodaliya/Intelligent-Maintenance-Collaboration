from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient


def test_unexpected_inference_error_is_logged_but_not_returned(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv(
        "EDGE_CONTROL_SHARED_SECRET", "test-control-secret-32-bytes-long"
    )
    from edge_service import app as application

    def fail_inference(_payload):
        raise RuntimeError("private model failure detail")

    monkeypatch.setattr(application, "infer_edge", fail_inference)
    with caplog.at_level(logging.ERROR, logger=application.__name__):
        response = application.edge_infer({"packet_id": "packet-1"})

    body = json.loads(response.body)
    assert response.status_code == 500
    assert body["error_code"] == "MODEL_INFER_FAILED"
    assert len(body["error_id"]) == 32
    assert body["message"] == "edge inference failed"
    assert "private model failure detail" not in response.body.decode("utf-8")
    assert "private model failure detail" in caplog.text


def test_edge_app_installs_control_and_global_body_limits(monkeypatch) -> None:
    monkeypatch.setenv(
        "EDGE_CONTROL_SHARED_SECRET", "test-control-secret-32-bytes-long"
    )
    from edge_service import app as application

    client = TestClient(application.app)
    control = client.post("/edge/tasks", content=b"x" * (64 * 1024 + 1))
    data = client.post("/edge/packets", content=b"x" * (1024 * 1024 + 1))

    assert control.status_code == 413
    assert control.json()["error_code"] == "REQUEST_BODY_TOO_LARGE"
    assert data.status_code == 413
    assert data.json()["error_code"] == "REQUEST_BODY_TOO_LARGE"
