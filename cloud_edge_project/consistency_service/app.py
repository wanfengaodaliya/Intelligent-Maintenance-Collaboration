"""FastAPI entry point for V0.1 decision consistency resolution."""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config
from consistency_service.resolver import ConsistencyValidationError, resolve_decisions


app = FastAPI(title="consistency_service")


@app.get("/health")
def health() -> dict[str, object]:
    service_config = load_config()["services"]["consistency"]
    return {
        "service": "consistency_service",
        "status": "ok",
        "port": service_config["port"],
    }


@app.post("/consistency/resolve", response_model=None)
def resolve(payload: Any = Body(default=None)) -> dict | JSONResponse:
    try:
        return resolve_decisions(payload)
    except ConsistencyValidationError as error:
        return JSONResponse(status_code=400, content={"error_code": "INVALID_CONSISTENCY_REQUEST", "message": str(error)})
