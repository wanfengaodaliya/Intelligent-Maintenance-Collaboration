"""Application service for the human-controlled cloud model-update lifecycle."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from cloud_service.model_update.approval import ApprovalError, approve_candidate
from cloud_service.model_update.candidate_registry import CandidateRegistry
from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.model_update.contracts import (
    CONFIRMATION_STATES,
    DATA_PREPARATION_STATES,
    DEFAULT_CONFIG,
    DISTRIBUTION_HANDOFF_STATES,
    ModelUpdateConfig,
    TRAINING_RESULT_STATES,
    VALIDATION_STATES,
)
from cloud_service.model_update.dataset_builder import DatasetBuilder
from cloud_service.model_update.dataset_repository import (
    DatasetManifestRepository,
    LabelConfirmationRepository,
    PacketSourceRepository,
)
from cloud_service.model_update.decision import decide_update
from cloud_service.model_update.distribution_client import build_distribution_request
from cloud_service.model_update.label_confirmation import (
    LabelConfirmationProvider,
    SnapshotLabelProvider,
)
from cloud_service.model_update.model_types import (
    ActiveModelVersionStore,
    MODEL_TYPE_SPECS,
    validate_model_type,
)
from cloud_service.model_update.post_validator import (
    select_post_validation_metrics,
    validate_post_deployment,
)
from cloud_service.model_update.repository import ModelUpdateRepository
from cloud_service.model_update.suggestion import generate_suggestion as _generate_suggestion
from cloud_service.model_update.trainer import build_training_plan
from cloud_service.model_update.training import OfflineTrainingRunner
from cloud_service.model_update.validator import validate_candidate


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
        packet_source_database_path: Path | None = None,
        config: ModelUpdateConfig = DEFAULT_CONFIG,
        training_data_source: Any | None = None,
        label_provider: LabelConfirmationProvider | None = None,
        trainer: OfflineTrainingRunner | None = None,
        settings: CloudSettings | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.settings = settings or load_cloud_settings()
        self.data_root = (
            data_root
            or Path(__file__).resolve().parents[1] / "data" / "model_updates"
        ).resolve()
        self.config = config
        self.repository = ModelUpdateRepository(self.database_path)
        self.dataset_repository = DatasetManifestRepository(self.database_path)
        self.label_repository = LabelConfirmationRepository(self.database_path)
        self.source_repository = PacketSourceRepository(
            packet_source_database_path or self.database_path
        )
        self.training_data_source = training_data_source
        self.label_provider = label_provider
        self.trainer = trainer
        self.dataset_builder = DatasetBuilder(config)
        self.candidate_registry = CandidateRegistry(self.data_root)

    def _active_versions(self) -> ActiveModelVersionStore:
        return ActiveModelVersionStore(self.database_path)

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ModelUpdateError("INVALID_UPDATE_REQUEST")
        analysis_id = _required_string(request, "analysis_id")
        problem_id = _required_string(request, "problem_id")
        model_type = request.get("model_type") or "distilled_h5"
        try:
            model_type = validate_model_type(model_type)
        except ValueError as error:
            raise ModelUpdateError(str(error)) from error
        baseline_version = request.get("baseline_version")
        if not isinstance(baseline_version, str) or not baseline_version.strip():
            baseline_version = self._active_versions().get(model_type) or MODEL_TYPE_SPECS[
                model_type
            ].default_version
        analysis = self.repository.get_analysis(analysis_id)
        if analysis is None:
            raise ModelUpdateError("GLOBAL_ANALYSIS_NOT_FOUND")
        result = analysis["result"]
        candidates = result.get("problem_candidates")
        problem = next(
            (
                candidate for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("problem_id") == problem_id
            ),
            None,
        ) if isinstance(candidates, list) else None
        if problem is None:
            raise ModelUpdateError("PROBLEM_CANDIDATE_NOT_FOUND")
        decision = decide_update(problem, self.config)
        if decision == "observe":
            return {"decision": "observe", "update": None}
        existing = self.repository.find_by_analysis_problem(analysis_id, problem_id)
        if existing is not None:
            return {"decision": "create_update", "update": existing}
        now = time.time_ns()
        task = {
            "update_id": f"update_{uuid4().hex}",
            "analysis_id": analysis_id,
            "problem_id": problem_id,
            "scenario_type": analysis["scenario_type"],
            "subject_id": analysis["subject_id"],
            "problem_type": problem["problem_type"],
            "model_type": model_type,
            "problem_context_json": problem.get("problem_context", {}),
            "evidence_snapshot_json": problem.get("evidence", {}),
            "baseline_version": baseline_version,
            "status": "created",
            "created_at_ns": now,
            "updated_at_ns": now,
        }
        created = self.repository.create(task)
        try:
            self.generate_suggestion(created["update_id"])
        except Exception:
            pass
        return {"decision": decision, "update": self.repository.get(created["update_id"])}

    def generate_suggestion(self, update_id: str) -> dict[str, Any]:
        """Generate (or regenerate) the human-facing LLM suggestion for a task.

        The suggestion is a sidecar artifact for frontend review only; it never
        feeds back into data preparation or training. Falls back to a template
        when the LLM is unavailable.
        """

        task = self._task(update_id)
        text, source = _generate_suggestion(task, self.settings)
        return self.repository.update(
            update_id,
            suggestion_json={
                "text": text,
                "source": source,
                "generated_at_ns": time.time_ns(),
            },
            updated_at_ns=time.time_ns(),
        )

    def get(self, update_id: str) -> dict[str, Any]:
        return self._task(update_id)

    def get_download_artifact(self, update_id: str) -> dict[str, Any]:
        """Return the frozen candidate artifact descriptor for edge pull.

        The artifact is registered during training-result handoff and stays
        frozen for download once approved; edge nodes pull it to activate a
        new version locally.
        """

        task = self._task(update_id)
        artifact = task.get("candidate_artifact")
        if not isinstance(artifact, dict) or not artifact.get("artifact_path"):
            raise ModelUpdateError("CANDIDATE_ARTIFACT_NOT_FOUND")
        if task["status"] not in {
            "trained",
            "waiting_confirmation",
            "approved",
            "handoff_to_distribution",
            "distribution_in_progress",
            "distribution_succeeded",
            "verifying",
            "ineffective",
            "partial_improvement",
            "succeeded",
        }:
            raise ModelUpdateError("ARTIFACT_NOT_READY")
        return artifact

    def list_pending_distribution(
        self, edge_node_id: str | None = None
    ) -> dict[str, Any]:
        """Return edge-family updates awaiting pull and requested rollbacks.

        Edge nodes poll this to discover approved candidates to activate and
        baseline versions to roll back to. ``edge_node_id`` filters by the
        distribution target's explicit node list when one is present.
        """

        pulls: list[dict[str, Any]] = []
        for task in self.repository.list_pending_distribution():
            distribution = task.get("distribution_result") or {}
            target = distribution.get("target") or {}
            if target.get("family") != "edge":
                continue
            node_ids = target.get("edge_node_ids") or []
            if node_ids and edge_node_id not in node_ids:
                continue
            artifact = task.get("candidate_artifact") or {}
            pulls.append(
                {
                    "update_id": task["update_id"],
                    "model_type": task["model_type"],
                    "baseline_version": task.get("baseline_version"),
                    "candidate_version": artifact.get("candidate_version"),
                    "artifact_sha256": artifact.get("artifact_sha256"),
                    "target": target,
                }
            )
        rollbacks: list[dict[str, Any]] = []
        for task in self.repository.list_pending_rollback():
            spec = MODEL_TYPE_SPECS.get(task["model_type"])
            if spec is None or spec.family != "edge":
                continue
            rollbacks.append(
                {
                    "update_id": task["update_id"],
                    "model_type": task["model_type"],
                    "rollback_target_version": task.get("rollback_target_version"),
                }
            )
        return {
            "edge_node_id": edge_node_id,
            "pending_pull_count": len(pulls),
            "pending_rollback_count": len(rollbacks),
            "pending_pulls": pulls,
            "pending_rollbacks": rollbacks,
        }

    def list_pending_human_confirmation(self, update_id: str) -> dict[str, Any]:
        """List samples that still lack an authoritative label.

        A sample needs human verification when it has no human_confirmed and no
        dataset_ground_truth label, i.e. it would otherwise fall back to the
        low-priority cloud_reference during dataset construction.
        """

        task = self._task(update_id)
        if self.training_data_source is None or self.label_provider is None:
            raise ModelUpdateError("MODEL_UPDATE_SCENARIO_NOT_CONFIGURED")
        samples = self.training_data_source.load(task)
        pending: list[dict[str, Any]] = []
        for sample in samples:
            if self._needs_human_confirmation(sample):
                pending.append(self._pending_item(sample))
        return {
            "update_id": update_id,
            "pending_count": len(pending),
            "items": pending,
        }

    def _needs_human_confirmation(self, sample: dict[str, Any]) -> bool:
        provider = self.label_provider
        resolver = getattr(provider, "confirm_sources", None)
        if resolver is not None:
            sources = resolver(sample)
            return not (
                "human_confirmed" in sources or "dataset_ground_truth" in sources
            )
        confirmation = provider.confirm(sample)
        return not (
            isinstance(confirmation, dict)
            and confirmation.get("label_source")
            in {"human_confirmed", "dataset_ground_truth"}
        )

    @staticmethod
    def _pending_item(sample: dict[str, Any]) -> dict[str, Any]:
        edge = sample.get("historical_edge_result") or {}
        return {
            "packet_id": sample["packet_id"],
            "task_id": sample.get("task_id"),
            "source_file": sample.get("source_file"),
            "edge_label": edge.get("label"),
            "edge_risk_level": edge.get("risk_level"),
            "edge_model_version": edge.get("version"),
            "cloud_label": sample.get("cloud_label"),
            "is_cloud_reviewed": sample.get("is_cloud_reviewed", False),
            "sample_pools": sample.get("sample_pools", []),
        }

    def prepare_data(
        self, update_id: str, *, feature_pipeline_version: str = "edge_feature_v1"
    ) -> dict[str, Any]:
        task = self._task(update_id)
        existing = self.dataset_repository.get_by_update(update_id)
        if existing is not None:
            if task["status"] in DATA_PREPARATION_STATES | {"waiting_training"}:
                return self.repository.update(
                    update_id,
                    training_dataset_id=existing["dataset_id"],
                    status="waiting_training",
                    updated_at_ns=time.time_ns(),
                )
            return task
        self._require_state(task, DATA_PREPARATION_STATES)
        if self.training_data_source is None or self.label_provider is None:
            raise ModelUpdateError("MODEL_UPDATE_SCENARIO_NOT_CONFIGURED")
        self.repository.update(
            update_id, status="data_preparing", updated_at_ns=time.time_ns()
        )
        try:
            samples = self.training_data_source.load(task)
            snapshot_provider = SnapshotLabelProvider(self.label_provider)
            manifest = self.dataset_builder.build(
                update=task,
                samples=samples,
                label_provider=snapshot_provider,
                feature_pipeline_version=feature_pipeline_version,
            )
            for sample in samples:
                confirmation = snapshot_provider.confirm(sample)
                if confirmation is not None:
                    self.label_repository.save(confirmation)
            self.dataset_repository.save(manifest)
        except (KeyError, ValueError, OSError, sqlite3.Error) as error:
            self.repository.update(
                update_id, status="data_prepare_failed", updated_at_ns=time.time_ns()
            )
            raise ModelUpdateError(str(error)) from error
        return self.repository.update(
            update_id,
            training_dataset_id=manifest["dataset_id"],
            status="waiting_training",
            updated_at_ns=time.time_ns(),
        )

    def start_training(self, update_id: str) -> dict[str, Any]:
        """Record the operator's explicit handoff to an offline trainer.

        Builds and persists the trainer plan so the selected dual-family
        trainer (edge H5 or cloud MOMENT) knows where to train and write.
        """

        task = self._task(update_id)
        self._require_state(task, {"waiting_training"})
        manifest = self._manifest(update_id)
        try:
            plan = build_training_plan(
                update_id=update_id,
                model_type=task["model_type"],
                dataset_id=manifest["dataset_id"],
                training_root=self.data_root,
            )
        except ValueError as error:
            raise ModelUpdateError(str(error)) from error
        return self.repository.update(
            update_id,
            trainer_plan_json=plan,
            status="training",
            updated_at_ns=time.time_ns(),
        )

    def run_training(self, update_id: str) -> dict[str, Any]:
        """Execute the offline trainer and register its candidate artifact.

        Runs the trainer selected by the persisted plan against the frozen
        dataset manifest, then hands the produced artifact to
        ``register_training_result`` so the lifecycle continues to validation.
        """

        task = self._task(update_id)
        self._require_state(task, {"training"})
        if self.trainer is None:
            raise ModelUpdateError("MODEL_UPDATE_SCENARIO_NOT_CONFIGURED")
        plan = task.get("trainer_plan")
        if not isinstance(plan, dict) or not plan.get("output_dir"):
            raise ModelUpdateError("TRAINER_PLAN_NOT_FOUND")
        manifest = self._manifest(update_id)
        try:
            payload = self.trainer.run(plan, manifest)
        except (ValueError, OSError) as error:
            self.repository.update(
                update_id, status="training_failed", updated_at_ns=time.time_ns()
            )
            raise ModelUpdateError(str(error)) from error
        return self.register_training_result(update_id, payload)

    def register_training_result(
        self, update_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, TRAINING_RESULT_STATES)
        manifest = self._manifest(update_id)
        try:
            candidate = self.candidate_registry.register(manifest, payload)
        except ValueError as error:
            self.repository.update(
                update_id, status="training_failed", updated_at_ns=time.time_ns()
            )
            raise ModelUpdateError(str(error)) from error
        return self.repository.update(
            update_id,
            candidate_version=candidate["candidate_version"],
            candidate_artifact_json=candidate,
            status="trained",
            updated_at_ns=time.time_ns(),
        )

    def validate(
        self, update_id: str, test_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, VALIDATION_STATES)
        try:
            validation = validate_candidate(
                task, self._manifest(update_id), test_results, self.config
            )
        except ValueError as error:
            raise ModelUpdateError(str(error)) from error
        status = (
            "waiting_confirmation"
            if validation["validation_passed"]
            else "validation_failed"
        )
        return self.repository.update(
            update_id,
            validation_result_json=validation,
            status=status,
            updated_at_ns=time.time_ns(),
        )

    def approve(self, update_id: str, *, confirmed_by: str | None) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, CONFIRMATION_STATES)
        try:
            approved_model = approve_candidate(task, confirmed_by or "")
        except ApprovalError as error:
            raise ModelUpdateError(str(error)) from error
        return self.repository.update(
            update_id,
            confirmation_result_json=approved_model,
            status="approved",
            updated_at_ns=time.time_ns(),
        )

    def reject(self, update_id: str, *, confirmed_by: str | None) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, CONFIRMATION_STATES)
        if not isinstance(confirmed_by, str) or not confirmed_by.strip():
            raise ModelUpdateError("CONFIRMER_REQUIRED")
        result = {
            "action": "rejected",
            "confirmed_by": confirmed_by.strip(),
            "confirmed_at_ns": time.time_ns(),
        }
        return self.repository.update(
            update_id,
            confirmation_result_json=result,
            status="rejected",
            updated_at_ns=time.time_ns(),
        )

    def handoff_distribution(
        self,
        update_id: str,
        *,
        local_cloud_activator: Callable[[dict[str, Any], str], Any] | None = None,
    ) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, DISTRIBUTION_HANDOFF_STATES)
        try:
            request = build_distribution_request(
                task["confirmation_result"],
                subject_id=task.get("subject_id"),
            )
        except ValueError as error:
            raise ModelUpdateError(str(error)) from error
        handed_off = self.repository.update(
            update_id,
            distribution_result_json=request,
            status="handoff_to_distribution",
            updated_at_ns=time.time_ns(),
        )
        if request["target"].get("family") != "cloud" or local_cloud_activator is None:
            return handed_off
        try:
            local_cloud_activator(
                handed_off["candidate_artifact"], handed_off["candidate_version"]
            )
        except Exception as error:
            self.record_distribution_result(
                update_id,
                {"status": "failed", "message": str(error), "deploy_to": "local_cloud"},
                local_cloud_activation_result=True,
            )
            raise ModelUpdateError("LOCAL_CLOUD_ACTIVATION_FAILED") from error
        return self.record_distribution_result(
            update_id,
            {"status": "succeeded", "deploy_to": "local_cloud"},
            local_cloud_activation_result=True,
        )

    def record_distribution_result(
        self,
        update_id: str,
        payload: dict[str, Any],
        *,
        local_cloud_activation_result: bool = False,
    ) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(
            task, {"handoff_to_distribution", "distribution_in_progress"}
        )
        if (
            MODEL_TYPE_SPECS[task["model_type"]].family == "cloud"
            and not local_cloud_activation_result
        ):
            raise ModelUpdateError("CLOUD_DISTRIBUTION_REQUIRES_LOCAL_ACTIVATION")
        external_status = payload.get("status") if isinstance(payload, dict) else None
        status_map = {
            "in_progress": "distribution_in_progress",
            "succeeded": "distribution_succeeded",
            "failed": "distribution_failed",
        }
        if external_status not in status_map:
            raise ModelUpdateError("INVALID_DISTRIBUTION_RESULT")
        recorded_at_ns = time.time_ns()
        stored_result = dict(payload)
        stored_result["recorded_at_ns"] = recorded_at_ns
        recorded = self.repository.update(
            update_id,
            distribution_result_json=stored_result,
            status=status_map[external_status],
            updated_at_ns=recorded_at_ns,
        )
        if external_status == "succeeded":
            self._active_versions().set(
                task["model_type"], task["candidate_version"]
            )
        return recorded

    def post_validate(self, update_id: str, analysis_id: str) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, {"distribution_succeeded", "verifying"})
        analysis = self.repository.get_analysis(analysis_id)
        if analysis is None:
            raise ModelUpdateError("GLOBAL_ANALYSIS_NOT_FOUND")
        if analysis_id == task["analysis_id"]:
            raise ModelUpdateError("POST_ANALYSIS_MUST_BE_NEW")
        if analysis["subject_id"] != task["subject_id"]:
            raise ModelUpdateError("POST_ANALYSIS_SUBJECT_MISMATCH")
        distribution = task.get("distribution_result")
        distributed_at_ns = (
            distribution.get("recorded_at_ns")
            if isinstance(distribution, dict)
            else None
        )
        if (
            not isinstance(distributed_at_ns, int)
            or analysis["created_at_ns"] <= distributed_at_ns
        ):
            raise ModelUpdateError("POST_ANALYSIS_NOT_AFTER_DISTRIBUTION")
        try:
            metrics = select_post_validation_metrics(
                analysis["result"],
                problem_context=task.get("problem_context") or {},
                minimum_sample_count=self.config.min_update_evidence_count,
            )
        except ValueError as error:
            raise ModelUpdateError(str(error)) from error
        self.repository.update(
            update_id, status="verifying", updated_at_ns=time.time_ns()
        )
        try:
            result = validate_post_deployment(task, metrics, self.config)
        except ValueError as error:
            raise ModelUpdateError(str(error)) from error
        result["analysis_id"] = analysis_id
        return self.repository.update(
            update_id,
            post_validation_result_json=result,
            status=result["outcome"],
            updated_at_ns=time.time_ns(),
        )

    def request_rollback(
        self, update_id: str, *, requested_by: str
    ) -> dict[str, Any]:
        task = self._task(update_id)
        self._require_state(task, {"ineffective", "partial_improvement", "succeeded"})
        if not isinstance(requested_by, str) or not requested_by.strip():
            raise ModelUpdateError("ROLLBACK_REQUESTER_REQUIRED")
        result = dict(task.get("post_validation_result") or {})
        result.update(
            {
                "rollback_requested_by": requested_by.strip(),
                "rollback_requested_at_ns": time.time_ns(),
            }
        )
        return self.repository.update(
            update_id,
            rollback_requested=1,
            rollback_target_version=task["baseline_version"],
            post_validation_result_json=result,
            updated_at_ns=time.time_ns(),
        )

    def execute_rollback(
        self,
        update_id: str,
        *,
        executed_by: str,
        local_cloud_activator: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Actually revert the active model version to the baseline.

        Runs only after ``request_rollback`` set the target; flips the active
        version pointer so both the cloud review model and the edge pull path
        resolve back to the pre-update version.
        """

        task = self._task(update_id)
        self._require_state(task, {"ineffective", "partial_improvement", "succeeded"})
        if task.get("rollback_requested") is not True:
            raise ModelUpdateError("ROLLBACK_NOT_REQUESTED")
        target = task.get("rollback_target_version")
        if not isinstance(target, str) or not target.strip():
            raise ModelUpdateError("ROLLBACK_TARGET_MISSING")
        if not isinstance(executed_by, str) or not executed_by.strip():
            raise ModelUpdateError("ROLLBACK_EXECUTOR_REQUIRED")
        model_type = task["model_type"]
        if MODEL_TYPE_SPECS[model_type].family == "cloud":
            if local_cloud_activator is None:
                raise ModelUpdateError("CLOUD_ROLLBACK_REQUIRES_LOCAL_ACTIVATION")
            try:
                local_cloud_activator(target)
            except Exception as error:
                raise ModelUpdateError(
                    "LOCAL_CLOUD_ROLLBACK_ACTIVATION_FAILED"
                ) from error
        self._active_versions().set(model_type, target)
        result = {
            "action": "rolled_back",
            "model_type": model_type,
            "rollback_target_version": target,
            "executed_by": executed_by.strip(),
            "executed_at_ns": time.time_ns(),
        }
        return self.repository.update(
            update_id,
            rollback_result_json=result,
            status="rolled_back",
            updated_at_ns=time.time_ns(),
        )

    def record_rollback_result(
        self, update_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Record an edge node's rollback acknowledgement."""

        task = self._task(update_id)
        if task.get("rollback_requested") is not True:
            raise ModelUpdateError("ROLLBACK_NOT_REQUESTED")
        status = payload.get("status")
        if status not in {"succeeded", "failed"}:
            raise ModelUpdateError("INVALID_ROLLBACK_RESULT")
        edge_node_id = payload.get("edge_node_id")
        if not isinstance(edge_node_id, str) or not edge_node_id.strip():
            raise ModelUpdateError("ROLLBACK_EDGE_NODE_REQUIRED")
        target_version = payload.get("rollback_target_version")
        if target_version != task.get("rollback_target_version"):
            raise ModelUpdateError("ROLLBACK_TARGET_MISMATCH")

        recorded_at_ns = time.time_ns()
        result = dict(task.get("rollback_result") or {})
        result["edge_ack"] = {
            "status": status,
            "edge_node_id": edge_node_id.strip(),
            "rollback_target_version": target_version,
            "recorded_at_ns": recorded_at_ns,
        }
        changes: dict[str, Any] = {
            "rollback_result_json": result,
            "updated_at_ns": recorded_at_ns,
        }
        if status == "succeeded":
            changes.update(status="rolled_back", rollback_requested=0)
        return self.repository.update(update_id, **changes)

    def _manifest(self, update_id: str) -> dict[str, Any]:
        manifest = self.dataset_repository.get_by_update(update_id)
        if manifest is None:
            raise ModelUpdateError("DATASET_MANIFEST_NOT_FOUND")
        return manifest

    def _task(self, update_id: str) -> dict[str, Any]:
        task = self.repository.get(update_id)
        if task is None:
            raise ModelUpdateError("UPDATE_NOT_FOUND")
        return task

    @staticmethod
    def _require_state(task: dict[str, Any], allowed: set[str]) -> None:
        if task["status"] not in allowed:
            raise ModelUpdateError("INVALID_UPDATE_STATE")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelUpdateError("INVALID_UPDATE_REQUEST")
    return value.strip()
