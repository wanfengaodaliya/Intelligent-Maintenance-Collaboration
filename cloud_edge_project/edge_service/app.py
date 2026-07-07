"""FastAPI entry point for the edge inference service."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config
from common.schemas import ContractError, error_response
from edge_service.model import EDGE_NODE_ID, infer_edge


config = load_config()
app = FastAPI(title="edge_service")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "service": "edge_service",
        "node_id": EDGE_NODE_ID,
        "status": "ok",
        "port": config["services"]["edge"]["port"],
        "model_backend": config["model"]["edge_backend"],
    }


@app.post("/edge/infer", response_model=None)
def edge_infer(payload: dict) -> dict | JSONResponse:
    try:
        return infer_edge(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except Exception as exc:
        error = ContractError("MODEL_INFER_FAILED", str(exc), payload.get("packet_id"))
        return JSONResponse(status_code=500, content=error_response(error))
