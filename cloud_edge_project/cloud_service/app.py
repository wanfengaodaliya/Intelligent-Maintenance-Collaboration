"""FastAPI entry point for the cloud inference service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
import sqlite3
from pathlib import Path
from typing import Any

import requests
from fastapi import BackgroundTasks, Body, FastAPI
from fastapi.responses import JSONResponse

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.global_analysis.contracts import DEFAULT_TASK_LIMIT
from cloud_service.global_analysis.service import GlobalAnalysisService
from cloud_service.model_update.service import ModelUpdateError, ModelUpdateService
from scenarios.bearing.cloud.model_update.config import load_label_mapping
from cloud_service.bearing_review.service import (
    BearingReviewService,
    BearingReviewConflictError,
    BearingReviewValidationError,
)
from cloud_service.bearing_review.receiver import BearingRawContextReceiver
from cloud_service.task_results import TaskResultService
from cloud_service.context_aggregation.coordinator import ContextAggregationCoordinator
from cloud_service.context_aggregation.dispatcher import AggregationReadyDispatcher
from cloud_service.context_aggregation.recovery import WindowRecoveryScanner
from cloud_service.errors import CloudServiceError
from cloud_service.model import CLOUD_NODE_ID
from cloud_service.raw_context.receiver import RawContextReceiver
from cloud_service.raw_context.transport import HttpRawContextTransport
from cloud_service.storage.database import initialize_database
from cloud_service.storage.edge_feature_repository import EdgeFeatureRepository
from cloud_service.storage.raw_context_repository import (
    RawContextRequestRepository,
)
from cloud_service.workflow_review import WorkflowReviewError, WorkflowReviewService
from common.config import load_config
from common.schemas import (
    ContractError,
    error_response,
    require_confidence,
    require_field,
    require_mapping,
    require_non_empty_string,
    validate_edge_feature_summary,
    validate_edge_feature_summary_envelope,
)
from core.scenario_registry import (
    DEFAULT_SCENARIO_TYPE,
    get_scenario_handler,
)
from core.scenario_errors import UnsupportedScenarioError
from core.arbitration_contracts import ArbitrationValidationError


config = load_config()


def _run_enhanced_analysis(database_path: Path, review_id: str) -> None:
    handler = get_scenario_handler(
        DEFAULT_SCENARIO_TYPE,
        database_path=database_path,
    )
    handler.run_enhanced_analysis(review_id)


async def _expire_raw_context_requests() -> None:
    while True:
        try:
            settings = load_cloud_settings()
            initialize_database(settings.database_path)
            RawContextRequestRepository(
                settings.database_path
            ).expire_due()
            await asyncio.to_thread(
                WorkflowReviewService(settings.database_path).process_pending,
                20,
            )
            await asyncio.to_thread(
                ContextAggregationCoordinator(settings.database_path).aggregate_eligible,
                config_version="cloud-preprocess-v1",
                limit=20,
            )
            await asyncio.to_thread(
                AggregationReadyDispatcher(
                    settings.database_path,
                    handler=lambda payload: _run_enhanced_analysis(
                        settings.database_path, payload["review_id"]
                    ),
                ).dispatch_pending,
                limit=20,
            )
        except sqlite3.Error:
            pass
        await asyncio.sleep(0.5)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    settings = load_cloud_settings()
    await asyncio.to_thread(
        WindowRecoveryScanner(settings.database_path).warn_orphan_files
    )
    expiry_task = asyncio.create_task(_expire_raw_context_requests())
    try:
        yield
    finally:
        expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiry_task


app = FastAPI(title="cloud_service", lifespan=_lifespan)


def infer_cloud_v01(payload: dict[str, Any]) -> dict[str, Any]:
    """Produce the documented V0.1 CloudResult from an edge result."""

    request = require_mapping(payload, "CloudRequest")
    task_id = require_non_empty_string(require_field(request, "task_id"), "task_id")
    for field in ("scenario", "task_type", "source_node"):
        require_non_empty_string(require_field(request, field, task_id), field, task_id)
    require_mapping(require_field(request, "data", task_id), "data", task_id)
    edge_result = require_mapping(require_field(request, "edge_result", task_id), "edge_result", task_id)
    label = require_field(edge_result, "label", task_id)
    if label not in {"normal", "abnormal"}:
        raise ContractError("INVALID_PACKET", "edge_result.label must be normal or abnormal", task_id)
    edge_confidence = require_confidence(
        require_field(edge_result, "confidence", task_id), "edge_result.confidence", task_id
    )
    risk_level = require_field(edge_result, "risk_level", task_id)
    if risk_level not in {"low", "medium", "high"}:
        raise ContractError("INVALID_PACKET", "edge_result.risk_level must be low, medium, or high", task_id)

    if label == "abnormal":
        confidence = max(edge_confidence, 0.93)
        decision = {"action": "send_alert", "description": "设备存在高风险异常，建议停机检查"}
        final_risk = "high"
    else:
        confidence = max(edge_confidence, 0.9)
        decision = {"action": "ignore", "description": "未发现高风险异常，建议持续监测"}
        final_risk = "low"
    return {
        "task_id": task_id,
        "node_id": "cloud_1",
        "model_name": "cloud_full_model",
        "label": label,
        "confidence": round(confidence, 2),
        "risk_level": final_risk,
        "cloud_latency_ms": 1.0,
        "decision": decision,
    }


def _workflow_review_service() -> WorkflowReviewService:
    return WorkflowReviewService(load_cloud_settings().database_path)


def _workflow_error_response(error: WorkflowReviewError) -> JSONResponse:
    status = 404 if error.code == "REVIEW_NOT_FOUND" else 409 if error.code.endswith("CONFLICT") else 400
    return JSONResponse(status_code=status, content={"error_code": error.code, "message": str(error)})


def _workflow_job_response(job: dict[str, Any], status_code: int = 200) -> JSONResponse:
    public = {key: value for key, value in job.items() if key not in {"request", "raw_packets"}}
    return JSONResponse(status_code=status_code, content=public)


def _edge_summary_repository() -> EdgeFeatureRepository:
    database_path = Path(
        os.getenv("CLOUD_SUMMARY_DATABASE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "cloud_summary.db"))
    )
    initialize_database(database_path)
    return EdgeFeatureRepository(database_path)


def _models_url(settings: CloudSettings) -> str:
    base_url = settings.vllm_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url[: -len("/chat/completions")] + "/models"
    return base_url + "/models"


def _raw_context_transport() -> HttpRawContextTransport:
    edge = config["services"]["edge"]
    return HttpRawContextTransport(
        os.getenv(
            "EDGE_RAW_CONTEXT_BASE_URL",
            f"http://{edge['host']}:{edge['port']}",
        ),
        timeout_seconds=float(
            os.getenv("EDGE_RAW_CONTEXT_TIMEOUT_SECONDS", "3")
        ),
    )


def _health_payload(settings: CloudSettings, status: str) -> dict[str, object]:
    return {
        "service": "cloud_service",
        "node_id": CLOUD_NODE_ID,
        "status": status,
        "port": int(
            os.getenv(
                "CLOUD_SERVICE_PORT",
                str(config["services"]["cloud"]["port"]),
            )
        ),
        "model_backend": settings.backend,
    }


@app.get("/health", response_model=None)
def health() -> dict[str, object] | JSONResponse:
    settings = load_cloud_settings()
    if settings.backend == "mock":
        return _health_payload(settings, "ok")
    if settings.backend != "vllm":
        return JSONResponse(
            status_code=500,
            content=_health_payload(settings, "unavailable"),
        )

    headers: dict[str, str] = {}
    if settings.vllm_api_key:
        headers["Authorization"] = f"Bearer {settings.vllm_api_key}"
    try:
        response = requests.get(
            _models_url(settings),
            headers=headers,
            timeout=min(settings.vllm_timeout_seconds, 3.0),
        )
        response.raise_for_status()
    except requests.RequestException:
        return JSONResponse(
            status_code=503,
            content=_health_payload(settings, "unavailable"),
        )
    return _health_payload(settings, "ok")


@app.post("/cloud/infer", response_model=None)
def cloud_infer(payload: dict) -> dict | JSONResponse:
    try:
        if "task_id" in payload:
            return infer_cloud_v01(payload)
        settings = load_cloud_settings()
        handler = get_scenario_handler(
            payload.get("scenario_type", DEFAULT_SCENARIO_TYPE),
            database_path=settings.database_path,
        )
        return handler.infer(payload)
    except UnsupportedScenarioError as error:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "UNSUPPORTED_SCENARIO",
                "message": str(error),
            },
        )
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except CloudServiceError as error:
        packet = payload.get("cloud_raw_packet", {}) if isinstance(payload.get("cloud_raw_packet"), dict) else {}
        contract_error = ContractError(
            error.code,
            error.message,
            packet.get("packet_id"),
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_response(contract_error),
        )
    except Exception as exc:
        packet = payload.get("cloud_raw_packet", {}) if isinstance(payload.get("cloud_raw_packet"), dict) else {}
        error = ContractError("MODEL_INFER_FAILED", str(exc), packet.get("packet_id"))
        return JSONResponse(status_code=500, content=error_response(error))


@app.post("/cloud/packet-reviews", response_model=None)
def create_packet_review(payload: dict, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        service = _workflow_review_service()
        job = service.submit("PACKET", payload)
        background_tasks.add_task(service.process, job["review_id"])
        return _workflow_job_response(job, 202)
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.get("/cloud/packet-reviews/{review_id}", response_model=None)
def get_packet_review(review_id: str) -> JSONResponse:
    try:
        return _workflow_job_response(_workflow_review_service().get(review_id))
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.post("/cloud/bearing-window-reviews", response_model=None)
def create_bearing_window_review(payload: dict) -> JSONResponse:
    try:
        return _workflow_job_response(
            _workflow_review_service().submit("BEARING_WINDOW", payload), 202
        )
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.post("/cloud/bearing-window-reviews/{review_id}/raw-batch", response_model=None)
def upload_bearing_window_raw(
    review_id: str, payload: dict, background_tasks: BackgroundTasks
) -> JSONResponse:
    try:
        service = _workflow_review_service()
        job = service.upload_window_raw(review_id, payload)
        background_tasks.add_task(service.process, review_id)
        return _workflow_job_response(job, 202)
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.get("/cloud/bearing-window-reviews/{review_id}", response_model=None)
def get_bearing_window_review(review_id: str) -> JSONResponse:
    try:
        return _workflow_job_response(_workflow_review_service().get(review_id))
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.post("/cloud/device-reviews", response_model=None)
def create_device_review(payload: dict, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        service = _workflow_review_service()
        job = service.submit("DEVICE", payload)
        background_tasks.add_task(service.process, job["review_id"])
        return _workflow_job_response(job, 202)
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.get("/cloud/device-reviews/{review_id}", response_model=None)
def get_device_review(review_id: str) -> JSONResponse:
    try:
        return _workflow_job_response(_workflow_review_service().get(review_id))
    except WorkflowReviewError as error:
        return _workflow_error_response(error)


@app.post("/cloud/bearing-review", response_model=None)
def create_bearing_review(payload: dict) -> dict | JSONResponse:
    try:
        return BearingReviewService(
            load_cloud_settings().database_path,
            transport=_raw_context_transport(),
        ).create(payload)
    except BearingReviewValidationError as error:
        return JSONResponse(status_code=400, content={"error_code": error.code})
    except BearingReviewConflictError as error:
        return JSONResponse(status_code=409, content={"error_code": error.code})


@app.get("/cloud/bearing-review/{bearing_review_id}", response_model=None)
def get_bearing_review(bearing_review_id: str) -> dict | JSONResponse:
    result = BearingReviewService(
        load_cloud_settings().database_path,
        transport=_raw_context_transport(),
    ).get(bearing_review_id)
    if result is None:
        return JSONResponse(status_code=404, content={"error_code": "BEARING_REVIEW_NOT_FOUND"})
    return result


@app.post("/cloud/bearing-task-results", response_model=None)
def bearing_task_results(payload: dict) -> dict | JSONResponse:
    try: return TaskResultService(load_cloud_settings().database_path).ingest_bearing(payload)
    except ValueError as error: return JSONResponse(status_code=400, content={"error_code": str(error)})


@app.post("/cloud/device-task-results", response_model=None)
def device_task_results(payload: dict) -> dict | JSONResponse:
    try: return TaskResultService(load_cloud_settings().database_path).ingest_device(payload)
    except ValueError as error: return JSONResponse(status_code=400, content={"error_code": str(error)})


@app.post("/cloud/device-arbitration", response_model=None)
def device_arbitration(payload: dict) -> dict | JSONResponse:
    try:
        settings = load_cloud_settings()
        handler = get_scenario_handler(
            payload.get("scenario_type", DEFAULT_SCENARIO_TYPE),
            database_path=settings.database_path,
        )
        return handler.arbitrate_device_conflict(payload)
    except UnsupportedScenarioError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": "UNSUPPORTED_SCENARIO", "message": str(error)},
        )
    except ArbitrationValidationError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": error.code, "message": error.message},
        )
    except Exception:
        return JSONResponse(status_code=500, content={"error_code": "ARBITRATION_FAILED"})


@app.get("/cloud/device-arbitration/{conflict_id}", response_model=None)
def get_device_arbitration(conflict_id: str) -> dict | JSONResponse:
    try:
        handler = get_scenario_handler(
            DEFAULT_SCENARIO_TYPE,
            database_path=load_cloud_settings().database_path,
        )
        result = handler.get_device_arbitration(conflict_id)
    except Exception:
        return JSONResponse(status_code=500, content={"error_code": "ARBITRATION_FAILED"})
    if result is None:
        return JSONResponse(status_code=404, content={"error_code": "ARBITRATION_NOT_FOUND"})
    return result


@app.post("/cloud/global-analysis", response_model=None)
def global_analysis(payload: dict) -> dict | JSONResponse:
    """执行并保存一个场景分析对象的全局分析。"""

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"error_code": "INVALID_GLOBAL_ANALYSIS_REQUEST"},
        )
    try:
        result = GlobalAnalysisService(load_cloud_settings().database_path).analyze(
            payload.get("scenario_type"),
            payload.get("subject_id"),
            payload.get("task_limit", DEFAULT_TASK_LIMIT),
        )
        return {"success": True, "result": result}
    except UnsupportedScenarioError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": "UNSUPPORTED_SCENARIO", "message": str(error)},
        )
    except ValueError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": "INVALID_GLOBAL_ANALYSIS_REQUEST", "message": str(error)},
        )
    except sqlite3.Error:
        return JSONResponse(
            status_code=503,
            content={"error_code": "SERVICE_UNAVAILABLE"},
        )


@app.get(
    "/cloud/global-analysis/{scenario_type}/{subject_id}/latest",
    response_model=None,
)
def get_latest_global_analysis(
    scenario_type: str, subject_id: str
) -> dict | JSONResponse:
    """读取一个场景分析对象最近一次已保存的全局分析。"""

    result = GlobalAnalysisService(load_cloud_settings().database_path).repository.get_latest(
        scenario_type, subject_id
    )
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"error_code": "GLOBAL_ANALYSIS_NOT_FOUND"},
        )
    return {"success": True, "result": result}


def _model_update_service() -> ModelUpdateService:
    settings = load_cloud_settings()
    source_database = os.getenv("PACKET_SOURCE_DATABASE_PATH")
    label_mapping_path = os.getenv("PADERBORN_LABEL_MAPPING_PATH")
    data_root = os.getenv("MODEL_UPDATE_DATA_ROOT")
    return ModelUpdateService(
        settings.database_path,
        data_root=Path(data_root) if data_root else None,
        packet_source_database_path=(
            Path(source_database) if source_database else None
        ),
        label_mapping=load_label_mapping(
            Path(label_mapping_path) if label_mapping_path else None
        ),
    )


def _model_update_error_response(error: ModelUpdateError) -> JSONResponse:
    status_code = 404 if error.code in {
        "GLOBAL_ANALYSIS_NOT_FOUND",
        "PROBLEM_CANDIDATE_NOT_FOUND",
        "UPDATE_NOT_FOUND",
        "DATASET_MANIFEST_NOT_FOUND",
    } else 409 if error.code == "INVALID_UPDATE_STATE" else 400
    return JSONResponse(status_code=status_code, content={"error_code": error.code})


@app.post("/cloud/model-update", response_model=None)
def create_model_update(payload: dict) -> dict | JSONResponse:
    try:
        return _model_update_service().create(payload)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/validate", response_model=None)
def validate_model_update(update_id: str, payload: Any = Body(...)) -> dict | JSONResponse:
    try:
        test_results = payload.get("test_results") if isinstance(payload, dict) else None
        if not isinstance(test_results, list):
            raise ModelUpdateError("INVALID_VALIDATION_RESULT")
        return _model_update_service().validate(update_id, test_results)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/prepare-data", response_model=None)
def prepare_model_update_data(
    update_id: str, payload: Any = Body(None)
) -> dict | JSONResponse:
    try:
        version = (
            payload.get("feature_pipeline_version", "edge_feature_v1")
            if isinstance(payload, dict)
            else "edge_feature_v1"
        )
        return _model_update_service().prepare_data(
            update_id, feature_pipeline_version=version
        )
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/training-result", response_model=None)
def register_model_training_result(
    update_id: str, payload: Any = Body(...)
) -> dict | JSONResponse:
    try:
        if not isinstance(payload, dict):
            raise ModelUpdateError("INVALID_TRAINING_RESULT")
        return _model_update_service().register_training_result(update_id, payload)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/start-training", response_model=None)
def start_model_update_training(update_id: str) -> dict | JSONResponse:
    try:
        return _model_update_service().start_training(update_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.get("/cloud/model-update/{update_id}", response_model=None)
def get_model_update(update_id: str) -> dict | JSONResponse:
    try:
        return _model_update_service().get(update_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/approve", response_model=None)
def approve_model_update(update_id: str, payload: Any = Body(None)) -> dict | JSONResponse:
    try:
        confirmed_by = payload.get("confirmed_by") if isinstance(payload, dict) else None
        return _model_update_service().approve(update_id, confirmed_by=confirmed_by)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/reject", response_model=None)
def reject_model_update(update_id: str, payload: Any = Body(None)) -> dict | JSONResponse:
    try:
        confirmed_by = payload.get("confirmed_by") if isinstance(payload, dict) else None
        return _model_update_service().reject(update_id, confirmed_by=confirmed_by)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/handoff-distribution", response_model=None)
def handoff_model_update_distribution(update_id: str) -> dict | JSONResponse:
    try:
        return _model_update_service().handoff_distribution(update_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/distribution-result", response_model=None)
def record_model_update_distribution(
    update_id: str, payload: Any = Body(...)
) -> dict | JSONResponse:
    try:
        if not isinstance(payload, dict):
            raise ModelUpdateError("INVALID_DISTRIBUTION_RESULT")
        return _model_update_service().record_distribution_result(update_id, payload)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/post-validate", response_model=None)
def post_validate_model_update(
    update_id: str, payload: Any = Body(...)
) -> dict | JSONResponse:
    try:
        analysis_id = payload.get("analysis_id") if isinstance(payload, dict) else None
        if not isinstance(analysis_id, str) or not analysis_id:
            raise ModelUpdateError("INVALID_POST_VALIDATION_REQUEST")
        return _model_update_service().post_validate(update_id, analysis_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/request-rollback", response_model=None)
def request_model_update_rollback(
    update_id: str, payload: Any = Body(...)
) -> dict | JSONResponse:
    try:
        requested_by = payload.get("requested_by") if isinstance(payload, dict) else None
        return _model_update_service().request_rollback(
            update_id, requested_by=requested_by
        )
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/raw-context-batches", response_model=None)
def raw_context_batches(
    payload: Any = Body(...),
) -> dict[str, object] | JSONResponse:
    """Receive one edge raw-context batch and acknowledge each packet."""

    try:
        if isinstance(payload, dict) and payload.get("review_type") == "bearing_review":
            return BearingRawContextReceiver(
                load_cloud_settings().database_path
            ).receive_batch(payload)
        return RawContextReceiver(
            load_cloud_settings().database_path
        ).receive_batch(payload)
    except ContractError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": error.code, "message": error.message},
        )
    except BearingReviewValidationError as error:
        return JSONResponse(status_code=400, content={"error_code": error.code})
    except BearingReviewConflictError as error:
        return JSONResponse(status_code=409, content={"error_code": error.code})
    except sqlite3.Error:
        return JSONResponse(
            status_code=503,
            content={"error_code": "SERVICE_UNAVAILABLE"},
        )


@app.get("/cloud/reviews/{review_id}/summary", response_model=None)
def final_summary(review_id: str) -> dict[str, object] | JSONResponse:
    handler = get_scenario_handler(
        DEFAULT_SCENARIO_TYPE,
        database_path=load_cloud_settings().database_path,
    )
    summary = handler.get_final_summary(review_id)
    if summary is None:
        return JSONResponse(status_code=404, content={"error_code": "SUMMARY_NOT_READY"})
    return summary


@app.post("/cloud/edge-feature-summaries", response_model=None)
def edge_feature_summaries(payload: Any = Body(...)) -> dict[str, object] | JSONResponse:
    """Receive a batch of edge summaries and acknowledge each item independently."""

    try:
        batch = validate_edge_feature_summary_envelope(payload)
    except ContractError as error:
        return JSONResponse(status_code=400, content={"error_code": error.code})

    repository = _edge_summary_repository()
    results: list[dict[str, str]] = []
    summary_ids: set[str] = set()
    try:
        for summary in batch["summaries"]:
            summary_id = summary.get("summary_id", "") if isinstance(summary, dict) else ""
            if summary_id in summary_ids:
                results.append({"summary_id": summary_id, "status": "rejected", "error_code": "INVALID_IDENTIFIER"})
                continue
            summary_ids.add(summary_id)
            try:
                validated = validate_edge_feature_summary(summary, batch["edge_node_id"])
                status, error_code = repository.ingest_summary(validated)
                result = {"summary_id": validated["summary_id"], "status": status}
                if error_code:
                    result["error_code"] = error_code
                results.append(result)
            except ContractError as error:
                results.append({"summary_id": summary_id, "status": "rejected", "error_code": error.code})
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"error_code": "SERVICE_UNAVAILABLE"})
    return {"batch_id": batch["batch_id"], "results": results}
