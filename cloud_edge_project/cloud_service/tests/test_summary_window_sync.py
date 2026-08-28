from __future__ import annotations

import json
import sqlite3

import pytest

from cloud_service.device_arbitration.errors import ArbitrationPayloadConflictError
from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from cloud_service.global_analysis.v12_data_source import V12GlobalAnalysisDataSource
from cloud_service.global_analysis.periodic import list_subject_ids
from cloud_service.summary_windows import SummaryWindowRepository
from core.diagnosis_identity import build_summary_window_id


def window_payload(
    summary_result_id: str,
    *,
    sequence: int,
    conflict: bool,
    excluded: bool = False,
) -> dict:
    run_id = f"run_{sequence:02d}"
    if excluded:
        result_status = "INCOMPLETE"
        node_states = {"edge_01": "normal"}
        action_levels_by_edge = {"edge_01": 0}
        action_scores_by_edge = {"edge_01": 0.0}
        max_action_level_gap = 0
        max_action_score_gap = 0.0
        final_action_level = None
        recommended_action = None
    elif conflict:
        result_status = "PENDING_ARBITRATION"
        node_states = {"edge_01": "normal", "edge_02": "fault"}
        action_levels_by_edge = {"edge_01": 0, "edge_02": 3}
        action_scores_by_edge = {"edge_01": 0.0, "edge_02": 1.0}
        max_action_level_gap = 3
        max_action_score_gap = 1.0
        final_action_level = None
        recommended_action = None
    else:
        result_status = "FINAL"
        node_states = {"edge_01": "normal", "edge_02": "normal"}
        action_levels_by_edge = {"edge_01": 0, "edge_02": 1}
        action_scores_by_edge = {"edge_01": 0.0, "edge_02": 0.35}
        max_action_level_gap = 1
        max_action_score_gap = 0.35
        final_action_level = 1
        recommended_action = "enhanced_monitoring"
    return {
        "summary_result_id": summary_result_id,
        "summary_window_id": build_summary_window_id(
            device_id="machine_01",
            run_id=run_id,
            window_start_sequence=sequence,
            window_end_sequence=sequence,
        ),
        "device_id": "machine_01",
        "run_id": run_id,
        "window_start_sequence": sequence,
        "window_end_sequence": sequence,
        "result_status": result_status,
        "revision": 1,
        "has_conflict": conflict,
        "conflict_semantics": "action_level_gap_v1",
        "action_scorer_version": "action_scorer_v1",
        "final_decision_semantics": "action_derived_v1",
        "state_mismatch": conflict and not excluded,
        "state_mismatch_pair_count": 1 if (conflict and not excluded) else 0,
        "node_states": node_states,
        "final_state": None if (conflict or excluded) else "normal",
        "arbitration_status": "PENDING" if conflict else None,
        "excluded_from_formal_metrics": excluded,
        "conflicting_pair_count": 1 if conflict else 0,
        "action_levels_by_edge": action_levels_by_edge,
        "action_scores_by_edge": action_scores_by_edge,
        "max_action_level_gap": max_action_level_gap,
        "max_action_score_gap": max_action_score_gap,
        "final_action_level": final_action_level,
        "recommended_action": recommended_action,
        "closed_at_ns": sequence * 100,
    }


def test_cloud_summary_window_storage_is_idempotent_and_rejects_identity_reuse(tmp_path):
    repository = SummaryWindowRepository(tmp_path / "cloud.db")
    payload = window_payload("summary_01", sequence=1, conflict=False)

    assert repository.accept(payload) == payload
    assert repository.accept(payload) == payload
    changed = dict(payload)
    changed["max_action_level_gap"] = 1
    with pytest.raises(ArbitrationPayloadConflictError):
        repository.accept(changed)
    assert repository.list_recent(device_id="machine_01") == [payload]


def test_higher_revision_replaces_window_after_arbitration(tmp_path):
    repository = SummaryWindowRepository(tmp_path / "cloud.db")
    payload = window_payload("summary_02", sequence=2, conflict=True)
    assert repository.accept(payload) == payload

    resolved = dict(payload)
    resolved.update(
        {
            "result_status": "FINAL",
            "revision": 2,
            "final_state": "fault",
            "arbitration_status": "RESOLVED",
            "final_action_level": 3,
            "recommended_action": "shutdown",
        }
    )
    assert repository.accept(resolved) == resolved

    stale = dict(payload)
    stale["revision"] = 1
    with pytest.raises(ArbitrationPayloadConflictError):
        repository.accept(stale)
    assert repository.list_recent(device_id="machine_01") == [resolved]


def test_conflict_flag_must_match_action_level_gap(tmp_path):
    repository = SummaryWindowRepository(tmp_path / "cloud.db")
    payload = window_payload("summary_03", sequence=3, conflict=True)
    # Keep a self-consistent level-gap-1 window but has_conflict=True.
    payload["action_levels_by_edge"] = {"edge_01": 0, "edge_02": 1}
    payload["action_scores_by_edge"] = {"edge_01": 0.0, "edge_02": 0.35}
    payload["max_action_level_gap"] = 1
    payload["max_action_score_gap"] = 0.35
    payload["conflicting_pair_count"] = 0

    with pytest.raises(ValueError, match="has_conflict must equal"):
        repository.accept(payload)


def test_same_sequence_in_different_runs_creates_distinct_cloud_windows(tmp_path):
    repository = SummaryWindowRepository(tmp_path / "cloud.db")
    first = window_payload("summary_run_01", sequence=1, conflict=False)
    second = dict(first)
    second.update(
        {
            "summary_result_id": "summary_run_02",
            "run_id": "run_02",
            "summary_window_id": build_summary_window_id(
                device_id="machine_01",
                run_id="run_02",
                window_start_sequence=1,
                window_end_sequence=1,
            ),
            "closed_at_ns": 200,
        }
    )

    repository.accept(first)
    repository.accept(second)

    assert {item["run_id"] for item in repository.list_recent()} == {
        "run_01",
        "run_02",
    }


def test_legacy_grade_conflict_is_quarantined_during_identity_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    payload = {
        "summary_result_id": "legacy_summary",
        "device_id": "machine_01",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "result_status": "PENDING_ARBITRATION",
        "has_conflict": True,
        "excluded_from_formal_metrics": False,
        "max_cross_edge_grade_gap": 3,
        "conflicting_pair_count": 1,
        "closed_at_ns": 100,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE summary_window_record (
                summary_result_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                window_start_sequence INTEGER NOT NULL,
                window_end_sequence INTEGER NOT NULL,
                result_status TEXT NOT NULL,
                has_conflict INTEGER NOT NULL,
                excluded_from_formal_metrics INTEGER NOT NULL,
                max_cross_edge_grade_gap INTEGER NOT NULL,
                conflicting_pair_count INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                UNIQUE(device_id, window_start_sequence, window_end_sequence)
            );
            """
        )
        connection.execute(
            "INSERT INTO summary_window_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_summary",
                "machine_01",
                1,
                1,
                "PENDING_ARBITRATION",
                1,
                0,
                3,
                1,
                "legacy_hash",
                payload_json,
                100,
            ),
        )

    repository = SummaryWindowRepository(database_path)
    migrated = repository.list_recent()[0]
    assert migrated["conflict_semantics"] == "legacy_grade_gap"
    assert migrated["state_mismatch"] is False
    assert migrated["excluded_from_formal_metrics"] is True

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT conflict_semantics, state_mismatch, excluded_from_formal_metrics "
            "FROM summary_window_record"
        ).fetchone()
    assert row == ("legacy_grade_gap", 0, 1)


def test_global_analysis_uses_summary_windows_for_formal_consistency_metrics(tmp_path):
    repository = SummaryWindowRepository(tmp_path / "cloud.db")
    repository.accept(window_payload("summary_01", sequence=1, conflict=False))
    repository.accept(window_payload("summary_02", sequence=2, conflict=True))
    repository.accept(window_payload("summary_03", sequence=3, conflict=False, excluded=True))

    loaded = V12GlobalAnalysisDataSource(tmp_path / "cloud.db").load("machine_01", 20)
    analysis = analyze_device_arbitration(
        loaded["summary_windows"], loaded["arbitrations"], GlobalAnalysisConfig()
    )

    assert analysis["complete_window_count"] == 2
    assert analysis["incomplete_window_count"] == 1
    assert analysis["conflict_count"] == 1
    assert analysis["conflict_rate"] == pytest.approx(0.5)
    assert analysis["consistency_rate"] == pytest.approx(0.5)
    assert analysis["max_decision_gap"] == 3
    assert list_subject_ids(tmp_path / "cloud.db") == ["machine_01"]
