"""Deterministic providers for the minimal virtual-power-plant validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.arbitration_contracts import ArbitrationContext, DecisionUnit, RuleDecision
from core.consistency_engine import ConsistencyDecision, ConsistencyRequest
from core.scenario_contracts import ScenarioInferenceRequest
from core.scenario_plugin import (
    EdgeInferenceMetadata,
    EdgeInferenceRuntime,
    EdgeInferenceRuntimeRequest,
    StorageRegistrar,
)
from sender.input_adapter import PreparedScenarioInput


SCENARIO_ID = "virtual_power_plant"
EDGE_MODEL_ID = "vpp_edge_peak_estimator"
EDGE_MODEL_VERSION = "validation-1.0"
CLOUD_MODEL_ID = "vpp_cloud_dispatch"
CLOUD_MODEL_VERSION = "validation-1.0"

REGIONS = (
    {"region_id": "region_a", "net_load_kw": 320.0, "local_limit_kw": 350.0,
     "available_reduction_kw": 30.0},
    {"region_id": "region_b", "net_load_kw": 410.0, "local_limit_kw": 380.0,
     "available_reduction_kw": 80.0},
    {"region_id": "region_c", "net_load_kw": 270.0, "local_limit_kw": 300.0,
     "available_reduction_kw": 50.0},
)


@dataclass(frozen=True)
class VppInputWindow:
    sequence_number: int
    start_index: int
    end_index: int
    window_index: int
    data: dict[str, object]


class VppInputAdapter:
    def __init__(self) -> None:
        self.persisted_packet_ids: list[str] = []

    def prepare(
        self,
        source_path: Path | str,
        *,
        unit_id: str,
        duration_ms: int,
        count: int,
    ) -> PreparedScenarioInput:
        if not unit_id or duration_ms <= 0 or not 1 <= count <= len(REGIONS):
            raise ValueError("INVALID_VPP_INPUT")
        windows = tuple(
            VppInputWindow(
                sequence_number=index,
                start_index=index - 1,
                end_index=index,
                window_index=index - 1,
                data=dict(region),
            )
            for index, region in enumerate(REGIONS[:count], start=1)
        )
        return PreparedScenarioInput(
            source_path=Path(source_path),
            first_window=windows[0],
            windows=iter(windows),
        )

    def build_packet(
        self,
        *,
        device_id: str,
        task_id: str,
        unit_id: str,
        sender_id: str,
        sequence_number: int,
        window: VppInputWindow,
        end_generate_timestamp_ns: int,
    ) -> dict[str, Any]:
        return {
            "packet_id": f"{task_id}:{sequence_number}",
            "scenario_id": SCENARIO_ID,
            "task_id": task_id,
            "unit_id": unit_id,
            "device_id": device_id,
            "source_id": sender_id,
            "capability": "edge_inference",
            "observation_window_id": f"region-{window.window_index + 1}",
            "created_at_ns": end_generate_timestamp_ns,
            "evidence": dict(window.data),
        }

    def next_window(
        self,
        prepared_input: PreparedScenarioInput,
        *,
        unit_id: str,
        expected_sequence: int,
    ) -> VppInputWindow:
        try:
            window = next(prepared_input.windows)
        except StopIteration as error:
            raise RuntimeError("VPP_INPUT_EXHAUSTED") from error
        if window.sequence_number != expected_sequence:
            raise RuntimeError("VPP_INPUT_SEQUENCE_MISMATCH")
        return window

    def persist_source(
        self,
        *,
        packet: dict[str, Any],
        task_id: str,
        unit_id: str,
        source_path: Path,
        window: VppInputWindow,
    ) -> None:
        self.persisted_packet_ids.append(packet["packet_id"])

    def serialize_packet(self, packet: dict[str, Any]) -> bytes:
        return json.dumps(packet, sort_keys=True).encode("utf-8")


class VppInputAdapterProvider:
    scenario_id = SCENARIO_ID

    def build_adapter(
        self,
        state_dir: Path,
        source_mapping_store: object | None = None,
    ) -> VppInputAdapter:
        return VppInputAdapter()


@dataclass(frozen=True)
class VppReadiness:
    ok: bool = True
    model_version: str | None = EDGE_MODEL_VERSION
    version_mismatch: bool = False
    detail: str = "minimal VPP validation ready"


class VppModelClient:
    cfg = {"scenario_id": SCENARIO_ID}

    def __init__(self, provider: "VppEdgeInferenceProvider") -> None:
        self._provider = provider

    def readiness(self) -> VppReadiness:
        return VppReadiness()

    def infer_task(self, task: object, **kwargs: Any) -> object:
        return self._provider.infer_compatible(task)

    def activate_version(self, target_version: str) -> Mapping[str, str]:
        return {"status": "active", "model_version": target_version}


class VppEdgeInferenceProvider:
    scenario_id = SCENARIO_ID

    @property
    def metadata(self) -> EdgeInferenceMetadata:
        return EdgeInferenceMetadata(
            backend_id=EDGE_MODEL_ID,
            default_model_version=EDGE_MODEL_VERSION,
            feature_extractor_version="vpp-energy-fields-1",
            deployment_status="validation",
        )

    def build_runtime(self, request: EdgeInferenceRuntimeRequest) -> EdgeInferenceRuntime:
        return EdgeInferenceRuntime(
            pipeline_backend=EDGE_MODEL_ID,
            model_client=VppModelClient(self),
            evidence_builder=lambda payload: dict(payload),
        )

    def infer_compatible(self, payload: Any) -> dict[str, Any]:
        request = _coerce_request(payload)
        load = _non_negative(request.evidence, "net_load_kw")
        limit = _non_negative(request.evidence, "local_limit_kw")
        available = _non_negative(request.evidence, "available_reduction_kw")
        if limit == 0:
            raise ValueError("INVALID_VPP_REQUEST")
        peak = load > limit
        margin = abs(load - limit) / limit
        return _diagnosis(
            request,
            state="peak_risk" if peak else "within_limit",
            confidence=min(0.99, 0.8 + margin),
            risk_level="high" if peak else "low",
            action_level=3 if peak else 0,
            model_id=EDGE_MODEL_ID,
            model_version=EDGE_MODEL_VERSION,
            evidence={
                "source": "edge",
                "net_load_kw": load,
                "local_limit_kw": limit,
                "available_reduction_kw": available,
                "recommended_reduction_kw": min(max(load - limit, 0.0), available),
            },
        )


class VppCloudHandler:
    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _coerce_request(payload)
        regions = request.evidence.get("regions")
        if not isinstance(regions, Sequence) or isinstance(regions, (str, bytes)):
            raise ValueError("INVALID_VPP_REQUEST")
        normalized = [_validated_region(item) for item in regions]
        if not normalized:
            raise ValueError("INVALID_VPP_REQUEST")
        region_ids = [str(item["region_id"]) for item in normalized]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("INVALID_VPP_REQUEST")
        grid_limit = _non_negative(request.evidence, "grid_limit_kw")
        original_load = sum(item["net_load_kw"] for item in normalized)
        required = max(original_load - grid_limit, 0.0)
        remaining = required
        allocation: dict[str, float] = {}
        for region in sorted(
            normalized,
            key=lambda item: item["available_reduction_kw"],
            reverse=True,
        ):
            reduction = min(region["available_reduction_kw"], remaining)
            if reduction > 0:
                allocation[str(region["region_id"])] = reduction
                remaining -= reduction
        planned = required - remaining
        degraded = remaining > 0
        return _diagnosis(
            request,
            state="peak_risk" if required > 0 else "within_limit",
            confidence=0.95 if not degraded else 0.7,
            risk_level="high" if required > 0 else "low",
            action_level=3 if required > 0 else 0,
            model_id=CLOUD_MODEL_ID,
            model_version=CLOUD_MODEL_VERSION,
            evidence={
                "source": "cloud",
                "original_load_kw": original_load,
                "grid_limit_kw": grid_limit,
                "required_reduction_kw": required,
                "planned_reduction_kw": planned,
                "unmet_reduction_kw": remaining,
                "post_dispatch_load_kw": original_load - planned,
                "allocation_kw": allocation,
                "degraded": degraded,
            },
        )

    def arbitrate_device_conflict(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("VPP arbitration uses arbitration_policy")

    def get_device_arbitration(self, conflict_id: str) -> dict[str, Any] | None:
        return None


class VppCloudDiagnosisProvider:
    scenario_id = SCENARIO_ID

    def build_handler(self, database_path: Path) -> VppCloudHandler:
        return VppCloudHandler()


class VppConsistencyPolicy:
    scenario_id = SCENARIO_ID

    def evaluate(self, request: ConsistencyRequest) -> ConsistencyDecision:
        if not request.units:
            raise ValueError("VPP_UNITS_REQUIRED")
        states = tuple(_vpp_state(unit.scenario_payload) for unit in request.units)
        received = tuple(unit.unit_id for unit in request.units)
        missing = tuple(
            unit_id for unit_id in request.expected_unit_ids if unit_id not in received
        )
        dominant = max(request.units, key=lambda unit: unit.action_level)
        conflict = len(set(states)) > 1
        final_state = "peak_risk" if "peak_risk" in states else "within_limit"
        return ConsistencyDecision(
            status="INCOMPLETE" if missing else "FINAL",
            received_unit_ids=received,
            missing_unit_ids=missing,
            final_state=final_state,
            final_action_level=dominant.action_level,
            final_action="dispatch_reduction" if final_state == "peak_risk" else "hold",
            confidence=min(unit.confidence for unit in request.units),
            data_quality_score=min(unit.data_quality_score for unit in request.units),
            has_conflict=conflict,
            conflict_reasons=("peak_state_mismatch",) if conflict else (),
            degraded=bool(missing),
            decision_source="VPP_POLICY",
        )


class VppArbitrationPolicy:
    scenario_type = SCENARIO_ID

    def build_context(self, request: dict[str, Any]) -> ArbitrationContext:
        try:
            units = [DecisionUnit(**item) for item in request["decision_units"]]
            context = ArbitrationContext(
                scenario_type=request["scenario_type"],
                conflict_id=request["conflict_id"],
                subject_id=request["subject_id"],
                task_id=request["task_id"],
                decision_units=units,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("INVALID_VPP_ARBITRATION") from error
        if context.scenario_type != SCENARIO_ID:
            raise ValueError("INVALID_VPP_SCENARIO")
        try:
            for unit in context.decision_units:
                _vpp_state({"state": unit.state})
                if unit.recommended_action not in self.action_severity():
                    raise ValueError("UNSUPPORTED_VPP_ACTION")
        except ValueError as error:
            raise ValueError("INVALID_VPP_ARBITRATION") from error
        return context

    def evaluate_rules(self, context: ArbitrationContext) -> RuleDecision:
        candidates = [unit for unit in context.decision_units if unit.state == "peak_risk"]
        if not candidates:
            return RuleDecision(triggered=False)
        dominant = max(candidates, key=lambda unit: unit.confidence)
        return RuleDecision(
            triggered=True,
            rule_id="vpp-grid-limit-safety",
            final_action="dispatch_reduction",
            confidence=dominant.confidence,
            dominant_unit_id=dominant.unit_id,
            reason="aggregate load exceeds the grid limit",
        )

    def action_to_state(self, action: str) -> str:
        states = {"hold": "within_limit", "dispatch_reduction": "peak_risk"}
        try:
            return states[action]
        except KeyError as error:
            raise ValueError("UNSUPPORTED_VPP_ACTION") from error

    def action_severity(self) -> dict[str, int]:
        return {"hold": 0, "dispatch_reduction": 3}

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


class VppStorageProvider:
    scenario_id = SCENARIO_ID

    def initialize(self, registrar: StorageRegistrar) -> None:
        registrar.execute_schema(
            """CREATE TABLE IF NOT EXISTS vpp_dispatch_result(
                   result_id TEXT PRIMARY KEY,
                   original_load_kw REAL NOT NULL,
                   planned_reduction_kw REAL NOT NULL,
                   post_dispatch_load_kw REAL NOT NULL
               );"""
        )


def aggregate_request() -> dict[str, Any]:
    """Return the fixed aggregate request shared by the demo and tests."""
    return {
        "scenario_id": SCENARIO_ID,
        "task_id": "vpp-dispatch-1",
        "unit_id": "virtual-plant-1",
        "device_id": "energy-aggregator-1",
        "capability": "cloud_diagnosis",
        "observation_window_id": "dispatch-window-1",
        "evidence": {"grid_limit_kw": 900.0, "regions": [dict(item) for item in REGIONS]},
    }


def _coerce_request(payload: Any) -> ScenarioInferenceRequest:
    if isinstance(payload, ScenarioInferenceRequest):
        request = payload
    elif isinstance(payload, Mapping):
        try:
            evidence = payload["evidence"]
            if not isinstance(evidence, Mapping):
                raise ValueError("INVALID_VPP_REQUEST")
            request = ScenarioInferenceRequest(
                scenario_id=payload["scenario_id"],
                task_id=payload["task_id"],
                unit_id=payload["unit_id"],
                device_id=payload["device_id"],
                capability=payload["capability"],
                observation_window_id=payload["observation_window_id"],
                evidence=evidence,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("INVALID_VPP_REQUEST") from error
    else:
        raise ValueError("INVALID_VPP_REQUEST")
    if request.scenario_id != SCENARIO_ID:
        raise ValueError("INVALID_VPP_SCENARIO")
    return request


def _validated_region(value: object) -> dict[str, float | str]:
    if not isinstance(value, Mapping):
        raise ValueError("INVALID_VPP_REQUEST")
    region_id = value.get("region_id")
    if not isinstance(region_id, str) or not region_id:
        raise ValueError("INVALID_VPP_REQUEST")
    net_load = _non_negative(value, "net_load_kw")
    available_reduction = _non_negative(value, "available_reduction_kw")
    if available_reduction > net_load:
        raise ValueError("INVALID_VPP_REQUEST")
    return {
        "region_id": region_id,
        "net_load_kw": net_load,
        "available_reduction_kw": available_reduction,
    }


def _non_negative(evidence: Mapping[str, Any], field: str) -> float:
    value = evidence.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("INVALID_VPP_REQUEST")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("INVALID_VPP_REQUEST")
    return number


def _diagnosis(
    request: ScenarioInferenceRequest,
    *,
    state: str,
    confidence: float,
    risk_level: str,
    action_level: int,
    model_id: str,
    model_version: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "scenario_id": request.scenario_id,
        "task_id": request.task_id,
        "unit_id": request.unit_id,
        "state": state,
        "confidence": confidence,
        "risk_level": risk_level,
        "action_level": action_level,
        "model_id": model_id,
        "model_version": model_version,
        "evidence": dict(evidence),
    }


def _vpp_state(payload: Mapping[str, Any]) -> str:
    state = payload.get("state")
    if state not in {"within_limit", "peak_risk"}:
        raise ValueError("UNSUPPORTED_VPP_STATE")
    return str(state)
