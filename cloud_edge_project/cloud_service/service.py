"""Independent, structured cloud packet review."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import time
from math import isfinite
from pathlib import Path
import threading
from typing import Any, Callable

from core.bearing_actions import grade_for_action
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.moment_light_adapt import (
    MODEL_VERSION,
    MomentLightAdaptRunner,
    MomentReviewPolicy,
)
from cloud_service.model_update.model_types import ActiveModelVersionStore
from cloud_service.model_update.repository import ModelUpdateRepository
from cloud_service.moment_review_repository import MomentReviewRepository
from cloud_service.packet_diagnosis import (
    DiagnosisModel,
    PacketDiagnosis,
)
from cloud_service.perception.pipeline import run_single_packet_perception
from cloud_service.storage.persistence import CloudReviewPersistence


_moment_runner: MomentLightAdaptRunner | None = None
_moment_runner_settings: CloudSettings | None = None
_moment_runner_lock = threading.Lock()


def get_moment_runner(
    settings: CloudSettings,
    *,
    runner_factory: Callable[..., MomentLightAdaptRunner] = MomentLightAdaptRunner,
) -> MomentLightAdaptRunner:
    """Return the process-wide runner for the current MOMENT configuration."""

    global _moment_runner, _moment_runner_settings
    with _moment_runner_lock:
        if _moment_runner is None or _moment_runner_settings != settings:
            runtime_settings, model_version = _active_moment_runtime(settings)
            _moment_runner = runner_factory(
                runtime_settings, model_version=model_version
            )
            _moment_runner_settings = settings
        return _moment_runner


def activate_moment_candidate(
    settings: CloudSettings,
    artifact: dict[str, Any],
    candidate_version: str,
    *,
    runner_factory: Callable[..., MomentLightAdaptRunner] = MomentLightAdaptRunner,
) -> MomentLightAdaptRunner:
    """Load a candidate completely, then atomically replace the active runner."""

    runtime_settings = _candidate_runtime_settings(settings, artifact)
    return _load_and_swap_moment_runner(
        settings,
        runtime_settings,
        candidate_version,
        runner_factory=runner_factory,
    )


def activate_moment_version(
    settings: CloudSettings,
    model_version: str,
    *,
    runner_factory: Callable[..., MomentLightAdaptRunner] = MomentLightAdaptRunner,
) -> MomentLightAdaptRunner:
    """Load a baseline or deployed candidate version, then replace the runner."""

    if model_version == MODEL_VERSION:
        runtime_settings = settings
    else:
        task = ModelUpdateRepository(settings.database_path).find_runtime_candidate(
            "moment_light_adapt", model_version
        )
        artifact = task.get("candidate_artifact") if task else None
        if not isinstance(artifact, dict):
            raise ValueError("MOMENT_RUNTIME_VERSION_NOT_FOUND")
        runtime_settings = _candidate_runtime_settings(settings, artifact)
    return _load_and_swap_moment_runner(
        settings,
        runtime_settings,
        model_version,
        runner_factory=runner_factory,
    )


def _load_and_swap_moment_runner(
    base_settings: CloudSettings,
    runtime_settings: CloudSettings,
    model_version: str,
    *,
    runner_factory: Callable[..., MomentLightAdaptRunner],
) -> MomentLightAdaptRunner:
    candidate = runner_factory(runtime_settings, model_version=model_version)
    candidate.load()
    global _moment_runner, _moment_runner_settings
    with _moment_runner_lock:
        _moment_runner = candidate
        _moment_runner_settings = base_settings
    return candidate


def _active_moment_runtime(
    settings: CloudSettings,
) -> tuple[CloudSettings, str]:
    active_version = ActiveModelVersionStore(settings.database_path).get(
        "moment_light_adapt"
    )
    if not active_version:
        return settings, MODEL_VERSION
    task = ModelUpdateRepository(settings.database_path).find_runtime_candidate(
        "moment_light_adapt", active_version
    )
    artifact = task.get("candidate_artifact") if task else None
    if not isinstance(artifact, dict):
        return settings, MODEL_VERSION
    return _candidate_runtime_settings(settings, artifact), active_version


def _candidate_runtime_settings(
    settings: CloudSettings, artifact: dict[str, Any]
) -> CloudSettings:
    artifact_path = Path(str(artifact.get("artifact_path", ""))).resolve()
    checkpoint_path = artifact_path / "best_model.pt"
    condition_norm_path = artifact_path / "condition_norm.json"
    if not checkpoint_path.is_file() or not condition_norm_path.is_file():
        raise ValueError("MOMENT_CANDIDATE_BUNDLE_INCOMPLETE")
    bundle = artifact.get("artifact_bundle")
    if isinstance(bundle, dict):
        entries = bundle.get("entries")
        if not isinstance(entries, list):
            raise ValueError("MOMENT_CANDIDATE_BUNDLE_INCOMPLETE")
        expected_hashes = {
            entry.get("rel_path"): entry.get("sha256")
            for entry in entries
            if isinstance(entry, dict)
        }
        for rel_path, path in (
            ("best_model.pt", checkpoint_path),
            ("condition_norm.json", condition_norm_path),
        ):
            expected_sha256 = expected_hashes.get(rel_path)
            if not isinstance(expected_sha256, str):
                raise ValueError("MOMENT_CANDIDATE_BUNDLE_INCOMPLETE")
            if expected_sha256.lower() != _sha256(path):
                raise ValueError("MOMENT_CANDIDATE_CHECKSUM_MISMATCH")
    else:
        expected_sha256 = artifact.get("artifact_sha256")
        if not isinstance(expected_sha256, str):
            raise ValueError("MOMENT_CANDIDATE_BUNDLE_INCOMPLETE")
        if expected_sha256.lower() != _sha256(checkpoint_path):
            raise ValueError("MOMENT_CANDIDATE_CHECKSUM_MISMATCH")
    return replace(
        settings,
        moment_checkpoint_path=checkpoint_path,
        moment_condition_norm_path=condition_norm_path,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preload_moment_runner(
    settings: CloudSettings,
    *,
    runner_factory: Callable[..., MomentLightAdaptRunner] = MomentLightAdaptRunner,
) -> MomentLightAdaptRunner:
    """Load the cached runner during service startup."""

    runner = get_moment_runner(settings, runner_factory=runner_factory)
    runner.load()
    return runner


def infer_cloud(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
    *,
    diagnosis_model: DiagnosisModel | None = None,
) -> dict[str, Any]:
    """Review one high-rate packet without requesting or aggregating context."""

    if request.get("schema_version") == "cloud-infer/2.0":
        return _infer_v12(request, settings, diagnosis_model=diagnosis_model)

    return _infer_packet(request, settings, diagnosis_model=diagnosis_model)


def _infer_packet(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
    *,
    diagnosis_model: DiagnosisModel | None = None,
) -> dict[str, Any]:
    selected = settings or load_cloud_settings()
    perception_result = run_single_packet_perception(request)
    if not perception_result["data_quality"]["valid"]:
        return {
            "success": True,
            "perception_result": perception_result,
            "cloud_packet_result": None,
            "review_id": None,
        }

    review_id = CloudReviewPersistence(selected.database_path).persist_packet(
        request, perception_result
    )
    if diagnosis_model is None:
        raise CloudServiceError(
            "INVALID_CLOUD_BACKEND",
            f"unsupported cloud backend: {selected.backend}",
            500,
        )
    model = diagnosis_model
    diagnosis = model.predict(perception_result["cloud_recomputed_features"])
    model_version = model.model_version
    packet_result = _cloud_packet_result(
        review_id, request, diagnosis, model_version
    )
    return {
        "success": True,
        "perception_result": perception_result,
        "cloud_packet_result": packet_result,
        "review_result": packet_result,
        "review_id": review_id,
    }


def _infer_v12(
    request: dict[str, Any],
    settings: CloudSettings | None,
    *,
    diagnosis_model: DiagnosisModel | None,
) -> dict[str, Any]:
    window = _validate_v12_request(request)
    selected = settings or load_cloud_settings()
    if selected.backend == "moment_light_adapt":
        return _infer_v12_moment(request, window, selected)
    legacy_request = _legacy_packet_request(request, window)
    response = _infer_packet(legacy_request, selected, diagnosis_model=diagnosis_model)
    packet_result = response.get("cloud_packet_result")
    if not isinstance(packet_result, dict):
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window did not produce a bearing result", 422)
    response["cloud_packet_result"] = _cloud_bearing_result(
        response["review_id"], window, packet_result
    )
    response["review_result"] = response["cloud_packet_result"]
    return response


def _infer_v12_moment(
    request: dict[str, Any],
    window: dict[str, Any],
    settings: CloudSettings,
) -> dict[str, Any]:
    vibration = _moment_vibration(window)
    edge = request["edge_perception_result"]
    operating_context = edge["features"]["operating_context"]
    prediction = get_moment_runner(settings).predict(vibration, operating_context)
    bearing_state, risk_level, recommended_action = MomentReviewPolicy().decide(
        prediction.label
    )
    review_id = f"moment_{request['diagnosis_window_id']}"
    result = {
        "schema_version": "cloud-bearing-result/2.0",
        "result_id": f"cloud_{request['diagnosis_window_id']}",
        "review_id": review_id,
        "device_id": window["device_id"],
        "task_id": window["task_id"],
        "bearing_id": window["bearing_id"],
        "sender_id": window["sender_id"],
        "decision_round_id": request["decision_round_id"],
        "diagnosis_window_id": request["diagnosis_window_id"],
        "window_start_sequence": window["window_start_sequence"],
        "window_end_sequence": window["window_end_sequence"],
        "window_start_ns": window["window_start_ns"],
        "window_end_ns": window["window_end_ns"],
        "bearing_state": bearing_state,
        "edge_label": _edge_result_label(edge),
        "confidence": prediction.confidence,
        "data_quality_score": 1.0,
        "risk_level": risk_level,
        "action_grade": grade_for_action(recommended_action),
        "recommended_action": recommended_action,
        "model_version": prediction.model_version,
        "created_at_ns": time.time_ns(),
    }
    MomentReviewRepository(settings.database_path).save(result)
    return {
        "success": True,
        "review_id": review_id,
        "cloud_packet_result": result,
        "review_result": result,
    }


def _edge_result_label(edge: dict[str, Any]) -> str | None:
    inference = edge.get("edge_inference") or {}
    return inference.get("edge_result") or inference.get("label")


def _moment_vibration(window: dict[str, Any]) -> dict[str, Any]:
    if (
        len(window["contributing_packet_ids"]) != 1
        or window["sample_rate_hz"] != 64_000
        or window["sample_count"] != 3_200
    ):
        raise CloudServiceError(
            "INVALID_CLOUD_WINDOW",
            "MOMENT LIGHT_ADAPT requires one 50ms 64kHz window with 3200 samples",
            400,
        )
    vibration = window["data"].get("vibration")
    if not isinstance(vibration, dict):
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "vibration signal is required", 400)
    values = vibration.get("values")
    if (
        vibration.get("sample_rate_hz") != 64_000
        or vibration.get("sample_count") != 3_200
        or not isinstance(values, list)
        or len(values) != 3_200
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isfinite(value)
            for value in values
        )
    ):
        raise CloudServiceError(
            "INVALID_CLOUD_WINDOW",
            "vibration signal does not match the MOMENT 50ms input contract",
            400,
        )
    return vibration


def _cloud_packet_result(
    review_id: str,
    request: dict[str, Any],
    diagnosis: PacketDiagnosis,
    model_version: str,
) -> dict[str, Any]:
    edge = request["edge_perception_result"]
    inference = edge.get("edge_inference") or {}
    edge_label = _edge_result_label(edge)
    return {
        "review_id": review_id,
        "device_id": edge["device_id"],
        "task_id": edge["task_id"],
        "bearing_id": edge["bearing_id"],
        "packet_id": edge["packet_id"],
        "edge_label": edge_label,
        "edge_confidence": inference.get("confidence"),
        "edge_model_version": edge.get("edge_model_version"),
        "cloud_label": diagnosis.label,
        "cloud_confidence": diagnosis.confidence,
        "cloud_model_version": model_version,
        "risk_level": diagnosis.risk_level,
        "recommended_action": diagnosis.recommended_action,
        "created_at_ns": time.time_ns(),
    }


def _validate_v12_request(request: dict[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "decision_round_id", "diagnosis_window_id", "edge_perception_result", "cloud_raw_window"}
    if set(request) != required:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud-infer/2.0 fields do not match the shared contract", 400)
    window = request["cloud_raw_window"]
    required_window = {
        "device_id", "task_id", "bearing_id", "sender_id", "window_start_sequence",
        "window_end_sequence", "window_start_ns", "window_end_ns", "contributing_packet_ids",
        "sample_rate_hz", "sample_count", "data",
    }
    if not isinstance(window, dict) or set(window) != required_window:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud_raw_window fields do not match the shared contract", 400)
    for field in ("device_id", "task_id", "bearing_id", "sender_id"):
        if not isinstance(window[field], str) or not window[field].strip():
            raise CloudServiceError("INVALID_CLOUD_WINDOW", f"cloud_raw_window.{field} is invalid", 400)
    start, end = window["window_start_sequence"], window["window_end_sequence"]
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window sequence range is invalid", 400)
    for field in ("window_start_ns", "window_end_ns", "sample_rate_hz", "sample_count"):
        value = window[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if field == "window_start_ns" else 1):
            raise CloudServiceError("INVALID_CLOUD_WINDOW", f"cloud_raw_window.{field} is invalid", 400)
    if not isinstance(window["data"], dict):
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud_raw_window.data is invalid", 400)
    if not isinstance(window["contributing_packet_ids"], list) or not window["contributing_packet_ids"] or not all(isinstance(value, str) and value for value in window["contributing_packet_ids"]):
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window manifest is invalid", 400)
    if len(window["contributing_packet_ids"]) != end - start + 1:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window manifest does not match its sequence range", 400)
    packet_count = end - start + 1
    if packet_count not in {1, 2, 3}:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window must be 50, 100, or 150ms", 400)
    if window["window_end_ns"] - window["window_start_ns"] != packet_count * 50_000_000:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window duration is inconsistent", 400)
    if window["sample_count"] != window["sample_rate_hz"] * packet_count // 20:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window sample count is inconsistent", 400)
    expected_round = build_decision_round_id(device_id=window["device_id"], task_id=window["task_id"], window_start_sequence=start, window_end_sequence=end)
    expected_window = build_diagnosis_window_id(device_id=window["device_id"], task_id=window["task_id"], bearing_id=window["bearing_id"], sender_id=window["sender_id"], window_start_sequence=start, window_end_sequence=end)
    if request["decision_round_id"] != expected_round or request["diagnosis_window_id"] != expected_window:
        raise CloudServiceError("INVALID_CLOUD_WINDOW", "cloud raw window identity is inconsistent", 400)
    return window


def _legacy_packet_request(request: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    packet_id = window["contributing_packet_ids"][-1]
    raw = {
        **window,
        "packet_id": packet_id,
        "sequence_number": window["window_end_sequence"],
        "start_timestamp_ns": window["window_start_ns"],
        "end_timestamp_ns": window["window_end_ns"],
        "end_generate_timestamp_ns": window["window_end_ns"],
        "window_packet_count": window["window_end_sequence"] - window["window_start_sequence"] + 1,
    }
    edge = dict(request["edge_perception_result"])
    edge.update({
        "device_id": window["device_id"], "task_id": window["task_id"],
        "bearing_id": window["bearing_id"], "sender_id": window["sender_id"],
        "packet_id": packet_id, "sequence_number": window["window_end_sequence"],
        "end_generate_timestamp_ns": window["window_end_ns"],
    })
    return {"edge_perception_result": edge, "cloud_raw_packet": raw}


def _cloud_bearing_result(review_id: str, window: dict[str, Any], packet_result: dict[str, Any]) -> dict[str, Any]:
    action = _v12_action(packet_result["recommended_action"], packet_result["cloud_label"])
    return {
        "schema_version": "cloud-bearing-result/2.0",
        "result_id": f"cloud_{window['diagnosis_window_id'] if 'diagnosis_window_id' in window else review_id}",
        "review_id": review_id,
        "device_id": window["device_id"], "task_id": window["task_id"],
        "bearing_id": window["bearing_id"], "sender_id": window["sender_id"],
        "decision_round_id": build_decision_round_id(device_id=window["device_id"], task_id=window["task_id"], window_start_sequence=window["window_start_sequence"], window_end_sequence=window["window_end_sequence"]),
        "diagnosis_window_id": build_diagnosis_window_id(device_id=window["device_id"], task_id=window["task_id"], bearing_id=window["bearing_id"], sender_id=window["sender_id"], window_start_sequence=window["window_start_sequence"], window_end_sequence=window["window_end_sequence"]),
        "window_start_sequence": window["window_start_sequence"], "window_end_sequence": window["window_end_sequence"],
        "window_start_ns": window["window_start_ns"], "window_end_ns": window["window_end_ns"],
        "bearing_state": packet_result["cloud_label"],
        "confidence": packet_result["cloud_confidence"], "data_quality_score": 1.0,
        "risk_level": packet_result["risk_level"], "action_grade": grade_for_action(action),
        "recommended_action": action, "model_version": packet_result["cloud_model_version"],
        "created_at_ns": time.time_ns(),
    }


def _v12_action(legacy_action: str, label: str) -> str:
    """Map the retained packet-review vocabulary onto the shared V1.2 action enum."""
    mapping = {
        "record_only": "continue_operation",
        "urgent_bearing_attention": "urgent_intervention",
    }
    if legacy_action in mapping:
        return mapping[legacy_action]
    if legacy_action in {
        "continue_operation", "enhanced_monitoring", "scheduled_inspection",
        "urgent_intervention", "shutdown",
    }:
        return legacy_action
    return "urgent_intervention" if label == "fault" else "enhanced_monitoring"
