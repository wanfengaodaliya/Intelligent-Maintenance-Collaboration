"""FastAPI entry point for the cloud inference service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

LOGGER = logging.getLogger(__name__)

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.global_analysis.contracts import DEFAULT_TASK_LIMIT
from cloud_service.global_analysis.periodic import run_all as run_periodic_global_analysis
from cloud_service.global_analysis.service import GlobalAnalysisService
from cloud_service.model_update.service import ModelUpdateError, ModelUpdateService
from cloud_service.model_update.dataset_repository import (
    LabelConfirmationRepository,
    PacketSourceRepository,
)
from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from scenarios.bearing.cloud.model_update.config import load_label_mapping
from scenarios.bearing.cloud.model_update.dataset_label_provider import (
    DatasetLabelProvider,
)
from scenarios.bearing.cloud.model_update.human_review_provider import (
    HumanReviewProvider,
)
from scenarios.bearing.cloud.model_update.training_data_source import (
    BearingTrainingDataSource,
)
from cloud_service.task_results import TaskResultService
from cloud_service.device_arbitration.v12_contract import (
    adapt_v12_device_arbitration_request,
    attach_v12_identity,
    is_v12_device_arbitration_request,
)
from cloud_service.status_reporter import CloudNodeStatusReporter
from cloud_service.errors import CloudServiceError
from cloud_service.service import get_moment_runner, preload_moment_runner
from cloud_service.vllm_backend import infer_v01_vllm
from cloud_service.edge_status_registry import EdgeStatusRegistry, EdgeStatusValidationError
from cloud_service.model import CLOUD_NODE_ID
from cloud_service.raw_analysis import RawAnalysisSampleService, SignalAnalysisWorker
from cloud_service.storage.database import initialize_database
from cloud_service.storage.edge_feature_repository import EdgeFeatureRepository
from common.config import load_config
from common.schemas import (
    ContractError,
    error_response,
    is_v01_cloud_request,
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
    register_handler,
)
from scenarios.bearing.cloud.handler import BearingCloudHandler
from core.scenario_errors import UnsupportedScenarioError
from core.arbitration_contracts import ArbitrationValidationError
from scenarios.bearing.cloud.global_analysis.bearing_risk_analyzer import analyze_bearing_risk
from scenarios.bearing.cloud.global_analysis.bearing_aggregation_analyzer import analyze_bearing_aggregation
from scenarios.bearing.cloud.global_analysis.analyzer import build_bearing_maintenance_recommendations


config = load_config()
edge_status_registry = EdgeStatusRegistry()

# Register the default bearing scenario handler at module level.
# This ensures get_scenario_handler() works without core importing scenarios.
register_handler("bearing", BearingCloudHandler)


def build_cloud_status_reporter() -> CloudNodeStatusReporter:
    # 出站 HTTP 默认经过网络模拟器代理链路（links.yaml: cloud__to__scheduler__http）。
    scheduler_base_url = os.getenv(
        "SCHEDULER_SERVICE_BASE_URL",
        "http://127.0.0.1:18045",
    )

    def health_provider() -> tuple[str, str]:
        result = health()
        if isinstance(result, JSONResponse) and result.status_code >= 400:
            return "DEGRADED", "FAILED"
        return "ONLINE", "LOADED"

    return CloudNodeStatusReporter(
        scheduler_base_url=scheduler_base_url,
        cloud_node_id=os.getenv("CLOUD_REVIEW_NODE_ID", "cloud_01").strip(),
        settings_provider=load_cloud_settings,
        queue_length_provider=lambda: 0,
        health_provider=health_provider,
        last_activity_provider=lambda: 0,
    )


status_reporter = build_cloud_status_reporter()


def _build_bearing_global_analyzers() -> dict[str, Any]:
    """Build scenario-specific analyzers for the bearing scenario.

    Each wrapper extracts the relevant data slice from the full data dict
    that GlobalAnalysisService passes to all injected analyzers (data, config).
    """
    def _bearing_risk_wrapper(data: dict[str, Any], config: Any) -> dict[str, Any]:
        rows = data.get("bearing_tasks", [])
        return analyze_bearing_risk(rows, config)

    def _bearing_review_wrapper(data: dict[str, Any], config: Any) -> dict[str, Any]:
        rows = data.get("bearing_review_pairs", [])
        if not rows:
            return {"status": "not_available", "bearing_review_count": 0}
        return analyze_bearing_aggregation(rows, config)

    def _maintenance_wrapper(device_health: dict[str, Any], bearing_risk: dict[str, Any]) -> list[str]:
        if bearing_risk is None:
            return []
        return build_bearing_maintenance_recommendations(device_health, bearing_risk)

    return {
        "analyze_bearing_risk": _bearing_risk_wrapper,
        "analyze_cloud_bearing_review": _bearing_review_wrapper,
        "maintenance_recommendations": _maintenance_wrapper,
    }


async def _run_background_workers() -> None:
    while True:
        try:
            settings = load_cloud_settings()
            await asyncio.to_thread(
                SignalAnalysisWorker(RawAnalysisSampleService(settings.database_path)).run_once,
                now_ns=time.time_ns(),
            )
        except sqlite3.Error as exc:
            LOGGER.exception("signal analysis worker failed: %s", exc)
        await asyncio.sleep(0.5)


def _run_periodic_global_analysis_once(database_path: Path) -> list[str]:
    """Run global analysis for every known bearing device in a single pass."""
    analyzers = _build_bearing_global_analyzers()
    return run_periodic_global_analysis(
        database_path, scenario_type="bearing", analyzers=analyzers
    )


async def _run_periodic_global_analysis() -> None:
    lock = asyncio.Lock()
    while True:
        settings = load_cloud_settings()
        interval = settings.global_analysis_poll_seconds
        if interval <= 0:
            await asyncio.sleep(60.0)
            continue
        try:
            if lock.locked():
                LOGGER.warning("skip periodic global analysis: previous run still in progress")
            else:
                async with lock:
                    await asyncio.to_thread(
                        _run_periodic_global_analysis_once, settings.database_path
                    )
        except Exception as exc:
            LOGGER.exception("periodic global analysis worker failed: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    settings = load_cloud_settings()
    if settings.backend == "moment_light_adapt":
        await asyncio.to_thread(preload_moment_runner, settings)
    worker_task = asyncio.create_task(_run_background_workers())
    global_analysis_task = asyncio.create_task(_run_periodic_global_analysis())
    status_task = asyncio.create_task(status_reporter.run_forever())
    try:
        yield
    finally:
        for task in (status_task, worker_task, global_analysis_task):
            task.cancel()
        for task in (status_task, worker_task, global_analysis_task):
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="cloud_service", lifespan=_lifespan)


def infer_cloud_v01(payload: dict[str, Any]) -> dict[str, Any]:
    """Produce the documented V0.1 CloudResult from an edge result."""

    request = require_mapping(payload, "CloudRequest")
    task_id = require_non_empty_string(require_field(request, "task_id"), "task_id")
    scenario = require_field(request, "scenario", task_id)
    if scenario not in {"industrial", "energy"}:
        raise ContractError("INVALID_PACKET", "scenario must be industrial or energy", task_id)
    for field in ("task_type", "source_node"):
        require_non_empty_string(require_field(request, field, task_id), field, task_id)
    data = require_mapping(require_field(request, "data", task_id), "data", task_id)
    if not data:
        raise ContractError("INVALID_PACKET", "data must be a non-empty object", task_id)
    edge_result = require_mapping(require_field(request, "edge_result", task_id), "edge_result", task_id)
    label = require_field(edge_result, "label", task_id)
    if label not in {"normal", "fault"}:
        raise ContractError("INVALID_PACKET", "edge_result.label must be normal or fault", task_id)
    edge_confidence = require_confidence(
        require_field(edge_result, "confidence", task_id), "edge_result.confidence", task_id
    )
    risk_level = require_field(edge_result, "risk_level", task_id)
    if risk_level not in {"low", "medium", "high"}:
        raise ContractError("INVALID_PACKET", "edge_result.risk_level must be low, medium, or high", task_id)

    settings = load_cloud_settings()
    if settings.backend == "vllm":
        return infer_v01_vllm(request, settings)
    if settings.backend != "mock":
        raise CloudServiceError(
            "INVALID_CLOUD_BACKEND",
            f"unsupported cloud backend: {settings.backend}",
            500,
        )

    if label == "fault":
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
    if settings.backend == "moment_light_adapt":
        if get_moment_runner(settings).loaded:
            return _health_payload(settings, "ok")
        return JSONResponse(
            status_code=503,
            content=_health_payload(settings, "unavailable"),
        )
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


@app.post("/cloud/edge-status", response_model=None)
def receive_edge_status(payload: dict) -> dict | JSONResponse:
    try:
        return edge_status_registry.update(payload)
    except EdgeStatusValidationError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": "INVALID_EDGE_STATUS", "message": str(error)},
        )


@app.get("/cloud/edge-status/{edge_node_id}", response_model=None)
def get_edge_status(edge_node_id: str) -> dict | JSONResponse:
    report = edge_status_registry.get(edge_node_id)
    if report is None:
        return JSONResponse(
            status_code=404,
            content={"error_code": "EDGE_STATUS_NOT_FOUND"},
        )
    return report


@app.post("/cloud/infer", response_model=None)
def cloud_infer(payload: Any = Body(default=None)) -> dict | JSONResponse:
    try:
        request = require_mapping(payload, "CloudRequest")
        if is_v01_cloud_request(request):
            return infer_cloud_v01(request)
        settings = load_cloud_settings()
        handler = get_scenario_handler(
            request.get("scenario_type", DEFAULT_SCENARIO_TYPE),
            database_path=settings.database_path,
        )
        return handler.infer(request)
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
        packet = payload.get("cloud_raw_packet", {}) if isinstance(payload, dict) and isinstance(payload.get("cloud_raw_packet"), dict) else {}
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
        packet = payload.get("cloud_raw_packet", {}) if isinstance(payload, dict) and isinstance(payload.get("cloud_raw_packet"), dict) else {}
        error = ContractError("MODEL_INFER_FAILED", str(exc), packet.get("packet_id"))
        return JSONResponse(status_code=500, content=error_response(error))


@app.post("/cloud/bearing-diagnosis-results", response_model=None)
def bearing_diagnosis_results(payload: dict) -> dict | JSONResponse:
    try:
        return TaskResultService(load_cloud_settings().database_path).ingest_bearing_decision(payload)
    except ValueError as error:
        return JSONResponse(
            status_code=409 if str(error) == "RESULT_ID_CONFLICT" else 400,
            content={"error_code": str(error)},
        )


@app.post("/cloud/device-decision-results", response_model=None)
def device_decision_results(payload: dict) -> dict | JSONResponse:
    try:
        return TaskResultService(load_cloud_settings().database_path).ingest_device_decision(payload)
    except ValueError as error:
        return JSONResponse(
            status_code=409 if str(error) == "RESULT_ID_CONFLICT" else 400,
            content={"error_code": str(error)},
        )


@app.post("/cloud/raw-analysis-samples", response_model=None)
async def raw_analysis_samples(
    metadata: str = Form(...), payload: UploadFile = File(...)
) -> dict | JSONResponse:
    try:
        result = RawAnalysisSampleService(load_cloud_settings().database_path).accept(
            json.loads(metadata), await payload.read(), received_at_ns=time.time_ns()
        )
        return result
    except (ValueError, json.JSONDecodeError) as error:
        return JSONResponse(status_code=400, content={"error_code": str(error)})


@app.get("/cloud/raw-analysis-samples/{sample_id}", response_model=None)
def get_raw_analysis_sample(sample_id: str) -> dict | JSONResponse:
    result = RawAnalysisSampleService(load_cloud_settings().database_path).get_sample(sample_id)
    return result if result is not None else JSONResponse(status_code=404, content={"error_code": "RAW_SAMPLE_NOT_FOUND"})


@app.get("/cloud/physical-evidence/{sample_id}", response_model=None)
def get_physical_evidence(sample_id: str) -> dict | JSONResponse:
    result = RawAnalysisSampleService(load_cloud_settings().database_path).get_evidence(sample_id)
    return result if result is not None else JSONResponse(status_code=404, content={"error_code": "PHYSICAL_EVIDENCE_NOT_FOUND"})


@app.post("/cloud/device-arbitration", response_model=None)
def device_arbitration(payload: dict) -> dict | JSONResponse:
    try:
        adapted = (
            adapt_v12_device_arbitration_request(payload)
            if is_v12_device_arbitration_request(payload)
            else None
        )
        settings = load_cloud_settings()
        handler = get_scenario_handler(
            (adapted or payload).get("scenario_type", DEFAULT_SCENARIO_TYPE),
            database_path=settings.database_path,
        )
        result = handler.arbitrate_device_conflict(adapted or payload)
        return attach_v12_identity(result, adapted) if adapted is not None else result
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
    except Exception as exc:
        LOGGER.exception("device arbitration failed: %s", exc)
        return JSONResponse(status_code=500, content={"error_code": "ARBITRATION_FAILED"})


@app.get("/cloud/device-arbitration/{conflict_id}", response_model=None)
def get_device_arbitration(conflict_id: str) -> dict | JSONResponse:
    try:
        handler = get_scenario_handler(
            DEFAULT_SCENARIO_TYPE,
            database_path=load_cloud_settings().database_path,
        )
        result = handler.get_device_arbitration(conflict_id)
    except Exception as exc:
        LOGGER.exception("get device arbitration failed for %s: %s", conflict_id, exc)
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
        scenario_type = payload.get("scenario_type", DEFAULT_SCENARIO_TYPE)
        analyzers = _build_bearing_global_analyzers() if scenario_type == "bearing" else None
        result = GlobalAnalysisService(
            load_cloud_settings().database_path,
            scenario_analyzers=analyzers,
        ).analyze(
            scenario_type,
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
    database_path = settings.database_path
    source_repository = PacketSourceRepository(
        Path(source_database) if source_database else database_path
    )
    label_repository = LabelConfirmationRepository(database_path)
    label_mapping = load_label_mapping(
        Path(label_mapping_path) if label_mapping_path else None
    )
    return ModelUpdateService(
        database_path,
        data_root=Path(data_root) if data_root else None,
        packet_source_database_path=(
            Path(source_database) if source_database else None
        ),
        training_data_source=BearingTrainingDataSource(
            database_path, source_repository
        ),
        label_provider=LabelConfirmationResolver(
            [
                DatasetLabelProvider(source_repository, label_mapping),
                HumanReviewProvider(label_repository),
                CloudReferenceProvider(),
            ]
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


_HUMAN_LABEL_STATES = {"normal", "warning", "fault"}


@app.get("/cloud/model-update/label-confirmations/{packet_id}", response_model=None)
def get_label_confirmation(packet_id: str) -> dict | JSONResponse:
    repository = LabelConfirmationRepository(load_cloud_settings().database_path)
    row = repository.get(packet_id)
    if row is None:
        return JSONResponse({"error": "LABEL_CONFIRMATION_NOT_FOUND"}, status_code=404)
    return dict(row)


@app.post("/cloud/model-update/label-confirmations/{packet_id}", response_model=None)
def upsert_human_label_confirmation(
    packet_id: str, payload: Any = Body(None)
) -> dict | JSONResponse:
    body = payload if isinstance(payload, dict) else {}
    confirmed_label = body.get("confirmed_label")
    if not isinstance(confirmed_label, str) or not confirmed_label.strip():
        return JSONResponse({"error": "INVALID_HUMAN_LABEL"}, status_code=400)
    risk_level = body.get("confirmed_risk_level")
    if risk_level not in _HUMAN_LABEL_STATES:
        risk_level = None
    repository = LabelConfirmationRepository(load_cloud_settings().database_path)
    saved = repository.save(
        {
            "packet_id": packet_id,
            "confirmed_label": confirmed_label.strip(),
            "label_source": "human_confirmed",
            "confirmed_risk_level": risk_level,
        }
    )
    return dict(saved)


@app.get(
    "/cloud/model-update/{update_id}/pending-label-confirmations",
    response_model=None,
)
def pending_label_confirmations(update_id: str) -> dict | JSONResponse:
    try:
        return _model_update_service().list_pending_human_confirmation(update_id)
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


@app.post("/cloud/model-update/{update_id}/execute-rollback", response_model=None)
def execute_model_update_rollback(
    update_id: str, payload: Any = Body(...)
) -> dict | JSONResponse:
    try:
        executed_by = payload.get("executed_by") if isinstance(payload, dict) else None
        return _model_update_service().execute_rollback(
            update_id, executed_by=executed_by
        )
    except ModelUpdateError as error:
        return _model_update_error_response(error)


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
