from dataclasses import replace
from pathlib import Path

import pytest

from core.arbitration_engine import ArbitrationEngine
from core.consistency_engine import (
    ConsistencyEngine,
    ConsistencyRequest,
    ConsistencyUnit,
)
from core.scenario_contracts import ScenarioDiagnosis
from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    INPUT_ADAPTER,
    STORAGE_PROVIDER,
    EdgeInferenceRuntimeRequest,
)
from core.scenario_registry import ScenarioRegistry
from cloud_service.storage.database import connect, initialize_database
from scenarios.bearing.storage import BearingStorageProvider
from tests.fixtures.scenarios.reference_inspection import ReferenceInspectionPlugin


def _registry() -> ScenarioRegistry:
    registry = ScenarioRegistry()
    registry.register(ReferenceInspectionPlugin())
    return registry


def _payload(request) -> dict:
    return {
        "scenario_id": request.scenario_id,
        "task_id": request.task_id,
        "unit_id": request.unit_id,
        "device_id": request.device_id,
        "capability": "cloud_diagnosis",
        "observation_window_id": request.observation_window_id,
        "evidence": dict(request.evidence),
    }


def test_reference_input_runs_through_edge_and_cloud_providers(tmp_path: Path) -> None:
    registry = _registry()
    adapter_provider = registry.require_provider("reference_inspection", INPUT_ADAPTER)
    edge_provider = registry.require_provider("reference_inspection", EDGE_INFERENCE)
    cloud_provider = registry.require_provider("reference_inspection", CLOUD_DIAGNOSIS)
    request = adapter_provider.build_adapter(tmp_path).read()

    edge = ScenarioDiagnosis(**edge_provider.infer_compatible(request))
    cloud = ScenarioDiagnosis(**cloud_provider.build_handler(tmp_path).infer(_payload(request)))

    assert request.scenario_id == "reference_inspection"
    assert edge.state == cloud.state == "defect_detected"
    assert edge.confidence == 0.85
    assert cloud.confidence == 0.9
    assert edge.model_id == "reference_edge_fixed"
    assert cloud.model_id == "reference_cloud_fixed"
    assert "bearing_id" not in _payload(request)
    assert "bearing" not in repr(edge).lower()


def test_reference_edge_provider_builds_complete_test_runtime(tmp_path: Path) -> None:
    registry = _registry()
    adapter_provider = registry.require_provider("reference_inspection", INPUT_ADAPTER)
    edge_provider = registry.require_provider("reference_inspection", EDGE_INFERENCE)
    request = adapter_provider.build_adapter(tmp_path).read()
    runtime = edge_provider.build_runtime(
        EdgeInferenceRuntimeRequest(
            model_root=tmp_path,
            bundled_model_root=tmp_path,
            pinned_model_version=None,
            observation_window_ms=50,
            lifecycle_enabled=False,
        )
    )

    assert runtime.pipeline_backend == "reference_edge_fixed"
    assert runtime.model_client.readiness().ok is True
    assert runtime.model_client.infer_task(request)["state"] == "defect_detected"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"scenario_id": "reference_inspection"}, "INVALID_REFERENCE_REQUEST"),
        (
            replace(
                ReferenceInspectionPlugin()
                .capabilities[INPUT_ADAPTER]
                .provider.build_adapter(Path("."))
                .read(),
                scenario_id="other",
            ),
            "INVALID_REFERENCE_SCENARIO",
        ),
    ],
)
def test_reference_edge_provider_rejects_invalid_requests(payload, error: str) -> None:
    edge_provider = _registry().require_provider("reference_inspection", EDGE_INFERENCE)

    with pytest.raises(ValueError, match=error):
        edge_provider.infer_compatible(payload)


def test_reference_results_run_through_generic_consistency_engine(
    tmp_path: Path,
) -> None:
    registry = _registry()
    adapter = registry.require_provider(
        "reference_inspection", INPUT_ADAPTER
    ).build_adapter(tmp_path)
    edge_provider = registry.require_provider("reference_inspection", EDGE_INFERENCE)
    cloud_provider = registry.require_provider("reference_inspection", CLOUD_DIAGNOSIS)
    request = adapter.read()
    edge = ScenarioDiagnosis(**edge_provider.infer_compatible(request))
    cloud = ScenarioDiagnosis(**cloud_provider.build_handler(tmp_path).infer(_payload(request)))
    policy = registry.require_provider("reference_inspection", CONSISTENCY_POLICY)
    decision = ConsistencyEngine(policy).evaluate(
        ConsistencyRequest(
            units=(
                _consistency_unit("edge-result", edge),
                _consistency_unit("cloud-result", cloud),
            ),
            expected_unit_ids=("edge-result", "cloud-result"),
            closure_reason="all_results_received",
            closed_at_ns=1,
        )
    )

    assert decision.status == "FINAL"
    assert decision.final_state == "defect_detected"
    assert decision.final_action == "stop_and_inspect"
    assert decision.has_conflict is False


def test_reference_consistency_reports_state_conflict() -> None:
    policy = _registry().require_provider("reference_inspection", CONSISTENCY_POLICY)
    decision = ConsistencyEngine(policy).evaluate(
        ConsistencyRequest(
            units=(
                ConsistencyUnit(
                    unit_id="edge-result",
                    lifecycle_status="FINAL",
                    confidence=0.85,
                    data_quality_score=1.0,
                    action_level=3,
                    scenario_payload={"state": "defect_detected"},
                ),
                ConsistencyUnit(
                    unit_id="cloud-result",
                    lifecycle_status="FINAL",
                    confidence=0.9,
                    data_quality_score=1.0,
                    action_level=0,
                    scenario_payload={"state": "clear"},
                ),
            ),
            expected_unit_ids=("edge-result", "cloud-result"),
            closure_reason="all_results_received",
            closed_at_ns=1,
        )
    )

    assert decision.final_state == "needs_review"
    assert decision.has_conflict is True
    assert decision.conflict_reasons == ("inspection_state_mismatch",)


def test_reference_policy_runs_through_generic_arbitration_engine() -> None:
    policy = _registry().require_provider("reference_inspection", ARBITRATION_POLICY)
    context = policy.build_context(
        {
            "scenario_type": "reference_inspection",
            "conflict_id": "inspection-conflict-1",
            "subject_id": "camera-1",
            "task_id": "inspection-task-1",
            "decision_units": [
                {
                    "unit_id": "panel-1",
                    "state": "defect_detected",
                    "confidence": 0.9,
                    "data_quality_score": 1.0,
                    "risk_level": "high",
                    "recommended_action": "stop_and_inspect",
                }
            ],
        }
    )

    def unexpected_fusion(*args, **kwargs):
        raise AssertionError("fusion must not run after the reference safety rule")

    result = ArbitrationEngine(policy, unexpected_fusion).decide(context)

    assert result["final_action"] == "stop_and_inspect"
    assert result["dominant_unit_id"] == "panel-1"
    assert result["triggered_rule_id"] == "reference-safety-stop"
    assert result["resolution_method"] == "scenario_rule"


def test_reference_storage_registers_through_existing_initializer(
    tmp_path: Path,
) -> None:
    storage = _registry().require_provider("reference_inspection", STORAGE_PROVIDER)
    database_path = tmp_path / "reference.db"
    providers = (BearingStorageProvider(), storage)

    storage.save_record("result-1", "defect_detected")
    storage.save_record("result-1", "defect_detected")
    initialize_database(database_path, storage_providers=providers)
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO reference_inspection_result(result_id,state) VALUES (?,?)",
            ("result-1", "defect_detected"),
        )
    initialize_database(database_path, storage_providers=providers)

    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT result_id,state FROM reference_inspection_result"
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM reference_inspection_result"
        ).fetchone()["count"]
    assert dict(row) == {"result_id": "result-1", "state": "defect_detected"}
    assert count == 1
    assert storage.records() == (
        {"result_id": "result-1", "state": "defect_detected"},
    )


def _consistency_unit(unit_id: str, diagnosis: ScenarioDiagnosis) -> ConsistencyUnit:
    return ConsistencyUnit(
        unit_id=unit_id,
        lifecycle_status="FINAL",
        confidence=diagnosis.confidence,
        data_quality_score=1.0,
        action_level=diagnosis.action_level,
        scenario_payload={"state": diagnosis.state},
    )
