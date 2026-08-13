"""FastAPI entry point for the edge inference service."""
# 该模块提供边缘推理服务的 FastAPI 启动入口。

from __future__ import annotations

import atexit
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from common.config import load_config, service_url
from common.schemas import ContractError, error_response, is_v01_task_request
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
from edge_aggregation import WindowTransferError  # noqa: E402
from edge_diagnosis import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    RUNTIME_MODEL_VERSION,
    RandomForestDiagnosticModel,
)
from edge_model.config import EdgeModelConfig, ModelClientConfig  # noqa: E402
from edge_model.model_client import ModelClient  # noqa: E402
from edge_model.pipeline import EdgeModelPipeline  # noqa: E402
from edge_perception import (  # noqa: E402
    ConstantDetectionConfig,
    EdgePerception,
    PerceptionConfig,
    file_sha256,
)
from edge_runtime import (  # noqa: E402
    ControlServerConfig,
    EdgeRuntimeConfig,
    MqttConfig,
    SchedulerConfig,
    WindowTransferConfig,
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
from edge_status_reporter import build_edge_status_integration  # noqa: E402


config = load_config()
edge_status_integration = build_edge_status_integration(
    edge_node_id=EDGE_NODE_ID,
    default_model_version=RUNTIME_MODEL_VERSION,
)
runtime_assembly = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if runtime_assembly is not None:
        runtime_assembly.service.start()
    try:
        async with edge_status_integration.lifespan(app):
            yield
    finally:
        if runtime_assembly is not None:
            runtime_assembly.service.stop()


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


def _build_runtime():
    fir = EDGE_RUNTIME_SRC / "edge_perception" / "assets" / "fir_64k_to_16k_369.txt"
    source = "development_test"
    perception = EdgePerception(
        PerceptionConfig(
            profile=source,
            fir_coefficients_path=fir,
            fir_sha256=file_sha256(fir),
            fir_asset_source=source,
            fir_asset_version="bundled-v1",
            running_speed_threshold_rpm=100.0,
            running_speed_threshold_source=source,
            running_speed_threshold_version="runtime-v1",
            constant_detection={
                name: ConstantDetectionConfig(True, 1e-9, source, "runtime-v1")
                for name in ("vibration", "phase_current_1_A", "phase_current_2_A")
            },
            feature_zero_rms_threshold=1e-10,
            feature_zero_power_threshold=1e-20,
            current_relationship_zero_rms_threshold=1e-10,
            numerical_threshold_source=source,
            numerical_threshold_version="runtime-v1",
            feature_extractor_version="edge-perception-v1",
            runtime_dependencies={"numpy": np.__version__},
            absolute_tolerance=1e-12,
            relative_tolerance=1e-9,
        )
    )
    model_config = EdgeModelConfig()
    model_client = ModelClient(
        ModelClientConfig(base_url=os.getenv("EDGE_MODEL_BASE_URL", "http://127.0.0.1:8012"))
    )
    pipeline = EdgeModelPipeline(
        model_config,
        model_client,
        RandomForestDiagnosticModel(
            os.getenv("EDGE_RF_MODEL_DIR", str(DEFAULT_MODEL_DIR))
        ),
        on_run_record=lambda _: None,
        on_packet_result=lambda _: None,
    )
    mqtt_settings = config.get("mqtt", {})
    transfer_settings = config.get("bearing_window_transfer", {})
    mqtt = MqttConfig(
        host=os.getenv("EDGE_MQTT_HOST", str(mqtt_settings.get("host", "127.0.0.1"))),
        port=int(os.getenv("EDGE_MQTT_PORT", str(mqtt_settings.get("port", 1883)))),
        qos=int(mqtt_settings.get("qos", 1)),
        input_topic=os.getenv(
            "EDGE_MQTT_INPUT_TOPIC",
            str(mqtt_settings.get("input_topic", f"edge/{EDGE_NODE_ID}/input")),
        ),
        client_id=os.getenv(
            "EDGE_MQTT_CLIENT_ID",
            str(mqtt_settings.get("client_id", f"{EDGE_NODE_ID}-runtime")),
        ),
    )
    scheduler_base = os.getenv(
        "SCHEDULER_SERVICE_BASE_URL",
        service_url("scheduler", config),
    )
    cloud_base = os.getenv("CLOUD_SERVICE_BASE_URL", "http://127.0.0.1:8004")
    runtime_config = EdgeRuntimeConfig(
        edge_node_id=EDGE_NODE_ID,
        mqtt=mqtt,
        scheduler=SchedulerConfig(base_url=scheduler_base),
        control=ControlServerConfig(
            host=os.getenv("EDGE_CONTROL_HOST", "0.0.0.0"),
            port=int(os.getenv("EDGE_CONTROL_PORT", "8011")),
        ),
        window_transfer=WindowTransferConfig(
            cache_directory=Path(
                os.getenv(
                    "EDGE_BEARING_WINDOW_CACHE_DIR",
                    str(
                        Path(__file__).resolve().parents[1]
                        / str(transfer_settings.get("cache_directory", "edge_service/data/bearing_windows"))
                    ),
                )
            ),
            cloud_base_url=cloud_base,
            hard_limit_bytes=int(transfer_settings.get("hard_limit_gib", 20)) * 1024**3,
            warning_bytes=int(transfer_settings.get("warning_gib", 16)) * 1024**3,
            reserved_free_bytes=int(transfer_settings.get("reserved_free_gib", 10)) * 1024**3,
            dispatch_interval_seconds=float(
                transfer_settings.get("dispatch_interval_seconds", 1.0)
            ),
        ),
        cloud_node_urls={"cloud_01": cloud_base},
    )
    return build_edge_runtime(
        config=runtime_config,
        ingress=task_ingress,
        cache=task_ingress.validation_cache,
        perception=perception,
        pipeline=pipeline,
        enable_heartbeat=False,
    )


runtime_assembly = _build_runtime()
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

@app.get("/health")
def health() -> dict[str, object]:
    return {
        "service": "edge_service",
        "node_id": EDGE_NODE_ID,
        "status": "ok",
        "port": config["services"]["edge"]["port"],
        "model_backend": config["model"]["edge_backend"],
        "model_version": runtime_assembly.coordinator.pipeline.fallback.model_version,
        "model_deployment_status": (
            runtime_assembly.coordinator.pipeline.fallback.deployment_status
        ),
        "mqtt_connected": runtime_assembly.service.mqtt_ingress.connected,
        "mqtt_topic": runtime_assembly.service.config.mqtt.input_topic,
        "mqtt_queue_depth": runtime_assembly.service.mqtt_ingress.queue_depth,
        "bearing_window_cache_bytes": runtime_assembly.window_review_store.usage_bytes(),
        "bearing_window_cache_warning": runtime_assembly.window_review_store.warning,
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
