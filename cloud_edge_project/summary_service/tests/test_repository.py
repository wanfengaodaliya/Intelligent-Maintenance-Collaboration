from __future__ import annotations

import json
import sqlite3

import pytest

from summary_service.aggregation import (
    build_incomplete_window_result,
    build_window_result,
)
from summary_service.contracts import normalize_bearing_result
from summary_service.repository import (
    BearingResultConflictError,
    SummaryRepository,
)


ACTIONS = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "urgent_intervention",
    4: "shutdown",
}

LEVEL_PROBS = {
    0: ({"healthy": 1.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0}, "low"),
    1: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "low"),
    2: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "high"),
    3: ({"healthy": 0.0, "outer_ring_damage": 1.0, "inner_ring_damage": 0.0}, "high"),
}

LEGACY_GRADE = {0: 0, 1: 1, 2: 2, 3: 4}


def bearing(
    bearing_id: str,
    edge_node_id: str,
    state: str = "normal",
    action_level: int = 0,
    *,
    seq: int = 1,
) -> dict:
    suffix = bearing_id[-2:]
    probabilities, risk_level = LEVEL_PROBS[action_level]
    grade = LEGACY_GRADE[action_level]
    return normalize_bearing_result(
        {
            "result_id": f"result_{seq}_{suffix}",
            "device_id": "machine_01",
            "task_id": f"sd_{suffix}_tk_0001",
            "bearing_id": bearing_id,
            "sender_id": f"sender_{suffix}",
            "edge_node_id": edge_node_id,
            "decision_round_id": f"round_{suffix}",
            "window_start_sequence": seq,
            "window_end_sequence": seq,
            "bearing_state": state,
            "risk_level": risk_level,
            "action_grade": grade,
            "recommended_action": ACTIONS[grade],
            "confidence": 0.9,
            "data_quality_score": 1.0,
            "model_version": "model-test",
            "created_at_ns": 100,
            "class_probabilities": probabilities,
        }
    )


def window(repository: SummaryRepository, left: dict, right: dict) -> dict:
    repository.save_bearing_result(left, received_at_ns=1_000)
    repository.save_bearing_result(right, received_at_ns=1_100)
    source = repository.load_window_bearing_results(left["summary_window_id"])
    return build_window_result(source, closed_at_ns=1_200)


def normal_window(repository: SummaryRepository) -> dict:
    result = window(
        repository,
        bearing("bearing_01", "edge_01", "normal", 0, seq=1),
        bearing("bearing_02", "edge_02", "normal", 1, seq=1),
    )
    return repository.save_window_result(result)


def conflict_window(repository: SummaryRepository) -> dict:
    result = window(
        repository,
        bearing("bearing_01", "edge_01", "normal", 0, seq=2),
        bearing("bearing_02", "edge_02", "fault", 3, seq=2),
    )
    return repository.save_window_result(result)


# ----------------------------------------------------------------------
# Bearing-result persistence (node-dimension uniqueness)
# ----------------------------------------------------------------------


def test_duplicate_message_is_idempotent(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    result = bearing("bearing_01", "edge_01", "normal", 0)

    assert repository.save_bearing_result(result, received_at_ns=1) is True
    assert repository.save_bearing_result(result, received_at_ns=2) is False


def test_same_bearing_slot_from_two_nodes_is_rejected(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    repository.save_bearing_result(
        bearing("bearing_01", "edge_01", "normal", 0), received_at_ns=1
    )

    with pytest.raises(BearingResultConflictError):
        repository.save_bearing_result(
            bearing("bearing_01", "edge_02", "fault", 3), received_at_ns=2
        )


def test_same_edge_node_cannot_submit_two_results(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    repository.save_bearing_result(
        bearing("bearing_01", "edge_01", "normal", 0), received_at_ns=1
    )

    with pytest.raises(BearingResultConflictError):
        repository.save_bearing_result(
            bearing("bearing_02", "edge_01", "fault", 3), received_at_ns=2
        )


def test_changed_payload_for_same_result_id_is_rejected(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    original = bearing("bearing_01", "edge_01", "normal", 0)
    repository.save_bearing_result(original, received_at_ns=1)
    changed = dict(original)
    changed["confidence"] = 0.5

    with pytest.raises(BearingResultConflictError):
        repository.save_bearing_result(changed, received_at_ns=2)


# ----------------------------------------------------------------------
# Window-result persistence and outbox fan-out
# ----------------------------------------------------------------------


def test_final_window_enqueues_publish_and_sync_and_suggestion(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = normal_window(repository)

    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        publish = connection.execute(
            "SELECT request_id, revision FROM summary_window_publish_outbox"
        ).fetchall()
        sync = connection.execute(
            "SELECT payload_json FROM summary_window_sync_outbox"
        ).fetchall()
        suggestion = connection.execute(
            "SELECT state FROM summary_suggestion_task"
        ).fetchall()
        arbitration = connection.execute(
            "SELECT COUNT(*) FROM summary_arbitration_outbox"
        ).fetchone()[0]

    assert len(publish) == 1
    assert publish[0]["revision"] == 1
    assert len(suggestion) == 1
    assert suggestion[0]["state"] == "PENDING"
    assert arbitration == 0
    sync_payload = json.loads(sync[0]["payload_json"])
    assert sync_payload["result_status"] == "FINAL"
    assert sync_payload["final_state"] == "normal"
    assert sync_payload["arbitration_status"] is None
    assert payload["revision"] == 1


def test_stale_incomplete_snapshot_cannot_replace_a_final_window(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    left = bearing("bearing_01", "edge_01", "normal", 0)
    right = bearing("bearing_02", "edge_02", "normal", 1)
    repository.save_bearing_result(left, received_at_ns=1_000)
    stale = build_incomplete_window_result([left], closed_at_ns=1_100)

    repository.save_bearing_result(right, received_at_ns=1_200)
    final = build_window_result([left, right], closed_at_ns=1_300)
    saved = repository.save_window_result(final)

    assert saved is not None
    assert repository.save_window_result(stale) is None
    current = repository.get_window_result(left["summary_window_id"])
    assert current is not None
    assert current["result_status"] == "FINAL"
    assert current["revision"] == 1


def test_conflict_window_enqueues_exactly_one_arbitration_request(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = conflict_window(repository)

    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT conflict_id, summary_result_id, revision, state "
            "FROM summary_arbitration_outbox"
        ).fetchall()
        suggestion = connection.execute(
            "SELECT COUNT(*) FROM summary_suggestion_task"
        ).fetchone()[0]

    assert len(rows) == 1
    assert rows[0]["conflict_id"] == payload["conflict_id"]
    assert rows[0]["state"] == "PENDING"
    assert suggestion == 0


def test_identical_resave_is_deduplicated_without_new_revision(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = normal_window(repository)
    rebuilt = build_window_result(
        repository.load_window_bearing_results(payload["summary_window_id"]),
        closed_at_ns=payload["closed_at_ns"],
    )

    assert repository.save_window_result(rebuilt) is None
    assert repository.list_window_results()[0]["revision"] == 1


def test_state_combination_metrics_are_tracked(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    normal_window(repository)  # seq 1: normal/normal
    conflict_window(repository)  # seq 2: normal/fault
    fault_result = window(
        repository,
        bearing("bearing_01", "edge_01", "fault", 3, seq=3),
        bearing("bearing_02", "edge_02", "fault", 2, seq=3),
    )
    repository.save_window_result(fault_result)

    metrics = repository.metrics()

    assert metrics["state_combinations"]["normal_normal"] == 1
    assert metrics["state_combinations"]["normal_fault"] == 1
    assert metrics["state_combinations"]["fault_fault"] == 1
    assert metrics["state_combinations"]["fault_normal"] == 0
    assert metrics["total_windows"] == 3
    assert metrics["eligible_windows"] == 3
    assert metrics["conflict_windows"] == 1
    assert metrics["conflict_rate"] == pytest.approx(1 / 3)


# ----------------------------------------------------------------------
# Cloud arbitration write-back
# ----------------------------------------------------------------------


def test_resolved_arbitration_marks_window_final_and_republishes(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = conflict_window(repository)

    arbitrated = repository.apply_arbitration_result(
        payload["summary_result_id"],
        {
            "arbitration_id": "arbitration_01",
            "status": "resolved",
            "final_state": "fault",
            "final_action": "shutdown",
            "confidence": 0.92,
        },
        now_ns=2_000,
    )

    assert arbitrated["result_status"] == "FINAL"
    assert arbitrated["arbitration_status"] == "RESOLVED"
    assert arbitrated["final_state"] == "fault"
    assert arbitrated["final_action"] == "shutdown"
    assert arbitrated["final_action_grade"] == 4
    assert arbitrated["recommended_action"] == "shutdown"
    assert arbitrated["final_source"] == "cloud_arbitration"
    assert arbitrated["arbitration_confidence"] == 0.92
    assert arbitrated["confidence"] == 0.92
    assert arbitrated["revision"] == 2
    assert arbitrated["has_conflict"] is True

    stored = repository.get_window_result_by_id(payload["summary_result_id"])
    assert stored["result_status"] == "FINAL"

    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        publish = connection.execute(
            "SELECT revision FROM summary_window_publish_outbox ORDER BY revision"
        ).fetchall()
        sync = connection.execute(
            "SELECT revision FROM summary_window_sync_outbox ORDER BY revision"
        ).fetchall()
        suggestion = connection.execute(
            "SELECT revision, state FROM summary_suggestion_task"
        ).fetchall()

    assert [row["revision"] for row in publish] == [1, 2]
    assert [row["revision"] for row in sync] == [1, 2]
    assert suggestion[0]["state"] == "PENDING"
    metrics = repository.metrics()
    assert metrics["counters"]["arbitration_resolved_windows"] == 1


def test_manual_review_outcome_moves_window_to_manual_review(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = conflict_window(repository)

    arbitrated = repository.apply_arbitration_result(
        payload["summary_result_id"],
        {
            "arbitration_id": "arbitration_02",
            "status": "manual_review",
            "final_state": None,
            "final_action": None,
        },
        now_ns=2_000,
    )

    assert arbitrated["result_status"] == "MANUAL_REVIEW"
    assert arbitrated["arbitration_status"] == "MANUAL_REVIEW"
    assert arbitrated["final_state"] is None
    assert arbitrated["revision"] == 2


def test_warning_final_state_requires_manual_review(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = conflict_window(repository)

    arbitrated = repository.apply_arbitration_result(
        payload["summary_result_id"],
        {
            "arbitration_id": "arbitration_03",
            "status": "resolved",
            "final_state": "warning",
            "final_action": "scheduled_inspection",
            "confidence": 0.8,
        },
        now_ns=2_000,
    )

    assert arbitrated["result_status"] == "MANUAL_REVIEW"
    assert arbitrated["final_state"] is None


def test_apply_rejects_windows_that_are_not_pending(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = normal_window(repository)

    with pytest.raises(ValueError, match="not pending arbitration"):
        repository.apply_arbitration_result(
            payload["summary_result_id"],
            {"arbitration_id": "arb_x", "status": "resolved", "final_state": "fault"},
            now_ns=2_000,
        )


def test_apply_is_idempotent_for_the_same_arbitration(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = conflict_window(repository)
    arbitration = {
        "arbitration_id": "arbitration_04",
        "status": "resolved",
        "final_state": "fault",
        "final_action": "shutdown",
        "confidence": 0.9,
    }

    first = repository.apply_arbitration_result(
        payload["summary_result_id"], arbitration, now_ns=2_000
    )
    repeated = repository.apply_arbitration_result(
        payload["summary_result_id"], arbitration, now_ns=3_000
    )

    assert repeated == first
    assert repository.list_window_results()[0]["revision"] == 2


def test_apply_rejects_a_different_arbitration_for_settled_window(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = conflict_window(repository)
    repository.apply_arbitration_result(
        payload["summary_result_id"],
        {
            "arbitration_id": "arbitration_05",
            "status": "resolved",
            "final_state": "fault",
            "final_action": "shutdown",
        },
        now_ns=2_000,
    )

    with pytest.raises(ValueError, match="not pending arbitration"):
        repository.apply_arbitration_result(
            payload["summary_result_id"],
            {
                "arbitration_id": "arbitration_06",
                "status": "resolved",
                "final_state": "normal",
            },
            now_ns=3_000,
        )


# ----------------------------------------------------------------------
# Suggestion tasks
# ----------------------------------------------------------------------


def test_suggestion_task_retry_and_dead_letter_lifecycle(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = normal_window(repository)

    tasks = repository.due_suggestion_tasks(now_ns=1_500)
    assert len(tasks) == 1
    assert tasks[0]["summary_result_id"] == payload["summary_result_id"]

    repository.defer_suggestion_task(
        payload["summary_result_id"],
        error="llm timeout",
        attempts=1,
        next_attempt_at_ns=2_000,
        dead_letter=False,
        now_ns=1_500,
    )
    assert repository.due_suggestion_tasks(now_ns=1_999) == []
    assert repository.due_suggestion_tasks(now_ns=2_000)

    repository.defer_suggestion_task(
        payload["summary_result_id"],
        error="llm timeout",
        attempts=5,
        next_attempt_at_ns=3_000,
        dead_letter=True,
        now_ns=2_500,
    )
    assert repository.due_suggestion_tasks(now_ns=9_999) == []
    metrics = repository.metrics()
    assert metrics["suggestion_tasks"]["dead_letter"] == 1


def test_completed_suggestion_task_publishes_once(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    payload = normal_window(repository)
    suggestion = {
        "result_id": "suggestion_01",
        "summary_result_id": payload["summary_result_id"],
        "created_at_ns": 2_000,
        "suggestion": "设备运行正常，继续运行。",
    }

    repository.complete_suggestion_task(
        payload["summary_result_id"], suggestion, now_ns=2_000
    )
    repository.complete_suggestion_task(
        payload["summary_result_id"], suggestion, now_ns=2_500
    )

    with sqlite3.connect(repository.database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT revision, state FROM summary_suggestion_outbox"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "PENDING"
    assert repository.get_suggestion(payload["summary_result_id"])["result_id"] == (
        "suggestion_01"
    )


# ----------------------------------------------------------------------
# Schema migration
# ----------------------------------------------------------------------


def test_legacy_v1_database_is_migrated_with_backup(tmp_path):
    database_path = tmp_path / "summary.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE summary_bearing_result (
            result_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            window_start_sequence INTEGER NOT NULL,
            window_end_sequence INTEGER NOT NULL,
            bearing_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            received_at_ns INTEGER NOT NULL
        );
        CREATE TABLE summary_window_result (
            summary_result_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            window_start_sequence INTEGER NOT NULL,
            window_end_sequence INTEGER NOT NULL,
            result_status TEXT NOT NULL,
            has_conflict INTEGER NOT NULL,
            excluded_from_formal_metrics INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL
        );
        """
    )
    legacy_payload = {
        "device_id": "machine_01",
        "run_id": None,
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "edge_node_id": "edge_01",
        "decision_round_id": "round_01",
    }
    connection.execute(
        "INSERT INTO summary_bearing_result VALUES (?,?,?,?,?,?,?)",
        ("result_01", "machine_01", 1, 1, "bearing_01",
         json.dumps(legacy_payload), 1_000),
    )
    connection.execute(
        "INSERT INTO summary_window_result VALUES (?,?,?,?,?,?,?,?,?)",
        ("summary_01", "machine_01", 1, 1, "FINAL", 0, 0,
         json.dumps(legacy_payload), 1_200),
    )
    connection.commit()
    connection.close()

    repository = SummaryRepository(database_path)

    assert (tmp_path / "summary.db.v1.bak").exists()
    with sqlite3.connect(database_path) as migrated:
        version = migrated.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            item[1] for item in migrated.execute(
                "PRAGMA table_info(summary_bearing_result)"
            )
        }
        row = migrated.execute(
            "SELECT summary_window_id, edge_node_id FROM summary_bearing_result "
            "WHERE result_id='result_01'"
        ).fetchone()
    assert version == 2
    assert {"summary_window_id", "edge_node_id", "decision_round_id"} <= columns
    assert row[1] == "edge_01"
    assert row[0].startswith("summary_window_")


def test_fresh_database_starts_at_schema_version_two(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")

    with sqlite3.connect(repository.database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    assert (tmp_path / "summary.db.v1.bak").exists() is False
