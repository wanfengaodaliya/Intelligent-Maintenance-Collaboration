"""FastAPI entry point for the edge inference service."""
# 该模块提供边缘推理服务的 FastAPI 启动入口。

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDGE_RUNTIME_SRC = Path(__file__).resolve().parent / "src"
for import_root in (PROJECT_ROOT, EDGE_RUNTIME_SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common.config import load_config
from common.control_auth import CONTROL_PATHS, ControlAuthVerifier
from common.schemas import ContractError, error_response, is_v01_task_request
from edge_service.model import EDGE_NODE_ID, infer_edge, infer_edge_v01

from edge_task_ingress import (  # noqa: E402
    EdgeTaskIngress,
    TaskIngressConfig,
)
from edge_validation_cache import (  # noqa: E402
    EdgeValidationCache,
    ValidationCacheConfig,
)
from edge_model.config import EdgeModelConfig, ModelClientConfig  # noqa: E402
from edge_model.contracts import RunRecord  # noqa: E402
from edge_model.local_h5_client import (  # noqa: E402
    H5_RUNTIME_MODEL_VERSION,
    LocalH5ClientConfig,
    LocalH5ModelClient,
)
from edge_model.model_client import ModelClient  # noqa: E402
from edge_model.model_store import (  # noqa: E402
    ModelStoreBootstrapError,
    initialize_model_store,
    validate_model_update_mode,
)
from edge_model.perception_evidence import PerceptionEvidenceBuilder  # noqa: E402
from edge_model.pipeline import EdgeModelPipeline  # noqa: E402
from edge_model.unavailable_runner import DiagnosisUnavailableRunner  # noqa: E402
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
from edge_runtime.trace_identity import trace_id_for_task  # noqa: E402
from edge_runtime.body_limit import RequestBodyLimitMiddleware  # noqa: E402


LOGGER = logging.getLogger(__name__)
config = load_config()
control_auth_verifier = ControlAuthVerifier.from_env()
# 正式边缘诊断路线（阶段 8 起）：
#   - local_h5：蒸馏模型 H5 三通道并行（CNN/物理特征/工况）加权融合，本地推理；
#   - official：宿主机/远端正式模型服务（HTTP /infer），供对照与故障演练矩阵。
# 环境变量 EDGE_DIAGNOSTIC_BACKEND 可覆盖 configs/local.yaml 的声明。
diagnostic_backend = os.getenv("EDGE_DIAGNOSTIC_BACKEND", "") or str(
    config["model"]["edge_backend"]
)
if diagnostic_backend not in ("local_h5", "official"):
    raise ValueError(
        "unsupported edge diagnostic backend: %s (allowed: 'local_h5' | 'official')"
        % diagnostic_backend
    )
pinned_model_version = (os.getenv("EDGE_MODEL_VERSION") or "").strip() or None
poller_enabled = (
    os.getenv("EDGE_MODEL_UPDATE_POLLER_ENABLED", "false").strip().lower()
    == "true"
)
build_revision = (os.getenv("EDGE_BUILD_REVISION") or "unknown").strip()
try:
    validate_model_update_mode(
        pinned_version=pinned_model_version,
        poller_enabled=poller_enabled,
    )
except ModelStoreBootstrapError as exc:
    error_code = str(exc).split(":", 1)[0]
    sys.stderr.write(
        json.dumps(
            {"level": "fatal", "error_code": error_code},
            ensure_ascii=False,
        )
        + "\n"
    )
    raise SystemExit(78) from exc
# 模型版本以部署时显式声明为准；local_h5 未声明时使用 H5 制品版本。
runtime_model_version = pinned_model_version or (
    H5_RUNTIME_MODEL_VERSION if diagnostic_backend == "local_h5"
    else "official-model-unpinned"
)
edge_status_integration = build_edge_status_integration(
    edge_node_id=EDGE_NODE_ID,
    default_model_version=runtime_model_version,
)
runtime_assembly = None
cloud_review_cleanup = None
# local_h5 复用 H5 自带的单包证据合同；official 路线用独立的 numpy 证据构建器。
EDGE_FEATURE_EXTRACTOR_VERSION = (
    H5_RUNTIME_MODEL_VERSION if diagnostic_backend == "local_h5"
    else PerceptionEvidenceBuilder.version
)


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
app.add_middleware(
    RequestBodyLimitMiddleware,
    default_limit_bytes=1024 * 1024,
    path_limits={path: 64 * 1024 for path in CONTROL_PATHS},
)


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


def _load_runtime_config() -> EdgeRuntimeConfig:
    mqtt_settings = config.get("mqtt", {})
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
    os.environ.setdefault("SCHEDULER_SERVICE_BASE_URL", "http://127.0.0.1:18011")
    os.environ.setdefault("CLOUD_SERVICE_BASE_URL", "http://127.0.0.1:18021")
    os.environ.setdefault(
        "EDGE_V12_DATABASE_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "edge_v12.db"),
    )
    return EdgeRuntimeConfig.from_env()


def _apply_model_runtime_env(model_config: EdgeModelConfig) -> None:
    """Apply optional deployment-specific model timeout budgets."""
    model_config.timeout.queue_wait_ms = int(
        os.getenv("EDGE_MODEL_QUEUE_WAIT_MS", str(model_config.timeout.queue_wait_ms))
    )
    model_config.timeout.total_ms = int(
        os.getenv("EDGE_MODEL_TOTAL_TIMEOUT_MS", str(model_config.timeout.total_ms))
    )


def _configure_local_h5_torch_threads() -> dict[str, int]:
    """限制单个本地 H5 推理的 PyTorch 内部并行，避免多 worker 过度争抢 CPU。"""
    intraop = int(os.getenv("EDGE_TORCH_INTRAOP_THREADS", "1"))
    interop = int(os.getenv("EDGE_TORCH_INTEROP_THREADS", "1"))
    if intraop < 1 or interop < 1:
        raise ValueError("EDGE_TORCH_INTRAOP_THREADS and EDGE_TORCH_INTEROP_THREADS must be >= 1")
    import torch

    torch.set_num_threads(intraop)
    torch.set_num_interop_threads(interop)
    return {"intraop": intraop, "interop": interop}


def _record_failed_model_run(
    recorder: PacketRouteErrorRecorder, record: RunRecord
) -> None:
    if not record.output_valid:
        recorder(record.as_dict())


def _build_runtime(review_store: CloudReviewStore | None = None):
    if review_store is None:
        review_store = cloud_review_store

    runtime_config = _load_runtime_config()
    model_config = EdgeModelConfig()
    _apply_model_runtime_env(model_config)
    if diagnostic_backend == "local_h5":
        thread_config = _configure_local_h5_torch_threads()
        LOGGER.info("configured local H5 PyTorch threads: %s", thread_config)
    # 阶段 7：推理队列容量/满载策略可配置（方案 6.2 满载策略细化）。
    # 默认容量 64：双 Sender 50ms 节奏 ≈ 40 窗口/秒时提供 >1.5s 突发缓冲；
    # 原默认 1 会在任何突发下把窗口全部打入降级失败，任务无法收敛。
    model_config.queue.max_waiting_requests = int(
        os.getenv("EDGE_MODEL_QUEUE_CAPACITY", "64")
    )
    model_config.queue.full_policy = os.getenv(
        "EDGE_MODEL_QUEUE_FULL_POLICY", "reject"
    )
    # H4：固定推理线程池大小（默认 1 保持现行为；local_h5 可配 2 提升并行）。
    model_config.inference_workers = int(
        os.getenv("EDGE_MODEL_INFERENCE_WORKERS", "1")
    )
    # 阶段 7.2：EDGE_MODEL_VERSION 为版本 pin（可选）；
    # 设置后模型路线（本地 H5 或模型服务）上报版本不一致 → readiness 不通过。
    if diagnostic_backend == "local_h5":
        # 正式路线：蒸馏模型 H5 三通道并行本地推理，与模型服务路线共用
        # 有界队列/超时预算/熔断/就绪探针；降级语义仍为"诊断不可用"。
        model_config.diagnostic_backend = "local_h5"
        selection = initialize_model_store(
            model_root=runtime_config.model_update.model_root,
            bundled_model_root=Path(__file__).resolve().parent / "models",
            baseline_version=H5_RUNTIME_MODEL_VERSION,
            pinned_version=pinned_model_version,
        )
        model_client = LocalH5ModelClient(
            LocalH5ClientConfig(
                model_root=selection.model_root,
                initial_version=selection.version,
                expected_version=pinned_model_version,
            )
        )
        initial_readiness = model_client.readiness()
        if not initial_readiness.ok:
            raise RuntimeError(initial_readiness.detail)
        evidence_builder = model_client.build_evidence
    else:
        model_config.diagnostic_backend = "http"
        model_client = ModelClient(
            ModelClientConfig(
                base_url=os.getenv("EDGE_MODEL_BASE_URL", "http://127.0.0.1:8012"),
                expected_version=pinned_model_version,
            )
        )
        evidence_builder = PerceptionEvidenceBuilder().build_evidence
    model_run_recorder = PacketRouteErrorRecorder(
        os.getenv(
            "EDGE_MODEL_RUN_LOG",
            str(Path(__file__).resolve().parents[1] / "data" / "edge_model_runs.jsonl"),
        )
    )
    # 降级语义（两种路线一致）：“诊断不可用”，等待云复核，不产生伪诊断。
    pipeline = EdgeModelPipeline(
        model_config,
        model_client,
        DiagnosisUnavailableRunner(),
        on_run_record=lambda record: _record_failed_model_run(model_run_recorder, record),
        on_packet_result=lambda _: None,
        evidence_builder=evidence_builder,
    )
    if (
        diagnostic_backend == "local_h5"
        and runtime_config.v12.enabled
        and runtime_config.v12.diagnosis_window_ms != 50
    ):
        # H5 蒸馏模型输入冻结为 50 ms（振动 3200 点 @64kHz，硬校验）；
        # 非 50 ms 窗口合并后超出输入尺寸，H5 校验失败会导致整窗
        # "诊断不可用"降级。启动即失败，而不是运行中静默降级。
        raise ValueError(
            "local_h5 requires v12.diagnosis_window_ms=50, got %d"
            % runtime_config.v12.diagnosis_window_ms
        )
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
        control_auth_verifier=control_auth_verifier,
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
        # 阶段 7.2：状态上报优先采用模型服务上报的真实版本。
        probe = coordinator.pipeline.model_readiness()
        reported = probe.get("model_version")
        version = reported if isinstance(reported, str) and reported else runtime_model_version
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

def _liveness_snapshot() -> dict[str, object]:
    """阶段 5：liveness 只看进程内关键线程是否存活。

    H2：把模型 worker、完成分发线程、建议线程纳入判定，避免"推理已静默死亡
    但 liveness 仍 200"的假活。模型更新轮询器非关键路径，仅观测上报。
    """
    maintenance = runtime_assembly.maintenance
    mqtt_ingress = runtime_assembly.service.mqtt_ingress
    coordinator = runtime_assembly.coordinator
    maintenance_alive = True if maintenance is None else maintenance.running
    # local 后端不启动推理 worker（无队列线程），不纳入判定，避免永久 503。
    uses_model_worker = coordinator.pipeline.cfg.diagnostic_backend in ("http", "local_h5")
    model_worker_alive = (
        bool(coordinator.pipeline.worker.worker_alive) if uses_model_worker else True
    )
    dispatcher_alive = bool(coordinator.completion_dispatcher_alive)
    suggestion_alive = bool(coordinator.suggestion_worker_alive)
    poller = runtime_assembly.service.model_update_poller
    poller_alive = True if poller is None else poller.running
    critical = bool(
        maintenance_alive
        and mqtt_ingress.worker_alive
        and model_worker_alive
        and dispatcher_alive
        and suggestion_alive
    )
    return {
        "alive": critical,
        "maintenance_worker_alive": maintenance_alive,
        "mqtt_worker_alive": mqtt_ingress.worker_alive,
        "model_worker_alive": model_worker_alive,
        "completion_dispatcher_alive": dispatcher_alive,
        "suggestion_worker_alive": suggestion_alive,
        "model_update_poller_alive": poller_alive,
    }


def _readiness_snapshot() -> dict[str, object]:
    """阶段 5/7.4：readiness 表示当前是否满足接收新任务的条件。

    阶段 7.4：模型服务就绪纳入判定——模型不可用（或版本 pin 不一致）时
    readiness 不通过，调度方应停止派发新任务（隔离故障，而非接单后失败）。
    """
    maintenance = runtime_assembly.maintenance
    service = runtime_assembly.service
    maintenance_running = True if maintenance is None else maintenance.running
    checks = {
        "service_started": bool(service.started),
        "maintenance_worker_running": bool(maintenance_running),
        "mqtt_connected": bool(service.mqtt_ingress.connected),
    }
    pipeline = runtime_assembly.coordinator.pipeline
    if pipeline.cfg.diagnostic_backend in ("http", "local_h5"):
        probe = pipeline.model_readiness()
        checks["model_service_ready"] = bool(probe.get("probed") and probe.get("ok"))
    return {
        "ready": all(checks.values()),
        "checks": checks,
    }


@app.get("/health/live")
def health_live() -> JSONResponse:
    snapshot = _liveness_snapshot()
    status_code = 200 if snapshot["alive"] else 503
    return JSONResponse(status_code=status_code, content=snapshot)


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    snapshot = _readiness_snapshot()
    status_code = 200 if snapshot["ready"] else 503
    return JSONResponse(status_code=status_code, content=snapshot)


def _status_reporter_healthy(integration: Any) -> bool:
    """EDGE-1: 状态上报模块整体是否为 OK；DEGRADED/FAILED 或未装配 → false。"""
    reporter = integration.reporter if integration is not None else None
    if reporter is None:
        return False
    return reporter.health()["status"] == "ok"


@app.get("/health")
def health() -> dict[str, object]:
    maintenance = runtime_assembly.maintenance
    outbox = runtime_assembly.device_result_outbox
    maintenance_health = maintenance.health() if maintenance is not None else None
    readiness = _readiness_snapshot()
    ready = readiness["ready"]
    # 阶段 7.2：优先使用模型服务 readiness 上报的真实版本（未 pin 且未探测到时回退）。
    probe = runtime_assembly.coordinator.pipeline.model_readiness()
    reported_version = probe.get("model_version")
    displayed_version = (
        reported_version if isinstance(reported_version, str) and reported_version
        else runtime_model_version
    )
    model_update_poller = runtime_assembly.service.model_update_poller
    model_update_health = (
        model_update_poller.health() if model_update_poller is not None else None
    )
    return {
        "service": "edge_service",
        "build_revision": build_revision,
        "node_id": EDGE_NODE_ID,
        "status": "ok" if ready else "starting",
        "ready": ready,
        "liveness": _liveness_snapshot(),
        "readiness": readiness,
        "port": config["services"]["edge"]["port"],
        "model_backend": diagnostic_backend,
        # 阶段 6：正式模型版本来自部署声明；本地不再持有模型制品。
        "model_version": displayed_version,
        "model_version_pinned": runtime_model_version if os.getenv("EDGE_MODEL_VERSION") else None,
        "model_deployment_status": (
            "local_distilled_h5" if diagnostic_backend == "local_h5"
            else "official_model_service"
        ),
        "model_update": {
            "enabled": bool(runtime_assembly.service.config.model_update.enabled),
            "model_root": str(
                runtime_assembly.service.config.model_update.model_root
            ),
            "poller": model_update_health,
            "last_error_code": (
                model_update_health.get("last_error_code")
                if model_update_health is not None
                else None
            ),
        },
        # 阶段 7.4：模型路线就绪探针快照（含版本 pin 校验结论）。
        # local_h5 路线 base_url 为空（模型在进程内）。
        "model_service": {
            "base_url": (
                None if diagnostic_backend == "local_h5"
                else os.getenv("EDGE_MODEL_BASE_URL", "http://127.0.0.1:8012")
            ),
            **probe,
        },
        "feature_extractor_version": EDGE_FEATURE_EXTRACTOR_VERSION,
        "mqtt_connected": runtime_assembly.service.mqtt_ingress.connected,
        "mqtt_topic": runtime_assembly.service.config.mqtt.input_topic,
        "mqtt_queue_depth": runtime_assembly.service.mqtt_ingress.queue_depth,
        # 阶段 5：入站容量指标（满载拒绝数、最老任务年龄），过载行为可观测。
        "mqtt_capacity": runtime_assembly.service.mqtt_ingress.capacity_snapshot(),
        # 阶段 7：模型队列容量与满载指标（等待数/容量/满载累计/历史峰值）。
        "model_queue": runtime_assembly.coordinator.pipeline.queue_snapshot(),
        # H1/H3：完成分发与建议线程观测（队列深度/溢出/存活）。
        "completion_dispatch": {
            "queue_size": runtime_assembly.coordinator.dispatch_queue_size,
            "overflow_total": runtime_assembly.coordinator.dispatch_overflow_total,
            "alive": runtime_assembly.coordinator.completion_dispatcher_alive,
        },
        "suggestion_worker": {
            "queue_size": runtime_assembly.coordinator.suggestion_queue_size,
            "alive": runtime_assembly.coordinator.suggestion_worker_alive,
        },
        "maintenance": maintenance_health,
        "device_result_outbox": outbox.health() if outbox is not None else None,
        "suggestion_outbox": (
            runtime_assembly.suggestion_outbox.health()
            if runtime_assembly.suggestion_outbox is not None else None
        ),
        # 阶段 5：关键外部发送队列指标（积压、死信、最老记录年龄）。
        "result_upload": (
            runtime_assembly.coordinator.result_uploader.health()
            if runtime_assembly.coordinator.result_uploader is not None else None
        ),
        "raw_sample_queue": (
            runtime_assembly.coordinator.raw_sample_capture.repository.health()
            if runtime_assembly.coordinator.raw_sample_capture is not None else None
        ),
        "outbound_routes": {
            # 阶段 4：暴露出站链路地址，便于确认流量是否经过网络模拟代理。
            "scheduler_base_url": runtime_assembly.service.config.scheduler.base_url,
            "cloud_node_urls": dict(runtime_assembly.service.config.cloud_node_urls),
            "mqtt_host": runtime_assembly.service.config.mqtt.host,
            "mqtt_port": runtime_assembly.service.config.mqtt.port,
            "device_result_topic": runtime_assembly.service.config.mqtt.device_result_topic,
            "network_link_id": os.getenv(
                "EDGE_NETWORK_LINK_ID", f"{EDGE_NODE_ID}__to__scheduler__http"
            ),
        },
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
    except Exception:
        packet_id = payload.get("packet_id") if isinstance(payload, dict) else None
        content = _unexpected_api_error(
            "MODEL_INFER_FAILED",
            "edge inference failed",
            payload,
        )
        content.update({"success": False, "packet_id": packet_id})
        return JSONResponse(status_code=500, content=content)


@app.post("/edge/tasks", response_model=None)
async def register_edge_task(request: Request) -> JSONResponse:
    return await _forward_edge_control(request)


async def _forward_edge_control(request: Request) -> JSONResponse:
    # 统一对外控制入口：任务控制与仲裁回调都收敛到同一应用地址，
    # 处理逻辑复用运行时控制应用，旧控制端口仅保留兼容。
    application = runtime_assembly.service.control_application
    raw_body = await request.body()
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
        payload = decoded if isinstance(decoded, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    status, result = application.handle(
        request.url.path,
        payload,
        method=request.method,
        query_string=request.url.query,
        raw_body=raw_body,
        headers=request.headers,
    )
    return JSONResponse(status_code=status, content=result)


@app.post("/edge/task-revocations", response_model=None)
async def edge_task_revocation(request: Request) -> JSONResponse:
    return await _forward_edge_control(request)


@app.post("/edge/device-arbitration-results", response_model=None)
async def edge_device_arbitration_result(request: Request) -> JSONResponse:
    return await _forward_edge_control(request)



@app.post("/edge/packets", response_model=None)
def submit_edge_packet(payload: dict) -> JSONResponse:
    try:
        accepted = runtime_assembly.coordinator.receive_raw_packet(payload)
    except Exception:
        content = _unexpected_api_error(
            "EDGE_INGRESS_UNAVAILABLE",
            "edge ingress is temporarily unavailable",
            payload,
        )
        content["accepted"] = False
        return JSONResponse(status_code=503, content=content)
    if not accepted:
        return JSONResponse(status_code=409, content={"accepted": False, "error_code": "EDGE_PACKET_REJECTED", "packet_id": payload.get("packet_id")})
    return JSONResponse(status_code=202, content={"accepted": True, "packet_id": payload.get("packet_id")})


@app.post("/edge/cloud-review-tasks", response_model=None)
def submit_cloud_review_task(payload: dict) -> dict | JSONResponse:
    try:
        return cloud_review_service.handle(payload)
    except CloudReviewError as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"error_code": error.code, "message": error.message},
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=_unexpected_api_error(
                "EDGE_CLOUD_REVIEW_UNAVAILABLE",
                "cloud review is temporarily unavailable",
                payload,
            ),
        )


def _unexpected_api_error(
    error_code: str,
    generic_message: str,
    payload: Any,
) -> dict[str, str]:
    error_id = uuid.uuid4().hex
    task_id = payload.get("task_id") if isinstance(payload, dict) else None
    packet_id = payload.get("packet_id") if isinstance(payload, dict) else None
    trace_id = trace_id_for_task(task_id) if isinstance(task_id, str) and task_id else None
    LOGGER.exception(
        "%s error_id=%s trace_id=%s task_id=%s packet_id=%s",
        error_code,
        error_id,
        trace_id,
        task_id,
        packet_id,
    )
    return {
        "error_code": error_code,
        "error_id": error_id,
        "message": generic_message,
    }
