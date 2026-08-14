from __future__ import annotations

import json

from cloud_service.raw_analysis import RawAnalysisSampleService, SignalAnalysisWorker
from raw_sample_capture import (
    CaptureDecision,
    RawAnalysisSampleUploader,
    RawSampleFreezer,
    RawSampleRepository,
)


def test_offline_edge_queue_recovers_to_cloud_physical_evidence(tmp_path) -> None:
    packet = {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_01",
        "sender_id": "sender_01", "packet_id": "packet_001", "sequence_number": 1,
        "end_generate_timestamp_ns": 50_000_000,
        "data": {"vibration": {"sample_rate_hz": 64_000, "sample_count": 3_200, "values": [0.1] * 3_200}},
    }
    sample = RawSampleFreezer().freeze(
        CaptureDecision(
            device_id="machine_01", task_id="task_001", bearing_id="bearing_01",
            sender_id="sender_01", decision_round_id="round_001",
            trigger_reasons=("LOW_CONFIDENCE",), sample_type="event", requested_window_ms=50,
            edge_model_version="edge_model_v1", cloud_corrected=None, created_at_ns=50_000_000,
        ),
        (packet,),
    )
    edge_repository = RawSampleRepository(tmp_path / "edge-queue")
    edge_repository.enqueue(sample)
    offline = RawAnalysisSampleUploader(
        edge_repository, lambda _metadata, _payload: (_ for _ in ()).throw(TimeoutError("offline"))
    )
    assert offline.run_once(now_ns=1).retried == 1

    cloud = RawAnalysisSampleService(tmp_path / "cloud.db")
    recovered = RawAnalysisSampleUploader(
        edge_repository,
        lambda metadata, body: cloud.accept(metadata, body, received_at_ns=2_000_000_000),
    )
    assert recovered.run_once(now_ns=1_000_000_001).acknowledged == 1
    assert SignalAnalysisWorker(cloud).run_once(now_ns=3_000_000_000) == 1
    assert cloud.get_evidence(sample.sample_id)["status"] == "SUCCEEDED"
