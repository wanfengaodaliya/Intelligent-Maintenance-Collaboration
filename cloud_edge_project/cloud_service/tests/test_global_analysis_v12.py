from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cloud_service.global_analysis.service import GlobalAnalysisService
from cloud_service.moment_review_repository import MomentReviewRepository
from cloud_service.raw_analysis.service import RawAnalysisSampleService
from cloud_service.task_results import TaskResultService


def _bearing(*, result_id: str, revision: int, state: str, replaces: str | None, created_at_ns: int) -> dict:
    return {
        "result_id": result_id, "revision": revision, "replaces_result_id": replaces,
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "decision_round_id": "round_001", "diagnosis_window_id": "dw_001",
        "lifecycle_state": state, "bearing_state": "fault" if revision == 2 else "normal",
        "confidence": .8, "data_quality_score": .9, "risk_level": "high" if revision == 2 else "low",
        "action_grade": 3 if revision == 2 else 0, "recommended_action": "urgent_intervention" if revision == 2 else "continue_operation",
        "decision_source": "CLOUD" if revision == 2 else "PROVISIONAL_EDGE",
        "review_status": "REVIEWED" if revision == 2 else "PENDING_CLOUD", "degraded": revision == 1,
        "edge_result_id": "edge_dw_001", "cloud_result_id": "cloud_dw_001" if revision == 2 else None,
        "model_version": "cloud_model_v1" if revision == 2 else "edge_model_v1",
        "created_at_ns": created_at_ns, "edge_accepted_at_ns": 10,
    }


def _device(*, result_id: str, revision: int, replaces: str | None) -> dict:
    return {
        "result_id": result_id, "revision": revision, "replaces_result_id": replaces,
        "device_id": "machine_01", "task_id": "task_001", "decision_round_id": "round_001",
        "expected_bearing_ids": ["bearing_a"], "received_bearing_ids": ["bearing_a"],
        "missing_bearing_ids": [], "bearing_result_ids": ["bearing_round_r2"],
        "status": "FINAL", "closure_reason": "ROUND_TIMEOUT", "final_state": "fault",
        "final_action_grade": 3, "final_action": "urgent_intervention", "confidence": .8,
        "data_quality_score": .9, "has_conflict": False, "conflict_reasons": [],
        "decision_source": "EDGE", "degraded": True, "affects_realtime_action": True,
        "arbitration_id": None, "closed_at_ns": 30 + revision, "created_at_ns": 30 + revision,
    }


def _add_evidence(database_path: Path) -> None:
    service = RawAnalysisSampleService(database_path)
    payload = json.dumps({"packets": []}, separators=(",", ":")).encode()
    metadata = {
        "schema_version": "raw-analysis-sample/1.0", "sample_id": "sample_001",
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "decision_round_id": "round_001",
        "trigger_reasons": ["LOW_CONFIDENCE"], "sample_type": "event",
        "requested_window_ms": 1000, "actual_window_ms": 950, "complete": False,
        "missing_duration_ms": 50, "window_start_ns": 1, "window_end_ns": 2,
        "packet_manifest": [{"sequence_number": 1}], "sample_rate_hz": 64_000,
        "sample_count": 1, "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "edge_model_version": "edge_model_v1", "cloud_corrected": True, "created_at_ns": 40,
    }
    assert service.accept(metadata, payload, received_at_ns=41) == {"status": "accepted"}
    row = service.claim_pending()
    assert row is not None
    service.complete(row, {
        "schema_version": "physical-evidence-result/1.0", "sample_id": "sample_001",
        "time_domain": {"rms": 2.0}, "frequency_domain": {"dominant_peaks_hz": [100.0]},
        "envelope_spectrum": {"peaks": []}, "bearing_frequency_evidence": [], "limitations": [],
    }, now_ns=42)


def test_default_global_analysis_uses_current_v12_revisions_and_physical_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"
    results = TaskResultService(database_path)
    assert results.ingest_bearing_decision(_bearing(result_id="bearing_round_r1", revision=1, state="PROVISIONAL", replaces=None, created_at_ns=11))["duplicate"] is False
    assert results.ingest_bearing_decision(_bearing(result_id="bearing_round_r2", revision=2, state="LATE_CLOUD_CORRECTED", replaces="bearing_round_r1", created_at_ns=20))["duplicate"] is False
    assert results.ingest_device_decision(_device(result_id="device_round_r1", revision=1, replaces=None))["duplicate"] is False
    assert results.ingest_device_decision(_device(result_id="device_round_r2", revision=2, replaces="device_round_r1"))["duplicate"] is False
    _add_evidence(database_path)

    result = GlobalAnalysisService(database_path).analyze("bearing", "machine_01")

    assert result["analysis_window"]["actual_task_count"] == 1
    assert result["revision_deduplication"] == {
        "bearing_revision_record_count": 2, "current_bearing_result_count": 1,
        "superseded_bearing_revision_count": 1, "device_revision_record_count": 2,
        "current_device_result_count": 1, "superseded_device_revision_count": 1,
    }
    assert result["round_closure_analysis"] == {
        "round_timeout_count": 1, "late_bearing_revision_count": 1,
    }
    assert "bearing_aggregation_analysis" not in result
    assert "cloud_bearing_review_analysis" not in result
    assert result["physical_evidence_analysis"]["event_sample_count"] == 1
    assert result["physical_evidence_analysis"]["partial_sample_count"] == 1
    assert result["physical_evidence_analysis"]["multi_scale"]["edge_feature_window_ms"] == 50


def test_global_analysis_loads_packet_review_pairs_from_moment_record(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"
    results = TaskResultService(database_path)
    assert results.ingest_device_decision(_device(result_id="device_round_r2", revision=2, replaces=None))["duplicate"] is False
    MomentReviewRepository(database_path).save(
        {
            "review_id": "moment_dw_001",
            "result_id": "cloud_dw_001",
            "schema_version": "cloud-bearing-result/2.0",
            "device_id": "machine_01",
            "task_id": "task_001",
            "bearing_id": "bearing_a",
            "sender_id": "sender_a",
            "decision_round_id": "round_001",
            "diagnosis_window_id": "dw_001",
            "window_start_sequence": 1,
            "window_end_sequence": 1,
            "window_start_ns": 1,
            "window_end_ns": 2,
            "bearing_state": "fault",
            "edge_label": "normal",
            "confidence": 0.9,
            "data_quality_score": 0.9,
            "risk_level": "high",
            "action_grade": 3,
            "recommended_action": "urgent_intervention",
            "model_version": "edge_model_v1",
            "created_at_ns": 10,
        }
    )
    _add_evidence(database_path)

    result = GlobalAnalysisService(database_path).analyze("bearing", "machine_01")

    diagnosis = result["packet_diagnosis_analysis"]
    assert diagnosis["reviewed_packet_count"] == 1
    assert diagnosis["edge_cloud_agreement_count"] == 0
    assert diagnosis["cloud_correction_count"] == 1
    assert diagnosis["risk_underestimation_count"] == 1
