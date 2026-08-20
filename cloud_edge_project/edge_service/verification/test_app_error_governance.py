from __future__ import annotations

import json
import logging


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
