"""FastAPI entry point for JSONL logs and dashboard metrics."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config
from common.logger import append_task_trace, compute_metrics, read_task_traces
from common.schemas import ContractError, error_response


config = load_config()
app = FastAPI(title="log_service")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "service": "log_service",
        "node_id": "log_1",
        "status": "ok",
        "port": config["services"]["log"]["port"],
        "model_backend": "jsonl",
    }


@app.post("/logs/task_trace", response_model=None)
def logs_task_trace(payload: dict) -> dict | JSONResponse:
    try:
        return append_task_trace(payload, config)
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except Exception as exc:
        error = ContractError("LOG_SAVE_FAILED", str(exc), payload.get("packet_id"))
        return JSONResponse(status_code=500, content=error_response(error))


@app.get("/dashboard/metrics")
def dashboard_metrics() -> dict[str, object]:
    return compute_metrics(read_task_traces(config))


@app.get("/dashboard/tasks")
def dashboard_tasks(limit: int = 20) -> dict[str, object]:
    traces = read_task_traces(config)
    rows = [
        {
            "packet_id": trace["packet_id"],
            "device_id": trace["device_id"],
            "route": trace["route"],
            "final_label": trace["final_label"],
            "final_confidence": trace["final_confidence"],
            "risk_level": trace["risk_level"],
            "total_latency_ms": trace["total_latency_ms"],
            "success": trace["success"],
            "log_timestamp": trace["log_timestamp"],
        }
        for trace in traces[-limit:]
    ]
    return {"tasks": rows}
