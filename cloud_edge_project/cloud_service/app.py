"""FastAPI entry point for the cloud inference service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import io
import json
import logging
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from common.model_signing import ModelSigningError, sign_manifest

LOGGER = logging.getLogger(__name__)

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.global_analysis.runtime_contracts import DEFAULT_TASK_LIMIT
from cloud_service.global_analysis.periodic import run_all as run_periodic_global_analysis
from cloud_service.global_analysis.service import GlobalAnalysisService
from cloud_service.model_update.service import ModelUpdateError
from cloud_service.model_update.dataset_repository import (
    LabelConfirmationRepository,
)
from cloud_service.task_results import TaskResultService
from cloud_service.device_arbitration.summary_contract import (
    adapt_summary_arbitration_request,
    attach_summary_identity,
)
from cloud_service.device_arbitration.errors import ArbitrationPayloadConflictError
from cloud_service.device_arbitration.repository import DeviceArbitrationRepository
from cloud_service.summary_windows import SummaryWindowRepository
from cloud_service.status_reporter import CloudNodeStatusReporter
from cloud_service.runtime_status import CloudRuntimeState
from cloud_service.errors import CloudServiceError
from cloud_service.service import (
    activate_moment_candidate,
    activate_moment_version,
    evaluate_cloud_window,
    get_moment_runner,
    preload_moment_runner,
)
from cloud_service.device_arbitration.service import DeviceArbitrationService
from cloud_service.edge_status_registry import EdgeStatusRegistry, EdgeStatusValidationError
from cloud_service.model import CLOUD_NODE_ID
from cloud_service.raw_analysis import RawAnalysisSampleService, SignalAnalysisWorker
from cloud_service.storage.database import initialize_database
from cloud_service.storage.edge_feature_repository import EdgeFeatureRepository
from common.config import load_config
from common.schemas import (
    ContractError,
    error_response,
    require_mapping,
    validate_edge_feature_summary,
    validate_edge_feature_summary_envelope,
)
from bootstrap.scenarios import build_cloud_scenario_registry
from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    GLOBAL_ANALYSIS,
    MODEL_UPDATE,
    STORAGE_PROVIDER,
)
from core.scenario_registry import (
    MissingScenarioCapabilityError,
    ScenarioNotFoundError,
    UnresolvedScenarioCapabilityError,
    normalize_scenario_type,
    register_handler,
)
from compatibility.bearing_v12.scenario_mapper import (
    BEARING_SCENARIO_TYPE,
    normalize_legacy_scenario_type,
)
from core.scenario_errors import UnsupportedScenarioError
from core.arbitration_contracts import ArbitrationValidationError


config = load_config()
edge_status_registry = EdgeStatusRegistry()
cloud_runtime_state = CloudRuntimeState()
scenario_registry = build_cloud_scenario_registry()
DEFAULT_SCENARIO_TYPE = BEARING_SCENARIO_TYPE


def _scenario_provider(scenario_type: object, capability: str) -> object:
    normalized = normalize_scenario_type(scenario_type)
    try:
        return scenario_registry.require_provider(normalized, capability)
    except (
        ScenarioNotFoundError,
        MissingScenarioCapabilityError,
        UnresolvedScenarioCapabilityError,
    ) as exc:
        raise UnsupportedScenarioError(normalized) from exc


storage_provider = _scenario_provider(BEARING_SCENARIO_TYPE, STORAGE_PROVIDER)


def get_scenario_handler(scenario_type: object, *, database_path: Path):
    """Compatibility seam backed by the new scenario plugin registry."""

    provider = _scenario_provider(scenario_type, CLOUD_DIAGNOSIS)
    return provider.build_handler(database_path)


class _RegistryBackedDefaultScenarioHandler:
    """Keep the legacy core handler lookup backed by the plugin registry."""

    def __new__(cls, database_path: Path):
        return get_scenario_handler(
            BEARING_SCENARIO_TYPE,
            database_path=database_path,
        )


register_handler(BEARING_SCENARIO_TYPE, _RegistryBackedDefaultScenarioHandler)


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

    def model_runtime_provider() -> tuple[str, bool]:
        runner = get_moment_runner(load_cloud_settings())
        return runner.model_version, runner.gpu_available

    return CloudNodeStatusReporter(
        scheduler_base_url=scheduler_base_url,
        cloud_node_id=CLOUD_NODE_ID,
        settings_provider=load_cloud_settings,
        queue_length_provider=lambda: cloud_runtime_state.snapshot().queue_length,
        health_provider=health_provider,
        model_runtime_provider=model_runtime_provider,
        last_activity_provider=lambda: cloud_runtime_state.snapshot().last_task_activity_ns,
    )


status_reporter = build_cloud_status_reporter()


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
    """Run global analysis for every known default-scenario subject."""
    provider = _scenario_provider(BEARING_SCENARIO_TYPE, GLOBAL_ANALYSIS)
    return run_periodic_global_analysis(
        database_path,
        scenario_type=provider.scenario_id,
        runtime_factory=lambda runtime_database_path: provider.build_runtime(
            runtime_database_path
        ),
        on_result=_create_suggested_model_updates,
    )


def _create_suggested_model_updates(analysis: dict[str, Any]) -> None:
    """Create review-only update tasks for model-update problem candidates."""

    analysis_id = analysis.get("analysis_id")
    candidates = analysis.get("problem_candidates")
    if not isinstance(analysis_id, str) or not isinstance(candidates, list):
        return
    service = None
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or candidate.get("suggested_action") != "model_update"
            or not isinstance(candidate.get("problem_id"), str)
        ):
            continue
        try:
            service = service or _model_update_service()
            service.create(
                {
                    "analysis_id": analysis_id,
                    "problem_id": candidate["problem_id"],
                },
                reuse_active=True,
                use_llm_suggestion=False,
            )
        except Exception as exc:
            LOGGER.exception(
                "automatic model-update suggestion creation failed for %s: %s",
                candidate["problem_id"],
                exc,
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
    await asyncio.to_thread(
        initialize_database,
        settings.database_path,
        storage_providers=(storage_provider,),
    )
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


def _edge_summary_repository() -> EdgeFeatureRepository:
    database_path = Path(
        os.getenv("CLOUD_SUMMARY_DATABASE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "cloud_summary.db"))
    )
    initialize_database(database_path)
    return EdgeFeatureRepository(database_path)


def _health_payload(settings: CloudSettings, status: str) -> dict[str, object]:
    runner = get_moment_runner(settings)
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
        "model_version": runner.model_version,
        "model_device": settings.moment_device,
    }


@app.get("/health", response_model=None)
def health() -> dict[str, object] | JSONResponse:
    settings = load_cloud_settings()
    if get_moment_runner(settings).loaded:
        return _health_payload(settings, "ok")
    return JSONResponse(
        status_code=503,
        content=_health_payload(settings, "unavailable"),
    )


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
        cloud_runtime_state.begin_inference()
        try:
            settings = load_cloud_settings()
            handler = get_scenario_handler(
                normalize_legacy_scenario_type(request.get("scenario_type")),
                database_path=settings.database_path,
            )
            return handler.infer(request)
        finally:
            cloud_runtime_state.finish_inference()
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


@app.post("/cloud/evaluate", response_model=None)
def cloud_evaluate(payload: Any = Body(default=None)) -> dict | JSONResponse:
    """State-free classifier endpoint used by run-scoped paired evaluation."""
    try:
        request = require_mapping(payload, "CloudEvaluationRequest")
        cloud_runtime_state.begin_inference()
        try:
            return evaluate_cloud_window(request, load_cloud_settings())
        finally:
            cloud_runtime_state.finish_inference()
    except ContractError as error:
        return JSONResponse(status_code=400, content=error_response(error))
    except CloudServiceError as error:
        contract_error = ContractError(error.code, error.message, None)
        return JSONResponse(
            status_code=error.status_code,
            content=error_response(contract_error),
        )
    except Exception as exc:
        error = ContractError("MODEL_INFER_FAILED", str(exc), None)
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


def _recent_limit(limit: int | str) -> int:
    if isinstance(limit, bool):
        raise ValueError("INVALID_RECENT_LIMIT")
    raw_limit = str(limit).strip()
    if not raw_limit.isdigit():
        raise ValueError("INVALID_RECENT_LIMIT")
    parsed_limit = int(raw_limit)
    if not 1 <= parsed_limit <= 200:
        raise ValueError("INVALID_RECENT_LIMIT")
    return parsed_limit


@app.get("/cloud/device-decision-results/recent", response_model=None)
def list_recent_device_decisions(
    device_id: str | None = None, limit: str = "50"
) -> dict | JSONResponse:
    try:
        parsed_limit = _recent_limit(limit)
    except ValueError:
        return JSONResponse(
            status_code=400, content={"error_code": "INVALID_RECENT_LIMIT"}
        )
    try:
        items = TaskResultService(
            load_cloud_settings().database_path
        ).list_recent_device_decisions(
            device_id.strip() if device_id and device_id.strip() else None,
            parsed_limit,
        )
        return {"success": True, "items": items, "count": len(items)}
    except (sqlite3.Error, json.JSONDecodeError):
        return JSONResponse(
            status_code=503, content={"error_code": "SERVICE_UNAVAILABLE"}
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
        adapted = adapt_summary_arbitration_request(payload)
        settings = load_cloud_settings()
        policy = _scenario_provider(
            normalize_legacy_scenario_type(adapted.get("scenario_type")),
            ARBITRATION_POLICY,
        )
        result = DeviceArbitrationService(
            settings.database_path,
            policy,
        ).arbitrate(adapted)
        return attach_summary_identity(result, adapted)
    except ArbitrationPayloadConflictError as error:
        return JSONResponse(
            status_code=409,
            content={"error_code": "ARBITRATION_IDENTITY_CONFLICT", "message": str(error)},
        )
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


@app.post("/cloud/summary-window-results", response_model=None)
def accept_summary_window(payload: dict) -> dict | JSONResponse:
    try:
        return SummaryWindowRepository(load_cloud_settings().database_path).accept(payload)
    except ArbitrationPayloadConflictError as error:
        return JSONResponse(
            status_code=409,
            content={"error_code": "SUMMARY_IDENTITY_CONFLICT", "message": str(error)},
        )
    except ValueError as error:
        return JSONResponse(
            status_code=400,
            content={"error_code": "INVALID_SUMMARY_WINDOW", "message": str(error)},
        )


@app.get("/cloud/summary-window-results/recent", response_model=None)
def list_recent_summary_windows(
    device_id: str | None = None, limit: int = 100
) -> dict:
    parsed_limit = min(max(int(limit), 1), 1000)
    return {
        "items": SummaryWindowRepository(
            load_cloud_settings().database_path
        ).list_recent(device_id=device_id, limit=parsed_limit)
    }


@app.get("/cloud/device-arbitration/recent", response_model=None)
def list_recent_device_arbitrations(
    device_id: str | None = None, limit: str = "50"
) -> dict | JSONResponse:
    try:
        parsed_limit = _recent_limit(limit)
    except ValueError:
        return JSONResponse(
            status_code=400, content={"error_code": "INVALID_RECENT_LIMIT"}
        )
    try:
        items = DeviceArbitrationRepository(
            load_cloud_settings().database_path
        ).list_recent(
            device_id.strip() if device_id and device_id.strip() else None,
            parsed_limit,
        )
        return {"success": True, "items": items, "count": len(items)}
    except (sqlite3.Error, json.JSONDecodeError):
        return JSONResponse(
            status_code=503, content={"error_code": "SERVICE_UNAVAILABLE"}
        )


@app.get("/cloud/device-arbitration/{conflict_id}", response_model=None)
def get_device_arbitration(conflict_id: str) -> dict | JSONResponse:
    try:
        policy = _scenario_provider(
            BEARING_SCENARIO_TYPE,
            ARBITRATION_POLICY,
        )
        result = DeviceArbitrationService(
            load_cloud_settings().database_path,
            policy,
        ).get(conflict_id)
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
        scenario_type = normalize_legacy_scenario_type(payload.get("scenario_type"))
        provider = _scenario_provider(scenario_type, GLOBAL_ANALYSIS)
        database_path = load_cloud_settings().database_path
        result = GlobalAnalysisService(
            database_path,
            runtime=provider.build_runtime(database_path),
        ).analyze(
            provider.scenario_id,
            payload.get("subject_id"),
            payload.get("task_limit", DEFAULT_TASK_LIMIT),
        )
        _create_suggested_model_updates(result)
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


@app.get("/cloud/global-analysis/recent", response_model=None)
def list_recent_global_analyses(
    scenario_type: str = DEFAULT_SCENARIO_TYPE,
    subject_id: str | None = None,
    limit: str = "50",
) -> dict | JSONResponse:
    try:
        parsed_limit = _recent_limit(limit)
    except ValueError:
        return JSONResponse(
            status_code=400, content={"error_code": "INVALID_RECENT_LIMIT"}
        )
    try:
        items = GlobalAnalysisService(
            load_cloud_settings().database_path
        ).repository.list_recent(
            scenario_type,
            subject_id.strip() if subject_id and subject_id.strip() else None,
            parsed_limit,
        )
        return {"success": True, "items": items, "count": len(items)}
    except (sqlite3.Error, json.JSONDecodeError):
        return JSONResponse(
            status_code=503, content={"error_code": "SERVICE_UNAVAILABLE"}
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


def _model_update_service():
    return _model_update_provider().build_service(load_cloud_settings())


def _model_update_provider():
    return _scenario_provider(BEARING_SCENARIO_TYPE, MODEL_UPDATE)


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


@app.post("/cloud/model-update/{update_id}/train", response_model=None)
def run_model_update_training(update_id: str) -> dict | JSONResponse:
    """Execute the persisted offline trainer and register its candidate."""
    try:
        return _model_update_service().run_training(update_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.get("/cloud/model-update/pending-distribution", response_model=None)
def list_pending_model_distribution(
    edge_node_id: str | None = None,
) -> dict | JSONResponse:
    """Edge poll target: approved candidates to pull and rollbacks to execute."""
    try:
        return _model_update_service().list_pending_distribution(
            edge_node_id=edge_node_id
        )
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.get("/cloud/model-update/recent", response_model=None)
def list_recent_model_updates(limit: int = 20) -> list:
    """Frontend overview: latest model updates with the LLM suggestion text."""

    if not isinstance(limit, int) or limit <= 0:
        limit = 20
    if limit > 100:
        limit = 100
    return _model_update_service().list_recent(limit)


@app.get("/cloud/model-update/{update_id}", response_model=None)
def get_model_update(update_id: str) -> dict | JSONResponse:
    try:
        return _model_update_service().get(update_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.post("/cloud/model-update/{update_id}/suggestion", response_model=None)
def generate_model_update_suggestion(update_id: str) -> dict | JSONResponse:
    try:
        return _model_update_service().generate_suggestion(update_id)
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
        return _model_update_service().handoff_distribution(
            update_id,
            local_cloud_activator=lambda artifact, version: _model_update_provider().activate_candidate(
                load_cloud_settings(), artifact, version
            ),
        )
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


@app.post("/cloud/model-update/{update_id}/rollback-result", response_model=None)
def record_model_update_rollback_result(
    update_id: str, payload: Any = Body(...)
) -> dict | JSONResponse:
    try:
        if not isinstance(payload, dict):
            raise ModelUpdateError("INVALID_ROLLBACK_RESULT")
        return _model_update_service().record_rollback_result(update_id, payload)
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
            update_id,
            executed_by=executed_by,
            local_cloud_activator=lambda version: _model_update_provider().activate_version(
                load_cloud_settings(), version
            ),
        )
    except ModelUpdateError as error:
        return _model_update_error_response(error)


@app.get("/cloud/model-update/{update_id}/file", response_model=None)
def download_model_update_artifact(update_id: str) -> Response | JSONResponse:
    """Serve the frozen candidate artifact for edge pull.

    A multi-file bundle is zipped with a manifest.json listing per-file
    sha256; a single-file artifact is streamed directly.
    """
    try:
        artifact = _model_update_service().get_download_artifact(update_id)
    except ModelUpdateError as error:
        return _model_update_error_response(error)
    artifact_path = Path(artifact["artifact_path"])
    if not artifact_path.exists():
        return JSONResponse({"error": "ARTIFACT_FILE_MISSING"}, status_code=404)
    bundle = artifact.get("artifact_bundle")
    if isinstance(bundle, dict) and bundle.get("entries") and artifact_path.is_dir():
        manifest = {
            "version": artifact["candidate_version"],
            "model_type": artifact["model_type"],
            "model_family": artifact.get("model_family", "edge"),
            "feature_pipeline_version": artifact["feature_pipeline_version"],
            "files": {
                entry["rel_path"]: entry["sha256"]
                for entry in bundle["entries"]
            },
        }
        private_key_path = os.getenv("CLOUD_MODEL_SIGNING_PRIVATE_KEY_FILE", "").strip()
        key_id = os.getenv("MODEL_UPDATE_SIGNING_KEY_ID", "release-v1").strip()
        try:
            manifest = sign_manifest(
                manifest,
                private_key_path=private_key_path,
                key_id=key_id,
            )
        except ModelSigningError:
            LOGGER.exception("model bundle signing failed for update_id=%s", update_id)
            return JSONResponse(
                {"error_code": "MODEL_SIGNING_UNAVAILABLE"}, status_code=503
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in bundle["entries"]:
                archive.write(
                    artifact_path / entry["rel_path"], arcname=entry["rel_path"]
                )
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            )
        filename = f"{artifact['candidate_version']}.zip"
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return FileResponse(
        artifact_path,
        media_type="application/octet-stream",
        filename=Path(artifact_path).name,
    )


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
