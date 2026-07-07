"""FastAPI entry point for the cloud inference service."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cloud_service.model import CLOUD_NODE_ID, infer_cloud
from common.config import load_config
from common.schemas import ContractError, error_response


config = load_config()
app = FastAPI(title="cloud_service")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "service": "cloud_service",
        "node_id": CLOUD_NODE_ID,
        "status": "ok",
        "port": config["services"]["cloud"]["port"],
        "model_backend": config["model"]["cloud_backend"],
    }


@app.post("/cloud/infer", response_model=None)
def cloud_infer(payload: dict) -> dict | JSONResponse:
    try:
        return infer_cloud(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except Exception as exc:
        packet = payload.get("packet", {}) if isinstance(payload.get("packet"), dict) else {}
        error = ContractError("MODEL_INFER_FAILED", str(exc), packet.get("packet_id"))
        return JSONResponse(status_code=500, content=error_response(error))
