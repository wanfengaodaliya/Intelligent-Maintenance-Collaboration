# -*- coding: utf-8 -*-
"""交付健康检查必须反映正式 H5 本地路线和真实就绪状态。"""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "health_check.py"
SPEC = importlib.util.spec_from_file_location("edge_delivery_health_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
health_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health_check)


def test_local_h5_does_not_require_external_model_service(monkeypatch) -> None:
    monkeypatch.setenv("EDGE_DIAGNOSTIC_BACKEND", "local_h5")
    names = {target["name"] for target in health_check._probe_targets()}
    assert "model_service" not in names


def test_official_backend_includes_external_model_service(monkeypatch) -> None:
    monkeypatch.setenv("EDGE_DIAGNOSTIC_BACKEND", "official")
    names = {target["name"] for target in health_check._probe_targets()}
    assert "model_service" in names


def test_edge_probe_requires_liveness_and_readiness(monkeypatch) -> None:
    responses = {
        "/health/live": (True, {"alive": True}, ""),
        "/health/ready": (False, None, "http 503"),
        "/health": (True, {"model_backend": "local_h5"}, ""),
    }

    def fake_fetch(url: str):
        for suffix, response in responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(url)

    monkeypatch.setattr(health_check, "_fetch_json", fake_fetch)
    result = health_check._probe_edge("http://edge")
    assert result["ok"] is False
    assert result["summary"] == "live=True ready=False"
