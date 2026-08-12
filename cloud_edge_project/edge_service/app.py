"""FastAPI entry point for the edge inference service."""
# 该模块提供边缘推理服务的 FastAPI 启动入口。

from __future__ import annotations

import atexit
import sys
import requests
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config
from common.schemas import ContractError, error_response
from edge_service.model import EDGE_NODE_ID, infer_edge, infer_edge_v01


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
from packet_routing_bridge import PacketRoutingBridge  # noqa: E402
from cloud_review import (  # noqa: E402
    CloudReviewError,
    CloudReviewCleanupWorker,
    CloudReviewService,
    CloudReviewStore,
    HttpCloudClient,
    SchedulerUploadReporter,
    load_cloud_review_config,
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
cloud_review_config = load_cloud_review_config()
cloud_review_store = CloudReviewStore(
    cloud_review_config.cache_directory,
    retention_ns=cloud_review_config.retention_ns,
)
cloud_review_service = CloudReviewService(
    cloud_review_store,
    cloud_client=HttpCloudClient(cloud_review_config.cloud_base_url, timeout_seconds=cloud_review_config.timeout_seconds),
    scheduler_reporter=SchedulerUploadReporter(cloud_review_config.scheduler_base_url, timeout_seconds=cloud_review_config.timeout_seconds),
    edge_node_id=EDGE_NODE_ID,
)
cloud_review_cleanup = CloudReviewCleanupWorker(
    cloud_review_store,
    interval_seconds=cloud_review_config.cleanup_interval_seconds,
)
cloud_review_cleanup.start()
atexit.register(cloud_review_cleanup.stop)

def _post_scheduler_packet_route(path: str, payload: dict) -> dict:
    response = requests.post("http://127.0.0.1:8003" + path, json=payload, timeout=3.0)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("scheduler response must be an object")
    return result


packet_routing_bridge = PacketRoutingBridge(edge_node_id=EDGE_NODE_ID, store=cloud_review_store, post=_post_scheduler_packet_route)


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
        if "task_id" in payload:
            return infer_edge_v01(payload)
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



@app.post("/edge/packets", response_model=None)
def submit_edge_packet(payload: dict) -> JSONResponse:
    received = task_ingress.receive_packet(payload)
    if received.status != "ACCEPTED_FOR_PROCESSING":
        return JSONResponse(status_code=400, content={"accepted": False, "error_code": received.error_code, "packet_id": payload.get("packet_id")})
    try:
        decision = packet_routing_bridge.route(payload, infer_edge(payload))
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except Exception as error:
        return JSONResponse(status_code=503, content={"accepted": False, "error_code": "PACKET_ROUTE_UNAVAILABLE", "message": str(error)})
    return JSONResponse(status_code=202, content={"accepted": True, "packet_id": payload["packet_id"], "decision": decision})

@app.post("/edge/cloud-review-tasks", response_model=None)
def submit_cloud_review_task(payload: dict) -> dict | JSONResponse:
    try:
        return cloud_review_service.handle(payload)
    except CloudReviewError as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"error_code": error.code, "message": error.message},
        )
    except Exception as error:
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "EDGE_CLOUD_REVIEW_UNAVAILABLE",
                "message": str(error),
            },
        )
