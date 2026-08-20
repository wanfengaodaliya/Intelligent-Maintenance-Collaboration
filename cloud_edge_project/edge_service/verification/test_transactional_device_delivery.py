from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from core.diagnosis_contracts import CloudBearingResult, EdgeBearingResult, PacketRoute
from device_decision import DeviceDecisionRoundRepository
from edge_runtime.device_result_outbox import DeviceResultOutbox
from edge_runtime.v12_flow import V12DecisionFlow
from result_lifecycle import BearingResultLifecycleManager, BearingResultRepository
from result_uploader import ResultUploader


def _edge(bearing_id: str, *, grade: int) -> EdgeBearingResult:
    return EdgeBearingResult(
        result_id=f"edge_dw_{bearing_id}_v1",
        device_id="machine_01",
        task_id="task_001",
        bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}",
        decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}",
        window_start_sequence=1,
        window_end_sequence=1,
        window_start_ns=0,
        window_end_ns=50_000_000,
        contributing_packet_ids=(f"packet_{bearing_id}",),
        bearing_state="normal",
        confidence=0.9,
        data_quality_score=0.9,
        risk_level="low",
        action_grade=grade,
        recommended_action=(
            "continue_operation",
            "enhanced_monitoring",
            "scheduled_inspection",
            "urgent_intervention",
            "shutdown",
        )[grade],
        model_version="edge_model_v1",
        created_at_ns=10,
    )


def _route(result: EdgeBearingResult, route: PacketRoute) -> dict:
    return {
        "device_id": result.device_id,
        "task_id": result.task_id,
        "bearing_id": result.bearing_id,
        "decision_round_id": result.decision_round_id,
        "diagnosis_window_id": result.diagnosis_window_id,
        "route": route.value,
        "result_instruction": {
            "result_status": {
                PacketRoute.EDGE: "FINAL",
                PacketRoute.CLOUD_NOW: "WAITING_CLOUD",
                PacketRoute.DEFER: "PROVISIONAL",
            }[route],
            "review_status": (
                "NOT_REQUIRED" if route is PacketRoute.EDGE else "PENDING_CLOUD"
            ),
            "degraded": route is PacketRoute.DEFER,
        },
    }


def _cloud(bearing_id: str) -> CloudBearingResult:
    return CloudBearingResult(
        result_id=f"cloud_dw_{bearing_id}_v1",
        review_id="review_01",
        device_id="machine_01",
        task_id="task_001",
        bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}",
        decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}",
        window_start_sequence=1,
        window_end_sequence=1,
        window_start_ns=0,
        window_end_ns=50_000_000,
        bearing_state="warning",
        confidence=0.95,
        data_quality_score=0.95,
        risk_level="medium",
        action_grade=2,
        recommended_action="scheduled_inspection",
        model_version="cloud_model_v1",
        created_at_ns=20,
    )


class _DeliveryProbe:
    def __init__(self, database_path) -> None:
        self.mqtt = DeviceResultOutbox(database_path, lambda _payload: None)
        self.cloud = ResultUploader(
            database_path, lambda _path, _payload: {"status": "accepted"}
        )
        self.result_ids: list[str] = []

    def persist(self, result, connection: sqlite3.Connection) -> None:
        assert connection.in_transaction
        assert self.mqtt.enqueue(result, connection=connection)
        assert self.cloud.enqueue_device(result, connection=connection)
        self.result_ids.append(result.result_id)


def _flow(database_path, probe: _DeliveryProbe, *, round_timeout_ns=3_500_000_000):
    return V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
        round_timeout_ns=round_timeout_ns,
        on_device_result_persist=probe.persist,
    )


@pytest.mark.parametrize(
    "scenario",
    ("edge_close", "cloud_close", "round_timeout", "late_revision", "arbitration"),
)
def test_five_close_and_revision_paths_persist_both_deliveries_in_transaction(
    tmp_path, scenario: str
) -> None:
    database_path = tmp_path / f"{scenario}.db"
    probe = _DeliveryProbe(database_path)
    flow = _flow(
        database_path,
        probe,
        round_timeout_ns=100 if scenario == "round_timeout" else 3_500_000_000,
    )

    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=3 if scenario == "arbitration" else 1)
    if scenario == "cloud_close":
        flow.apply_edge_result(
            first,
            _route(first, PacketRoute.CLOUD_NOW),
            expected_bearing_ids=("bearing_a",),
            accepted_at_ns=10,
        )
        flow.apply_cloud_result(_cloud("bearing_a"), accepted_at_ns=20)
    elif scenario == "round_timeout":
        flow.apply_edge_result(
            first,
            _route(first, PacketRoute.EDGE),
            expected_bearing_ids=("bearing_a", "bearing_b"),
            accepted_at_ns=10,
        )
        flow.finalize_timeouts(now_ns=111, round_timeout_ns=100)
    else:
        flow.apply_edge_result(
            first,
            _route(first, PacketRoute.EDGE),
            expected_bearing_ids=("bearing_a", "bearing_b"),
            accepted_at_ns=10,
        )
        route = PacketRoute.DEFER if scenario == "late_revision" else PacketRoute.EDGE
        _, initial = flow.apply_edge_result(
            second,
            _route(second, route),
            expected_bearing_ids=("bearing_a", "bearing_b"),
            accepted_at_ns=11,
        )
        if scenario == "late_revision":
            flow.apply_cloud_result(_cloud("bearing_b"), accepted_at_ns=20)
        elif scenario == "arbitration":
            flow.apply_cloud_arbitration_result(
                {
                    "arbitration_id": "arbitration_01",
                    "device_id": "machine_01",
                    "task_id": "task_001",
                    "decision_round_id": "round_01",
                    "device_result_revision": initial.revision,
                    "final_action": "scheduled_inspection",
                    "confidence": 0.95,
                },
                accepted_at_ns=20,
            )

    expected = 2 if scenario in {"late_revision", "arbitration"} else 1
    assert len(probe.result_ids) == expected
    assert _count(database_path, "device_decision_result") == expected
    assert _count(database_path, "device_result_outbox") == expected
    assert _count(database_path, "v12_result_upload", "path='/cloud/device-decision-results'") == expected


def test_delivery_enqueue_failure_rolls_back_device_decision_and_partial_outbox(
    tmp_path,
) -> None:
    database_path = tmp_path / "rollback.db"
    mqtt = DeviceResultOutbox(database_path, lambda _payload: None)
    ResultUploader(database_path, lambda _path, _payload: {"status": "accepted"})

    def fail_after_first_enqueue(result, connection) -> None:
        assert mqtt.enqueue(result, connection=connection)
        raise RuntimeError("cloud outbox insert failed")

    flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
        on_device_result_persist=fail_after_first_enqueue,
    )
    edge = _edge("bearing_a", grade=0)

    with pytest.raises(RuntimeError, match="cloud outbox insert failed"):
        flow.apply_edge_result(
            edge,
            _route(edge, PacketRoute.EDGE),
            expected_bearing_ids=("bearing_a",),
            accepted_at_ns=10,
        )

    assert _count(database_path, "device_decision_result") == 0
    assert _count(database_path, "device_result_outbox") == 0
    assert flow.device_rounds.get_round(
        "machine_01", "task_001", "round_01"
    )["state"] == "OPEN"


def test_delivery_failure_rolls_back_revision_and_arbitration_receipt(tmp_path) -> None:
    database_path = tmp_path / "revision-rollback.db"
    probe = _DeliveryProbe(database_path)
    flow = _flow(database_path, probe)
    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=3)
    flow.apply_edge_result(
        first,
        _route(first, PacketRoute.EDGE),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        accepted_at_ns=10,
    )
    _, initial = flow.apply_edge_result(
        second,
        _route(second, PacketRoute.EDGE),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        accepted_at_ns=11,
    )

    def fail_revision(result, connection) -> None:
        assert probe.mqtt.enqueue(result, connection=connection)
        raise RuntimeError("cloud revision outbox insert failed")

    flow._on_device_result_persist = fail_revision
    with pytest.raises(RuntimeError, match="cloud revision outbox insert failed"):
        flow.apply_cloud_arbitration_result(
            {
                "arbitration_id": "arbitration_rollback",
                "device_id": "machine_01",
                "task_id": "task_001",
                "decision_round_id": "round_01",
                "device_result_revision": initial.revision,
                "final_action": "scheduled_inspection",
                "confidence": 0.95,
            },
            accepted_at_ns=20,
        )

    current = flow.device_rounds.get_current_result(
        "machine_01", "task_001", "round_01"
    )
    assert current is not None and current.result_id == initial.result_id
    assert flow.device_rounds.get_arbitration_receipt("arbitration_rollback") is None
    assert _count(database_path, "device_decision_result") == 1
    assert _count(database_path, "device_result_outbox") == 1
    assert _count(
        database_path,
        "v12_result_upload",
        "path='/cloud/device-decision-results'",
    ) == 1


def test_reconciliation_idempotently_backfills_both_missing_delivery_rows(
    tmp_path,
) -> None:
    database_path = tmp_path / "reconcile.db"
    initial = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
    )
    edge = _edge("bearing_a", grade=0)
    initial.apply_edge_result(
        edge,
        _route(edge, PacketRoute.EDGE),
        expected_bearing_ids=("bearing_a",),
        accepted_at_ns=10,
    )

    probe = _DeliveryProbe(database_path)
    recovered = _flow(database_path, probe)
    assert recovered.reconcile_device_result_deliveries() == 1
    assert recovered.reconcile_device_result_deliveries() == 1

    assert _count(database_path, "device_result_outbox") == 1
    assert _count(
        database_path,
        "v12_result_upload",
        "path='/cloud/device-decision-results'",
    ) == 1


def test_reconciliation_does_not_recreate_retention_cleaned_mqtt_delivery(
    tmp_path,
) -> None:
    database_path = tmp_path / "cleaned.db"
    probe = _DeliveryProbe(database_path)
    flow = _flow(database_path, probe)
    edge = _edge("bearing_a", grade=0)
    flow.apply_edge_result(
        edge,
        _route(edge, PacketRoute.EDGE),
        expected_bearing_ids=("bearing_a",),
        accepted_at_ns=10,
    )
    result_id = probe.result_ids[0]
    assert probe.mqtt.run_once(now_ns=100) == 1
    assert probe.mqtt.cleanup_published(
        retention_ns=1, now_ns=probe.mqtt.clock_ns() + 2
    ) == 1
    assert _count(database_path, "device_result_outbox") == 0

    assert flow.reconcile_device_result_deliveries() == 1

    assert _count(database_path, "device_result_outbox") == 0
    assert _count(
        database_path,
        "device_result_delivery_history",
        f"result_id='{result_id}'",
    ) == 1


def _count(database_path, table: str, where: str = "1=1") -> int:
    with closing(sqlite3.connect(database_path)) as connection:
        return int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}"
            ).fetchone()[0]
        )
