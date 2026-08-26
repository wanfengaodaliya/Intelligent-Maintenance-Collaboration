from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .runtime import SummaryRuntime, load_summary_settings


runtime: SummaryRuntime | None = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    global runtime
    runtime = SummaryRuntime(load_summary_settings())
    runtime.start()
    try:
        yield
    finally:
        runtime.stop()
        runtime = None


app = FastAPI(title="summary_service", lifespan=_lifespan)


def _runtime() -> SummaryRuntime:
    if runtime is None:
        raise RuntimeError("Summary runtime is not started")
    return runtime


@app.get("/health", response_model=None)
def health() -> dict[str, Any] | JSONResponse:
    selected = _runtime()
    payload = {
        "service": "summary_service",
        "status": "ok" if selected.connected else "unavailable",
        "mqtt_connected": selected.connected,
        "mqtt_input_topic": selected.settings.mqtt_input_topic,
        "mqtt_output_topic": selected.settings.mqtt_output_topic,
        "mqtt_suggestion_topic": selected.settings.mqtt_suggestion_topic,
        "suggestion_llm_enabled": selected.settings.suggestion_llm_enabled,
        "last_error": selected.last_error,
    }
    if selected.connected:
        return payload
    return JSONResponse(status_code=503, content=payload)


@app.post("/summary/bearing-results", response_model=None)
def ingest_bearing_result(payload: dict) -> dict[str, Any] | JSONResponse:
    try:
        result = _runtime().service.ingest(payload)
        return {"accepted": True, "window_result": result}
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error_code": "INVALID_BEARING_RESULT", "message": str(exc)},
        )


@app.get("/summary/window-results")
def list_window_results(device_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    parsed_limit = min(max(int(limit), 1), 1000)
    return {
        "items": _runtime().repository.list_window_results(
            device_id=device_id, limit=parsed_limit
        )
    }


@app.get("/summary/metrics")
def metrics(device_id: str | None = None) -> dict[str, Any]:
    return _runtime().repository.metrics(device_id=device_id)
