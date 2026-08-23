"""Minimal providers for the test-only reference inspection scenario."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.arbitration_contracts import (
    ArbitrationContext,
    DecisionUnit,
    RuleDecision,
)
from core.consistency_engine import (
    ConsistencyDecision,
    ConsistencyRequest,
)
from core.scenario_contracts import ScenarioInferenceRequest
from core.scenario_plugin import (
    EdgeInferenceMetadata,
    EdgeInferenceRuntime,
    EdgeInferenceRuntimeRequest,
    StorageRegistrar,
)


SCENARIO_ID = "reference_inspection"
EDGE_MODEL_ID = "reference_edge_fixed"
EDGE_MODEL_VERSION = "reference-edge-test-1"
CLOUD_MODEL_ID = "reference_cloud_fixed"
CLOUD_MODEL_VERSION = "reference-cloud-test-1"


class ReferenceInputAdapter:
    def read(self) -> ScenarioInferenceRequest:
        return ScenarioInferenceRequest(
            scenario_id=SCENARIO_ID,
            task_id="inspection-task-1",
            unit_id="panel-1",
            device_id="camera-1",
            capability="edge_inference",
            observation_window_id="frame-1",
            evidence={"defect_score": 0.85, "source": "fixed-fixture"},
        )


class ReferenceInputAdapterProvider:
    scenario_id = SCENARIO_ID

    def build_adapter(
        self,
        state_dir: Path,
        source_mapping_store: object | None = None,
    ) -> ReferenceInputAdapter:
        return ReferenceInputAdapter()


@dataclass(frozen=True)
class ReferenceReadiness:
    ok: bool = True
    model_version: str | None = EDGE_MODEL_VERSION
    version_mismatch: bool = False
    detail: str = "reference fixture ready"


class ReferenceModelClient:
    cfg = {"scenario_id": SCENARIO_ID}

    def __init__(self, provider: "ReferenceEdgeInferenceProvider") -> None:
        self._provider = provider

    def readiness(self) -> ReferenceReadiness:
        return ReferenceReadiness()

    def infer_task(self, task: object, **kwargs: Any) -> object:
        return self._provider.infer_compatible(task)

    def activate_version(self, target_version: str) -> Mapping[str, str]:
        return {"status": "active", "model_version": target_version}


class ReferenceEdgeInferenceProvider:
    scenario_id = SCENARIO_ID

    @property
    def metadata(self) -> EdgeInferenceMetadata:
        return EdgeInferenceMetadata(
            backend_id=EDGE_MODEL_ID,
            default_model_version=EDGE_MODEL_VERSION,
            feature_extractor_version="reference-feature-test-1",
            deployment_status="test_fixture",
        )

    def build_runtime(
        self,
        request: EdgeInferenceRuntimeRequest,
    ) -> EdgeInferenceRuntime:
        return EdgeInferenceRuntime(
            pipeline_backend=EDGE_MODEL_ID,
            model_client=ReferenceModelClient(self),
            evidence_builder=lambda payload: dict(payload),
        )

    def infer_compatible(self, payload: Any) -> dict[str, Any]:
        request = _coerce_request(payload)
        return _diagnosis(
            request,
            score=_defect_score(request.evidence),
            model_id=EDGE_MODEL_ID,
            model_version=EDGE_MODEL_VERSION,
            source="edge",
        )


class ReferenceCloudHandler:
    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _coerce_request(payload)
        score = min(1.0, _defect_score(request.evidence) + 0.05)
        return _diagnosis(
            request,
            score=score,
            model_id=CLOUD_MODEL_ID,
            model_version=CLOUD_MODEL_VERSION,
            source="cloud",
        )

    def arbitrate_device_conflict(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("reference arbitration uses arbitration_policy")

    def get_device_arbitration(self, conflict_id: str) -> dict[str, Any] | None:
        return None


class ReferenceCloudDiagnosisProvider:
    scenario_id = SCENARIO_ID

    def build_handler(self, database_path: Path) -> ReferenceCloudHandler:
        return ReferenceCloudHandler()


class ReferenceConsistencyPolicy:
    scenario_id = SCENARIO_ID

    def evaluate(self, request: ConsistencyRequest) -> ConsistencyDecision:
        if not request.units:
            raise ValueError("REFERENCE_INSPECTION_UNITS_REQUIRED")
        states = tuple(_inspection_state(unit.scenario_payload) for unit in request.units)
        received = tuple(unit.unit_id for unit in request.units)
        missing = tuple(
            unit_id for unit_id in request.expected_unit_ids if unit_id not in received
        )
        dominant = max(request.units, key=lambda unit: unit.action_level)
        has_conflict = len(set(states)) > 1
        return ConsistencyDecision(
            status="INCOMPLETE" if missing else "FINAL",
            received_unit_ids=received,
            missing_unit_ids=missing,
            final_state="needs_review" if has_conflict else states[0],
            final_action_level=dominant.action_level,
            final_action=(
                "stop_and_inspect" if dominant.action_level >= 3 else "continue"
            ),
            confidence=min(unit.confidence for unit in request.units),
            data_quality_score=min(
                unit.data_quality_score for unit in request.units
            ),
            has_conflict=has_conflict,
            conflict_reasons=("inspection_state_mismatch",) if has_conflict else (),
            degraded=bool(missing),
            decision_source="REFERENCE_POLICY",
        )


class ReferenceArbitrationPolicy:
    scenario_type = SCENARIO_ID

    def build_context(self, request: dict[str, Any]) -> ArbitrationContext:
        try:
            units = [DecisionUnit(**item) for item in request["decision_units"]]
            return ArbitrationContext(
                scenario_type=request["scenario_type"],
                conflict_id=request["conflict_id"],
                subject_id=request["subject_id"],
                task_id=request["task_id"],
                decision_units=units,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("INVALID_REFERENCE_ARBITRATION") from error

    def evaluate_rules(self, context: ArbitrationContext) -> RuleDecision:
        if context.scenario_type != SCENARIO_ID:
            raise ValueError("INVALID_REFERENCE_SCENARIO")
        candidates = [
            unit
            for unit in context.decision_units
            if unit.state == "defect_detected" and unit.risk_level == "high"
        ]
        if not candidates:
            return RuleDecision(triggered=False)
        dominant = max(candidates, key=lambda unit: unit.confidence)
        return RuleDecision(
            triggered=True,
            rule_id="reference-safety-stop",
            final_action="stop_and_inspect",
            confidence=dominant.confidence,
            dominant_unit_id=dominant.unit_id,
            reason="high-risk inspection defect detected",
        )

    def action_to_state(self, action: str) -> str:
        states = {"continue": "clear", "stop_and_inspect": "defect_detected"}
        try:
            return states[action]
        except KeyError as error:
            raise ValueError("UNSUPPORTED_REFERENCE_ACTION") from error

    def action_severity(self) -> dict[str, int]:
        return {"continue": 0, "stop_and_inspect": 3}

    def decision_thresholds(self) -> tuple[float, float]:
        return 0.6, 0.1

    def build_scenario_result(
        self,
        *,
        context: ArbitrationContext,
        dominant_unit_id: str | None,
        triggered_rule_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "scenario_id": SCENARIO_ID,
            "conflict_id": context.conflict_id,
            "dominant_unit_id": dominant_unit_id,
            "triggered_rule_id": triggered_rule_id,
            "reason": reason,
        }


class ReferenceStorageProvider:
    scenario_id = SCENARIO_ID

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def initialize(self, registrar: StorageRegistrar) -> None:
        registrar.execute_schema(
            """CREATE TABLE IF NOT EXISTS reference_inspection_result(
                   result_id TEXT PRIMARY KEY,
                   state TEXT NOT NULL
               );"""
        )

    def save_record(self, result_id: str, state: str) -> None:
        self._records[result_id] = {"result_id": result_id, "state": state}

    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(record) for record in self._records.values())


def _coerce_request(payload: Any) -> ScenarioInferenceRequest:
    if isinstance(payload, ScenarioInferenceRequest):
        request = payload
    elif isinstance(payload, dict):
        try:
            request = ScenarioInferenceRequest(
                scenario_id=payload["scenario_id"],
                task_id=payload["task_id"],
                unit_id=payload["unit_id"],
                device_id=payload["device_id"],
                capability=payload.get("capability", "cloud_diagnosis"),
                observation_window_id=payload["observation_window_id"],
                evidence=payload["evidence"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("INVALID_REFERENCE_REQUEST") from error
    else:
        raise ValueError("INVALID_REFERENCE_REQUEST")
    if request.scenario_id != SCENARIO_ID:
        raise ValueError("INVALID_REFERENCE_SCENARIO")
    return request


def _defect_score(evidence: Mapping[str, Any]) -> float:
    score = evidence.get("defect_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("INVALID_REFERENCE_REQUEST")
    value = float(score)
    if not 0.0 <= value <= 1.0:
        raise ValueError("INVALID_REFERENCE_REQUEST")
    return value


def _diagnosis(
    request: ScenarioInferenceRequest,
    *,
    score: float,
    model_id: str,
    model_version: str,
    source: str,
) -> dict[str, Any]:
    detected = score >= 0.8
    return {
        "scenario_id": request.scenario_id,
        "task_id": request.task_id,
        "unit_id": request.unit_id,
        "state": "defect_detected" if detected else "clear",
        "confidence": score if detected else 1.0 - score,
        "risk_level": "high" if detected else "low",
        "action_level": 3 if detected else 0,
        "model_id": model_id,
        "model_version": model_version,
        "evidence": {
            "observation_window_id": request.observation_window_id,
            "source": source,
            "defect_score": score,
        },
    }


def _inspection_state(payload: Mapping[str, Any]) -> str:
    state = payload.get("state")
    if state not in {"clear", "defect_detected"}:
        raise ValueError("UNSUPPORTED_REFERENCE_STATE")
    return str(state)
