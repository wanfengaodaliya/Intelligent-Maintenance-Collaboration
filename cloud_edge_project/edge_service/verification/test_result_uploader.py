from __future__ import annotations

import sqlite3

from core.diagnosis_contracts import BearingDecisionResult, BearingLifecycleStatus
from result_uploader import ResultUploader


def _bearing() -> BearingDecisionResult:
    return BearingDecisionResult(
        result_id="bearing_round_01_a_r1", revision=1, replaces_result_id=None,
        device_id="machine_01", task_id="task_001", bearing_id="bearing_a",
        sender_id="sender_a", decision_round_id="round_01", diagnosis_window_id="dw_01",
        lifecycle_state=BearingLifecycleStatus.FINAL_EDGE, bearing_state="normal",
        confidence=.9, data_quality_score=.9, risk_level="low", action_grade=0,
        recommended_action="continue_operation", decision_source="FINAL_EDGE",
        review_status="NOT_REQUIRED", degraded=False, edge_result_id="edge_dw_01",
        cloud_result_id=None, model_version="edge_model_v1", created_at_ns=10,
        edge_accepted_at_ns=11,
    )


def _row(database_path, result_id: str):
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM v12_result_upload WHERE result_id=?", (result_id,)
        ).fetchone()


def test_result_uploader_retries_with_persisted_bounded_backoff_and_recovers_restart(tmp_path) -> None:
    database_path = tmp_path / "edge.db"
    uploader = ResultUploader(
        database_path,
        lambda _path, _payload: (_ for _ in ()).throw(TimeoutError("offline")),
        max_backoff_seconds=4,
    )
    uploader.enqueue_bearing(_bearing())

    assert uploader.run_once(now_ns=100) == 1
    failed = _row(database_path, _bearing().result_id)
    assert failed["status"] == "PENDING"
    assert failed["attempt_count"] == 1
    assert failed["next_attempt_at_ns"] == 1_000_000_100
    assert failed["last_error"] == "TimeoutError: offline"
    assert uploader.run_once(now_ns=1_000_000_099) == 0

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE v12_result_upload SET status='UPLOADING' WHERE result_id=?",
            (_bearing().result_id,),
        )
    restarted = ResultUploader(database_path, lambda _path, _payload: {"status": "accepted"})
    assert restarted.run_once(now_ns=1_000_000_100) == 1
    assert _row(database_path, _bearing().result_id)["status"] == "ACKNOWLEDGED"


def test_result_uploader_migrates_the_existing_p1_queue_in_place(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("""CREATE TABLE v12_result_upload(
            result_id TEXT PRIMARY KEY,path TEXT NOT NULL,payload_json TEXT NOT NULL,
            status TEXT NOT NULL,created_at_ns INTEGER NOT NULL)""")
        connection.execute(
            "INSERT INTO v12_result_upload VALUES ('result_01','/cloud/test','{}','UPLOADING',1)"
        )

    ResultUploader(database_path, lambda _path, _payload: {"status": "accepted"})

    row = _row(database_path, "result_01")
    assert row["status"] == "PENDING"
    assert row["attempt_count"] == 0
    assert row["next_attempt_at_ns"] is None
