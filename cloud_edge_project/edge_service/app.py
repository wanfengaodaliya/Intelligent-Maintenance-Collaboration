"""FastAPI entry point for the edge inference service."""
# 该模块提供边缘推理服务的 FastAPI 启动入口。

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDGE_RUNTIME_SRC = Path(__file__).resolve().parent / "src"
for import_root in (PROJECT_ROOT, EDGE_RUNTIME_SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common.config import load_config, service_url
from common.schemas import ContractError, error_response, is_v01_task_request
from edge_service.model import EDGE_NODE_ID, infer_edge, infer_edge_v01

from edge_task_ingress import (  # noqa: E402
    TASK_CONFLICT,
    EdgeTaskIngress,
    TaskIngressConfig,
)
from edge_validation_cache import (  # noqa: E402
    EdgeValidationCache,
    ValidationCacheConfig,
)
from edge_aggregation import WindowTransferError  # noqa: E402
from edge_diagnosis import H5_RUNTIME_MODEL_VERSION, DistilledH5DiagnosticModel  # noqa: E402
from edge_model.config import EdgeModelConfig, ModelClientConfig  # noqa: E402
from edge_model.model_client import ModelClient  # noqa: E402
from edge_model.pipeline import EdgeModelPipeline  # noqa: E402
from edge_runtime import (  # noqa: E402
    EdgeRuntimeConfig,
    PacketRouteErrorRecorder,
    build_edge_runtime,
)
from cloud_review import (  # noqa: E402
    CloudReviewError,
    CloudReviewCleanupWorker,
    CloudReviewService,
    CloudReviewStore,
    HttpCloudClient,
    SchedulerUploadReporter,
    load_cloud_review_config,
)
from edge_status_reporter import ModelStatus, build_edge_status_integration  # noqa: E402


config = load_config()
diagnostic_backend = str(config["model"]["edge_backend"])
if diagnostic_backend != "distilled_h5":
    raise ValueError("unsupported edge diagnostic backend: %s" % diagnostic_backend)
runtime_model_version = H5_RUNTIME_MODEL_VERSION
edge_status_integration = build_edge_status_integration(
    edge_node_id=EDGE_NODE_ID,
    default_model_version=runtime_model_version,
)
runtime_assembly = None
cloud_review_cleanup = None
EDGE_FEATURE_EXTRACTOR_VERSION = "distilled-h5-three-branch-v1"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if runtime_assembly is not None:
        runtime_assembly.service.start()
    if cloud_review_cleanup is not None:
        cloud_review_cleanup.start()
    try:
        async with edge_status_integration.lifespan(app):
            yield
    finally:
        if runtime_assembly is not None:
            runtime_assembly.service.stop()
        if cloud_review_cleanup is not None:
            cloud_review_cleanup.stop()


app = FastAPI(title="edge_service", lifespan=_lifespan)
edge_status_integration.install(app)


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


def _build_runtime(review_store: CloudReviewStore | None = None):
    if review_store is None:
        review_store = cloud_review_store

    model_config = EdgeModelConfig()
    model_client = ModelClient(
        ModelClientConfig(base_url=os.getenv("EDGE_MODEL_BASE_URL", "http://127.0.0.1:8012"))
    )
    pipeline = EdgeModelPipeline(
        model_config,
        model_client,
        DistilledH5DiagnosticModel(),
        on_run_record=lambda _: None,
        on_packet_result=lambda _: None,
    )
    mqtt_settings = config.get("mqtt", {})
    transfer_settings = config.get("bearing_window_transfer", {})
    # 统一配置入口：环境变量优先，local.yaml 只作为本地开发兜底。
    os.environ.setdefault("EDGE_NODE_ID", EDGE_NODE_ID)
    os.environ.setdefault("EDGE_MQTT_HOST", str(mqtt_settings.get("host", "127.0.0.1")))
    os.environ.setdefault("EDGE_MQTT_PORT", str(mqtt_settings.get("port", 1883)))
    os.environ.setdefault(
        "EDGE_MQTT_INPUT_TOPIC",
        str(mqtt_settings.get("input_topic", f"edge/{EDGE_NODE_ID}/input")),
    )
    os.environ.setdefault(
        "EDGE_MQTT_CLIENT_ID",
        str(mqtt_settings.get("client_id", f"{EDGE_NODE_ID}-runtime")),
    )
    os.environ.setdefault(
        "SCHEDULER_SERVICE_BASE_URL", service_url("scheduler", config)
    )
    os.environ.setdefault("CLOUD_SERVICE_BASE_URL", "http://127.0.0.1:18021")
    os.environ.setdefault(
        "EDGE_BEARING_WINDOW_CACHE_DIR",
        str(
            Path(__file__).resolve().parents[1]
            / str(transfer_settings.get("cache_directory", "edge_service/data/bearing_windows"))
        ),
    )
    os.environ.setdefault(
        "EDGE_WINDOW_HARD_LIMIT_GIB", str(transfer_settings.get("hard_limit_gib", 20))
    )
    os.environ.setdefault(
        "EDGE_WINDOW_WARNING_GIB", str(transfer_settings.get("warning_gib", 16))
    )
    os.environ.setdefault(
        "EDGE_WINDOW_RESERVED_FREE_GIB", str(transfer_settings.get("reserved_free_gib", 10))
    )
    os.environ.setdefault(
        "EDGE_WINDOW_DISPATCH_INTERVAL_SECONDS",
        str(transfer_settings.get("dispatch_interval_seconds", 1.0)),
    )
    os.environ.setdefault(
        "EDGE_WINDOW_PACKET_CLOUD_CONFIDENCE_THRESHOLD",
        str(transfer_settings.get("packet_cloud_confidence_threshold", 0.0)),
    )
    runtime_config = EdgeRuntimeConfig.from_env()
    packet_route_error_recorder = PacketRouteErrorRecorder(
        os.getenv(
            "EDGE_PACKET_ROUTE_ERROR_LOG",
            str(Path(__file__).resolve().parents[1] / "data" / "edge_packet_route_errors.jsonl"),
        )
    )
    return build_edge_runtime(
        config=runtime_config,
        ingress=task_ingress,
        cache=task_ingress.validation_cache,
        pipeline=pipeline,
        cloud_review_store=review_store,
        on_packet_route_error=packet_route_error_recorder,
        enable_heartbeat=False,
    )


cloud_review_config = load_cloud_review_config()
cloud_review_store = CloudReviewStore(
    cloud_review_config.cache_directory,
    retention_ns=cloud_review_config.retention_ns,
)
runtime_assembly = _build_runtime(cloud_review_store)


# 阶段 2：把 Reporter 的业务快照接到真实运行时，替换固定值。
if edge_status_integration.state is not None:

    def _runtime_queue_breakdown() -> dict[str, int]:
        # EDGE-4：queue_length/queue_breakdown 表达“计算/工作流积压”，不包含
        # 结果发送积压。按任务生命周期分桶统计，各桶互不重复——
        # ingress（MQTT 接入等待）→ model_pending（等待推理）
        # → aggregation_waiting（推理完成、等待聚合回补）
        # → cloud_review_retry（已入云复核、等待重试）。
        # 已完成的推理结果等待 HTTP/MQTT 发布属 device_result_outbox（交付积压），
        # 在 /health 独立暴露，不得计入本计算队列，否则会让 Scheduler 误判
        # 节点仍有大量推理负载而重复惩罚通信故障。
        coordinator = runtime_assembly.coordinator
        return {
            "ingress": runtime_assembly.service.mqtt_ingress.queue_depth,
            "model_pending": coordinator.pipeline.queue_length,
            "aggregation_waiting": coordinator.pending_aggregation_count,
            "cloud_review_retry": len(
                cloud_review_store.list_decisions(phase="CLOUD_RETRY_WAIT")
            ),
        }

    def _runtime_queue_length() -> int:
        # 总负载 = 各生命周期分桶之和，与 queue_breakdown 保持同一口径。
        return sum(_runtime_queue_breakdown().values())

    def _runtime_models() -> tuple[ModelStatus, ...]:
        coordinator = runtime_assembly.coordinator
        fallback = coordinator.pipeline.fallback
        version = getattr(fallback, "model_version", runtime_model_version)
        return (ModelStatus(version, coordinator.model_load_status),)

    edge_status_integration.state.attach_runtime_providers(
        queue_length_provider=_runtime_queue_length,
        queue_breakdown_provider=_runtime_queue_breakdown,
        models_provider=_runtime_models,
        activity_ns_provider=lambda: runtime_assembly.coordinator.last_task_activity_ns,
    )


cloud_review_service = CloudReviewService(
    cloud_review_store,
    cloud_client=HttpCloudClient(cloud_review_config.cloud_base_url, timeout_seconds=cloud_review_config.request_timeout()),
    scheduler_reporter=SchedulerUploadReporter(cloud_review_config.scheduler_base_url, timeout_seconds=cloud_review_config.request_timeout()),
    edge_node_id=EDGE_NODE_ID,
    cloud_result_handler=runtime_assembly.v12_flow,
)
# 注入协调器，由后台维护轮次驱动可重试复核的到期重试。
runtime_assembly.coordinator.cloud_review_service = cloud_review_service
cloud_review_cleanup = CloudReviewCleanupWorker(
    cloud_review_store,
    interval_seconds=cloud_review_config.cleanup_interval_seconds,
)

def _status_reporter_healthy(integration: Any) -> bool:
    """EDGE-1: 状态上报模块整体是否为 OK；DEGRADED/FAILED 或未装配 → false。"""
    reporter = integration.reporter if integration is not None else None
    if reporter is None:
        return False
    return reporter.health()["status"] == "ok"


@app.get("/health")
def health() -> dict[str, object]:
    model = runtime_assembly.coordinator.pipeline.fallback
    maintenance = runtime_assembly.maintenance
    outbox = runtime_assembly.device_result_outbox
    maintenance_health = maintenance.health() if maintenance is not None else None
    ready = runtime_assembly.service.started and (
        maintenance is None
        or bool(maintenance_health and maintenance_health["maintenance_worker_running"])
    )
    return {
        "service": "edge_service",
        "node_id": EDGE_NODE_ID,
        "status": "ok" if ready else "starting",
        "ready": ready,
        "port": config["services"]["edge"]["port"],
        "model_backend": diagnostic_backend,
        "model_version": model.model_version,
        "model_deployment_status": model.deployment_status,
        "feature_extractor_version": EDGE_FEATURE_EXTRACTOR_VERSION,
        "feature_schema_version": getattr(model, "feature_schema_version", None),
        "model_input_schema_version": getattr(
            model, "model_input_schema_version", None
        ),
        "mqtt_connected": runtime_assembly.service.mqtt_ingress.connected,
        "mqtt_topic": runtime_assembly.service.config.mqtt.input_topic,
        "mqtt_queue_depth": runtime_assembly.service.mqtt_ingress.queue_depth,
        "legacy_bearing_aggregation_enabled": runtime_assembly.window_review_store is not None,
        "bearing_window_cache_bytes": (
            None
            if runtime_assembly.window_review_store is None
            else runtime_assembly.window_review_store.usage_bytes()
        ),
        "bearing_window_cache_warning": (
            False
            if runtime_assembly.window_review_store is None
            else runtime_assembly.window_review_store.warning
        ),
        "maintenance": maintenance_health,
        "device_result_outbox": outbox.health() if outbox is not None else None,
        # EDGE-1: 状态上报模块自身交付健康；DEGRADED/FAILED → status_reporter_healthy=false。
        "status_reporter": (
            edge_status_integration.reporter.health()
            if edge_status_integration.reporter is not None
            else None
        ),
        "status_reporter_healthy": _status_reporter_healthy(edge_status_integration),
        "http_timeout_ms": {
            "connect": runtime_assembly.service.config.v12.http_connect_timeout_ms,
            "read": runtime_assembly.service.config.v12.http_read_timeout_ms,
            "cloud_now": runtime_assembly.service.config.v12.cloud_now_timeout_ms,
            "round": runtime_assembly.service.config.v12.round_timeout_ms,
        },
    }


@app.post("/edge/infer", response_model=None)
def edge_infer(payload: Any = Body(default=None)) -> dict | JSONResponse:
    try:
        if is_v01_task_request(payload):
            return infer_edge_v01(payload)
        return infer_edge(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except Exception as exc:
        packet_id = payload.get("packet_id") if isinstance(payload, dict) else None
        error = ContractError("MODEL_INFER_FAILED", str(exc), packet_id)
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


def _forward_edge_control(path: str, payload: Any) -> JSONResponse:
    # 统一对外控制入口：任务控制与仲裁回调都收敛到同一应用地址，
    # 处理逻辑复用运行时控制应用，旧控制端口仅保留兼容。
    application = runtime_assembly.service.control_application
    body = payload if isinstance(payload, dict) else {}
    status, result = application.handle(path, body)
    return JSONResponse(status_code=status, content=result)


@app.post("/edge/task-revocations", response_model=None)
def edge_task_revocation(payload: dict = Body(default=None)) -> JSONResponse:
    return _forward_edge_control("/edge/task-revocations", payload)


@app.post("/edge/device-arbitration-results", response_model=None)
def edge_device_arbitration_result(payload: dict = Body(default=None)) -> JSONResponse:
    return _forward_edge_control("/edge/device-arbitration-results", payload)



@app.post("/edge/packets", response_model=None)
def submit_edge_packet(payload: dict) -> JSONResponse:
    try:
        accepted = runtime_assembly.coordinator.receive_raw_packet(payload)
    except WindowTransferError as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"accepted": False, "error_code": error.code, "message": str(error)},
        )
    except Exception as error:
        return JSONResponse(status_code=503, content={"accepted": False, "error_code": "EDGE_INGRESS_UNAVAILABLE", "message": str(error)})
    if not accepted:
        return JSONResponse(status_code=409, content={"accepted": False, "error_code": "EDGE_PACKET_REJECTED", "packet_id": payload.get("packet_id")})
    return JSONResponse(status_code=202, content={"accepted": True, "packet_id": payload.get("packet_id")})


@app.post("/edge/raw-context-requests", response_model=None)
def raw_context_request(payload: dict) -> dict | JSONResponse:
    if runtime_assembly.window_review_store is None:
        return JSONResponse(
            status_code=410,
            content={"error_code": "LEGACY_BEARING_AGGREGATION_DISABLED"},
        )
    try:
        record = runtime_assembly.window_review_store.attach_context_request(payload)
        return {
            "request_id": payload.get("request_id"),
            "status": "accepted",
            "window_id": record["window_id"],
        }
    except WindowTransferError as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"error_code": error.code, "message": str(error)},
        )

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
