from __future__ import annotations

import hashlib
import json

from cloud_service.raw_analysis import RawAnalysisSampleService, SignalAnalysisWorker


def _payload() -> bytes:
    return json.dumps({"packets": [{
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_01",
        "sender_id": "sender_01", "packet_id": "packet_001", "sequence_number": 1,
        "end_generate_timestamp_ns": 50_000_000,
        "data": {"vibration": {"sample_rate_hz": 64_000, "sample_count": 3_200, "values": [0.1] * 3_200}},
    }]} , sort_keys=True, separators=(",", ":")).encode()


def _metadata(payload: bytes) -> dict:
    return {
        "schema_version": "raw-analysis-sample/1.0", "sample_id": "ras_001",
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_01", "sender_id": "sender_01",
        "decision_round_id": "round_001", "trigger_reasons": ["LOW_CONFIDENCE"], "sample_type": "event",
        "requested_window_ms": 50, "actual_window_ms": 50, "complete": True, "missing_duration_ms": 0,
        "window_start_ns": 0, "window_end_ns": 50_000_000,
        "packet_manifest": [{"packet_id": "packet_001", "sequence_number": 1, "end_generate_timestamp_ns": 50_000_000, "sample_rate_hz": 64_000, "sample_count": 3_200}],
        "sample_rate_hz": 64_000, "sample_count": 3_200, "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "edge_model_version": "edge_model_v1", "cloud_corrected": None, "created_at_ns": 50_000_000,
    }


def test_raw_sample_service_is_checksum_idempotent_and_worker_persists_physical_evidence(tmp_path) -> None:
    payload = _payload()
    service = RawAnalysisSampleService(tmp_path / "cloud.db")

    assert service.accept(_metadata(payload), payload, received_at_ns=60_000_000)["status"] == "accepted"
    assert service.accept(_metadata(payload), payload, received_at_ns=61_000_000)["status"] == "duplicate"
    different_payload = payload + b" "
    assert service.accept(
        _metadata(different_payload), different_payload, received_at_ns=62_000_000
    )["status"] == "conflict"

    assert SignalAnalysisWorker(service).run_once(now_ns=70_000_000) == 1
    evidence = service.get_evidence("ras_001")
    assert evidence is not None and evidence["status"] == "SUCCEEDED"
    assert "final_diagnosis" not in evidence["result"]
    assert "recommended_action" not in evidence["result"]
