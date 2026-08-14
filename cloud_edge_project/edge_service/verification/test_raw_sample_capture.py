from __future__ import annotations

from raw_sample_capture import (
    CaptureDecision,
    RawSampleCapturePolicy,
    RawSampleFreezer,
    RawSampleRepository,
    RawSampleCaptureService,
)


def _packet(sequence_number: int, *, task_id: str = "task_001") -> dict:
    end_ns = sequence_number * 50_000_000
    return {
        "device_id": "machine_01",
        "task_id": task_id,
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "packet_id": f"packet_{sequence_number:03d}",
        "sequence_number": sequence_number,
        "end_generate_timestamp_ns": end_ns,
        "data": {
            "vibration": {
                "sample_rate_hz": 64_000,
                "sample_count": 3_200,
                "values": [float(sequence_number)] * 3_200,
            }
        },
    }


def _decision(*, requested_window_ms: int = 100) -> CaptureDecision:
    return CaptureDecision(
        device_id="machine_01",
        task_id="task_001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        decision_round_id="round_001",
        trigger_reasons=("LOW_CONFIDENCE",),
        sample_type="event",
        requested_window_ms=requested_window_ms,
        edge_model_version="edge_model_v1",
        cloud_corrected=None,
        created_at_ns=100_000_000,
    )


def test_policy_captures_documented_events_but_not_cloud_correction() -> None:
    policy = RawSampleCapturePolicy(low_confidence_threshold=0.8)

    decision = policy.evaluate(
        {
            "device_id": "machine_01",
            "task_id": "task_001",
            "bearing_id": "bearing_01",
            "sender_id": "sender_01",
            "decision_round_id": "round_001",
            "confidence": 0.7,
            "route": "CLOUD_NOW",
            "risk_level": "high",
            "edge_model_version": "edge_model_v1",
            "created_at_ns": 100,
        }
    )

    assert decision is not None
    assert decision.trigger_reasons == (
        "LOW_CONFIDENCE",
        "CLOUD_ROUTE_REQUESTED",
        "ABNORMAL_OR_HIGH_RISK",
    )
    assert policy.evaluate(
        {
            "device_id": "machine_01",
            "task_id": "task_001",
            "bearing_id": "bearing_01",
            "sender_id": "sender_01",
            "decision_round_id": "round_001",
            "cloud_corrected": True,
            "edge_model_version": "edge_model_v1",
            "created_at_ns": 100,
        }
    ) is None


def test_freezer_builds_deterministic_complete_manifest_and_checksum() -> None:
    freezer = RawSampleFreezer()

    first = freezer.freeze(_decision(), (_packet(1), _packet(2)))
    second = freezer.freeze(_decision(), (_packet(1), _packet(2)))

    assert first.complete is True
    assert first.actual_window_ms == 100
    assert first.missing_duration_ms == 0
    assert first.window_start_ns == 0
    assert first.window_end_ns == 100_000_000
    assert first.sample_count == 6_400
    assert [item["packet_id"] for item in first.packet_manifest] == [
        "packet_001",
        "packet_002",
    ]
    assert first.sample_id == second.sample_id
    assert first.payload_sha256 == second.payload_sha256
    assert first.payload == second.payload


def test_freezer_keeps_partial_data_without_zero_padding_or_cross_task_packets() -> None:
    sample = RawSampleFreezer().freeze(
        _decision(), (_packet(1), _packet(2, task_id="other_task"))
    )

    assert sample.complete is False
    assert sample.actual_window_ms == 50
    assert sample.missing_duration_ms == 50
    assert sample.sample_count == 3_200
    assert [item["packet_id"] for item in sample.packet_manifest] == ["packet_001"]
    assert b"other_task" not in sample.payload


def test_repository_is_idempotent_marks_conflicts_and_recovers_pending_uploads(tmp_path) -> None:
    repository = RawSampleRepository(tmp_path / "raw-samples")
    sample = RawSampleFreezer().freeze(_decision(), (_packet(1), _packet(2)))

    assert repository.enqueue(sample).status == "INSERTED"
    assert repository.enqueue(sample).status == "DUPLICATE"
    assert repository.mark_uploading(sample.sample_id) is True

    restarted = RawSampleRepository(tmp_path / "raw-samples")
    restored = restarted.get(sample.sample_id)
    assert restored is not None and restored.status == "PENDING"
    assert restarted.read_payload(sample.sample_id) == sample.payload

    conflicting = sample.with_payload(b'{"different":true}')
    assert restarted.enqueue(conflicting).status == "CONFLICT"
    assert restarted.get(sample.sample_id).status == "CONFLICT"


def test_service_freezes_buffered_history_after_route_event(tmp_path) -> None:
    repository = RawSampleRepository(tmp_path / "raw-samples")
    service = RawSampleCaptureService(
        RawSampleCapturePolicy(history_window_ms=100), RawSampleFreezer(), repository
    )
    service.record_packet(_packet(1))
    service.record_packet(_packet(2))

    outcome = service.capture(
        {
            "device_id": "machine_01",
            "task_id": "task_001",
            "bearing_id": "bearing_01",
            "sender_id": "sender_01",
            "decision_round_id": "round_001",
            "confidence": 0.7,
            "route": "EDGE",
            "bearing_state": "normal",
            "risk_level": "low",
            "edge_model_version": "edge_model_v1",
            "created_at_ns": 100_000_000,
        }
    )

    assert outcome is not None and outcome.status == "INSERTED"
    assert repository.get(outcome.sample_id).sample.complete is True


def test_repository_expires_old_payloads_and_enforces_quota_without_dropping_pending(tmp_path) -> None:
    first = RawSampleFreezer().freeze(_decision(requested_window_ms=50), (_packet(1),))
    second_decision = CaptureDecision(
        **{**_decision(requested_window_ms=50).__dict__,
           "decision_round_id": "round_002", "created_at_ns": 200_000_000}
    )
    second = RawSampleFreezer().freeze(second_decision, (_packet(2),))
    repository = RawSampleRepository(
        tmp_path / "quota", max_storage_bytes=len(first.payload) + 8,
        retention_ns=100_000_000,
    )

    assert repository.enqueue(first).status == "INSERTED"
    assert repository.enqueue(second).status == "EXPIRED"
    assert repository.get(first.sample_id).status == "PENDING"
    assert repository.get(second.sample_id).status == "EXPIRED"

    assert repository.cleanup(now_ns=first.created_at_ns + 100_000_000) == 1
    assert repository.get(first.sample_id).status == "EXPIRED"
    assert repository.read_payload(first.sample_id) is None
