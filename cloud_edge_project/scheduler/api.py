"""FastAPI entry point for the scheduler service."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config
from common.schemas import ContractError, error_response
from scheduler.rule_scheduler import SCHEDULER_NODE_ID, decide_schedule


config = load_config()
app = FastAPI(title="scheduler_service")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "service": "scheduler_service",
        "node_id": SCHEDULER_NODE_ID,
        "status": "ok",
        "port": config["services"]["scheduler"]["port"],
        "model_backend": "rule",
    }


@app.post("/scheduler/decide", response_model=None)
def scheduler_decide(payload: dict) -> dict | JSONResponse:
    try:
        return decide_schedule(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except Exception as exc:
        packet_id = payload.get("packet", {}).get("packet_id") if isinstance(payload.get("packet"), dict) else None
        error = ContractError("SCHEDULER_FAILED", str(exc), packet_id)
        return JSONResponse(status_code=500, content=error_response(error))
