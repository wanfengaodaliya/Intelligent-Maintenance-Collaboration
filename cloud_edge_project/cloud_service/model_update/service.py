"""Cloud-only service for update task lifecycle, validation and preparation."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cloud_service.model_update.contracts import CREATABLE_UPDATE_TYPES, VALIDATION_STATES
from cloud_service.model_update.decision import decide_update
from cloud_service.model_update.distributor import prepare_distribution
from cloud_service.model_update.repository import ModelUpdateRepository
from cloud_service.model_update.validator import validate_samples
from scenarios.bearing.cloud.model_update.candidate_runner import (
    CandidateInputIncompatible,
    InvalidCandidate,
    load_candidate,
)
from scenarios.bearing.cloud.model_update.data_loader import load_validation_samples


class ModelUpdateError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class ModelUpdateService:
    def __init__(
        self,
        database_path: Path,
        *,
        data_root: Path | None = None,
        download_base_url: str = "http://127.0.0.1:8004",
    ):
        self.database_path = Path(database_path)
        self.repository = ModelUpdateRepository(self.database_path)
        self.data_root = (data_root or Path(__file__).resolve().parents[2] / "data" / "model_updates").resolve()
        self.download_base_url = download_base_url

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ModelUpdateError("INVALID_UPDATE_REQUEST")
        analysis_id = _required_string(request, "analysis_id")
        analysis = self.repository.get_analysis(analysis_id)
        if analysis is None:
            raise ModelUpdateError("GLOBAL_ANALYSIS_NOT_FOUND")
        decision = decide_update(analysis)
        if decision == "observe":
            return {"decision": decision, "update": None}
        update_type = _required_string(request, "update_type")
        if update_type not in CREATABLE_UPDATE_TYPES:
            raise ModelUpdateError("INVALID_UPDATE_REQUEST")
        candidate_path = self._registered_candidate_path(_required_string(request, "update_file"))
        try:
            load_candidate(candidate_path)
        except InvalidCandidate as error:
            raise ModelUpdateError("INVALID_UPDATE_FILE", str(error)) from error
        targets = request.get("target_edge_nodes", [])
        if not isinstance(targets, list) or not all(isinstance(node, str) and node for node in targets):
            raise ModelUpdateError("INVALID_UPDATE_REQUEST")
        limit = request.get("test_data_limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ModelUpdateError("INVALID_UPDATE_REQUEST")
        now = time.time_ns()
        task = {
            "update_id": f"update_{uuid4().hex}",
            "analysis_id": analysis_id,
            "scenario_type": analysis["scenario_type"],
            "subject_id": analysis["subject_id"],
            "update_type": update_type,
            "update_reason": request.get("update_reason", "global_analysis_recommendation"),
            "old_version": _required_string(request, "old_version"),
            "new_version": _required_string(request, "new_version"),
            "update_file": str(candidate_path),
            "update_file_sha256": _sha256(candidate_path),
            "target_edge_nodes": targets,
            "test_data_limit": limit,
            "status": "created",
            "created_at_ns": now,
            "updated_at_ns": now,
        }
        return {"decision": decision, "update": self.repository.create(task)}

    def get(self, update_id: str) -> dict[str, Any]:
        return self._task(update_id)

    def validate(self, update_id: str, *, use_demo_data: bool = False) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, VALIDATION_STATES)
        try:
            candidate_path = self._unchanged_candidate(task)
            candidate = load_candidate(candidate_path)
            samples = load_validation_samples(
                self.database_path,
                task,
                use_demo_data=use_demo_data,
                demo_path=self.data_root / "demo" / "validation_samples.json",
            )
            result = validate_samples(candidate, task, samples)
        except ModelUpdateError:
            raise
        except CandidateInputIncompatible as error:
            return self._validation_failed(task, "MODEL_INPUT_INCOMPATIBLE", str(error))
        except (InvalidCandidate, OSError, ValueError) as error:
            return self._validation_failed(task, "CANDIDATE_RUN_FAILED", str(error))
        status = "waiting_validation_data" if result["test_count"] == 0 else "waiting_confirmation"
        return self.repository.update(
            update_id,
            status=status,
            validation_result_json=result,
            updated_at_ns=time.time_ns(),
        )

    def approve(self, update_id: str, *, confirmed_by: str | None = None) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, {"waiting_confirmation"})
        return self.repository.update(
            update_id,
            status="approved",
            confirmation_json={"action": "approved", "confirmed_by": confirmed_by, "confirmed_at_ns": time.time_ns()},
            updated_at_ns=time.time_ns(),
        )

    def reject(self, update_id: str, *, confirmed_by: str | None = None) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, {"waiting_confirmation"})
        return self.repository.update(
            update_id,
            status="rejected",
            confirmation_json={"action": "rejected", "confirmed_by": confirmed_by, "confirmed_at_ns": time.time_ns()},
            updated_at_ns=time.time_ns(),
        )

    def distribute(self, update_id: str) -> dict[str, Any]:
        task = self._task(update_id)
        if task["status"] == "distribution_prepared":
            return task
        self._require_state(task, {"approved"})
        self._unchanged_candidate(task)
        result = prepare_distribution(task, self.download_base_url)
        return self.repository.update(
            update_id,
            status="distribution_prepared",
            distribution_result_json=result,
            updated_at_ns=time.time_ns(),
        )

    def download_path(self, update_id: str) -> Path:
        return self._unchanged_candidate(self._task(update_id))

    def _validation_failed(self, task: dict[str, Any], code: str, message: str) -> dict[str, Any]:
        return self.repository.update(
            task["update_id"],
            status="validation_failed",
            validation_result_json={"error_code": code, "message": message},
            updated_at_ns=time.time_ns(),
        )

    def _task(self, update_id: str) -> dict[str, Any]:
        task = self.repository.get(update_id)
        if task is None:
            raise ModelUpdateError("UPDATE_NOT_FOUND")
        return task

    def _require_state(self, task: dict[str, Any], allowed: set[str]) -> None:
        if task["status"] not in allowed:
            raise ModelUpdateError("INVALID_UPDATE_STATE")

    def _registered_candidate_path(self, value: str) -> Path:
        supplied = Path(value)
        path = supplied.resolve() if supplied.is_absolute() else (self.data_root / supplied).resolve()
        if self.data_root not in path.parents or not path.is_file():
            raise ModelUpdateError("UPDATE_FILE_NOT_FOUND")
        return path

    def _unchanged_candidate(self, task: dict[str, Any]) -> Path:
        path = Path(task["update_file"])
        if not path.is_file():
            raise ModelUpdateError("UPDATE_FILE_NOT_FOUND")
        if _sha256(path) != task["update_file_sha256"]:
            raise ModelUpdateError("UPDATE_FILE_CHANGED")
        return path


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelUpdateError("INVALID_UPDATE_REQUEST")
    return value.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
