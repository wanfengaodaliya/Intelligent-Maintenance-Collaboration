from __future__ import annotations

import pytest

from cloud_service.device_arbitration.errors import ArbitrationPayloadConflictError
from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from cloud_service.global_analysis.v12_data_source import V12GlobalAnalysisDataSource
from cloud_service.global_analysis.periodic import list_subject_ids
from cloud_service.summary_windows import SummaryWindowRepository


def window_payload(
    summary_result_id: str,
    *,
    sequence: int,
    conflict: bool,
    excluded: bool = False,
) -> dict:
    return {
        "summary_result_id": summary_result_id,
        "device_id": "machine_01",
        "window_start_sequence": sequence,
        "window_end_sequence": sequence,
        "result_status": "INCOMPLETE" if excluded else "PENDING_ARBITRATION" if conflict else "FINAL",
        "has_conflict": conflict,
        "excluded_from_formal_metrics": excluded,
        "max_cross_edge_grade_gap": 3 if conflict else 0,
        "conflicting_pair_count": 1 if conflict else 0,
        "closed_at_ns": sequence * 100,
    }


def test_cloud_summary_window_storage_is_idempotent_and_rejects_identity_reuse(tmp_path):
    repository = SummaryWindowRepository(tmp_path / "cloud.db")
    payload = window_payload("summary_01", sequence=1, conflict=False)

    assert repository.accept(payload) == payload
    assert repository.accept(payload) == payload
    changed = dict(payload)
    changed["max_cross_edge_grade_gap"] = 1
    with pytest.raises(ArbitrationPayloadConflictError):
        repository.accept(changed)
    assert repository.list_recent(device_id="machine_01") == [payload]


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
