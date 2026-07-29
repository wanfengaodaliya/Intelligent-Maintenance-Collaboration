"""FastAPI entry point for the cloud inference service."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.model import CLOUD_NODE_ID, infer_cloud
from cloud_service.storage.database import initialize_database
from cloud_service.storage.edge_feature_repository import EdgeFeatureRepository
from common.config import load_config
from common.schemas import (
    ContractError,
    error_response,
    validate_edge_feature_summary,
    validate_edge_feature_summary_envelope,
)


config = load_config()
app = FastAPI(title="cloud_service")


def _edge_summary_repository() -> EdgeFeatureRepository:
    database_path = Path(
        os.getenv("CLOUD_SUMMARY_DATABASE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "cloud_summary.db"))
    )
    initialize_database(database_path)
    return EdgeFeatureRepository(database_path)


def _models_url(settings: CloudSettings) -> str:
    base_url = settings.vllm_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url[: -len("/chat/completions")] + "/models"
    return base_url + "/models"


def _health_payload(settings: CloudSettings, status: str) -> dict[str, object]:
    return {
        "service": "cloud_service",
        "node_id": CLOUD_NODE_ID,
        "status": status,
        "port": int(
            os.getenv(
                "CLOUD_SERVICE_PORT",
                str(config["services"]["cloud"]["port"]),
            )
        ),
        "model_backend": settings.backend,
    }


@app.get("/health", response_model=None)
def health() -> dict[str, object] | JSONResponse:
    settings = load_cloud_settings()
    if settings.backend == "mock":
        return _health_payload(settings, "ok")
    if settings.backend != "vllm":
        return JSONResponse(
            status_code=500,
            content=_health_payload(settings, "unavailable"),
        )

    headers: dict[str, str] = {}
    if settings.vllm_api_key:
        headers["Authorization"] = f"Bearer {settings.vllm_api_key}"
    try:
        response = requests.get(
            _models_url(settings),
            headers=headers,
            timeout=min(settings.vllm_timeout_seconds, 3.0),
        )
        response.raise_for_status()
    except requests.RequestException:
        return JSONResponse(
            status_code=503,
            content=_health_payload(settings, "unavailable"),
        )
    return _health_payload(settings, "ok")


@app.post("/cloud/infer", response_model=None)
def cloud_infer(payload: dict) -> dict | JSONResponse:
    try:
        return infer_cloud(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except CloudServiceError as error:
        packet = payload.get("cloud_raw_packet", {}) if isinstance(payload.get("cloud_raw_packet"), dict) else {}
        contract_error = ContractError(
            error.code,
            error.message,
            packet.get("packet_id"),
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_response(contract_error),
        )
    except Exception as exc:
        packet = payload.get("cloud_raw_packet", {}) if isinstance(payload.get("cloud_raw_packet"), dict) else {}
        error = ContractError("MODEL_INFER_FAILED", str(exc), packet.get("packet_id"))
        return JSONResponse(status_code=500, content=error_response(error))


@app.post("/cloud/edge-feature-summaries", response_model=None)
def edge_feature_summaries(payload: Any) -> dict[str, object] | JSONResponse:
    """Receive a batch of edge summaries and acknowledge each item independently."""

    try:
        batch = validate_edge_feature_summary_envelope(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content={"error_code": error.code})

    repository = _edge_summary_repository()
    results: list[dict[str, str]] = []
    summary_ids: set[str] = set()
    try:
        for summary in batch["summaries"]:
            summary_id = summary.get("summary_id", "") if isinstance(summary, dict) else ""
            if summary_id in summary_ids:
                results.append({"summary_id": summary_id, "status": "rejected", "error_code": "INVALID_IDENTIFIER"})
                continue
            summary_ids.add(summary_id)
            try:
                validated = validate_edge_feature_summary(summary, batch["edge_node_id"])
                status, error_code = repository.ingest_summary(validated)
                result = {"summary_id": validated["summary_id"], "status": status}
                if error_code:
                    result["error_code"] = error_code
                results.append(result)
            except ContractError as error:
                results.append({"summary_id": summary_id, "status": "rejected", "error_code": error.code})
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"error_code": "SERVICE_UNAVAILABLE"})
    return {"batch_id": batch["batch_id"], "results": results}
