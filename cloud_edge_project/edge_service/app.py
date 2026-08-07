"""FastAPI entry point for the edge inference service."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config
from common.schemas import ContractError, error_response
from edge_service.model import EDGE_NODE_ID, infer_edge


EDGE_RUNTIME_SRC = Path(__file__).resolve().parent / "src"
if str(EDGE_RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(EDGE_RUNTIME_SRC))

from edge_task_ingress import (  # noqa: E402
    TASK_CONFLICT,
    EdgeTaskIngress,
    TaskIngressConfig,
)
from edge_validation_cache import (  # noqa: E402
    EdgeValidationCache,
    ValidationCacheConfig,
)


config = load_config()
app = FastAPI(title="edge_service")


def _create_task_ingress() -> EdgeTaskIngress:
    validation_cache = EdgeValidationCache(
        ValidationCacheConfig(
            raw_cache_retention_seconds=60.0,
            max_receive_rate_per_sender=20.0,
            context_queue_capacity_per_sender=1200,
            raw_cache_capacity_per_sender=1200,
            context_before_packet_count=20,
            cache_cleanup_interval_seconds=1.0,
            hard_value_ranges={},
        )
    )
    return EdgeTaskIngress(
        TaskIngressConfig(edge_node_id=EDGE_NODE_ID),
        validation_cache,
    )


task_ingress = _create_task_ingress()


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


@app.post("/edge/tasks", response_model=None)
def register_edge_task(payload: dict) -> JSONResponse:
    ack = task_ingress.register_task(payload)
    if ack.ack_status == "ACCEPTED":
        status_code = 200
    elif ack.reason_code == TASK_CONFLICT:
        status_code = 409
    else:
        status_code = 400
    return JSONResponse(status_code=status_code, content=ack.as_dict())
