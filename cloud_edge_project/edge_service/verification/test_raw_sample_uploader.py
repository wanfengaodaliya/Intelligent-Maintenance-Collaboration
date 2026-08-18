from __future__ import annotations

from raw_sample_capture import (
    CaptureDecision,
    RawAnalysisSampleUploader,
    RawSampleFreezer,
    RawSampleRepository,
)


def _sample():
    packet = {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_01",
        "sender_id": "sender_01", "packet_id": "packet_001", "sequence_number": 1,
        "end_generate_timestamp_ns": 50_000_000,
        "data": {"vibration": {"sample_rate_hz": 64_000, "sample_count": 3_200, "values": [0.0] * 3_200}},
    }
    decision = CaptureDecision(
        device_id="machine_01", task_id="task_001", bearing_id="bearing_01", sender_id="sender_01",
        decision_round_id="round_001", trigger_reasons=("LOW_CONFIDENCE",), sample_type="event",
        requested_window_ms=50, edge_model_version="edge_model_v1", cloud_corrected=None, created_at_ns=50_000_000,
    )
    return RawSampleFreezer().freeze(decision, (packet,))


def test_uploader_acknowledges_accepted_and_duplicate_but_dead_letters_conflict(tmp_path) -> None:
    repository = RawSampleRepository(tmp_path / "queue")
    sample = _sample()
    repository.enqueue(sample)
    replies = iter(({"status": "accepted"}, {"status": "duplicate"}, {"status": "conflict"}))
    uploader = RawAnalysisSampleUploader(repository, lambda _metadata, _payload: next(replies))

    assert uploader.run_once(now_ns=1).acknowledged == 1
    assert repository.get(sample.sample_id).status == "ACKNOWLEDGED"
    repository.enqueue(sample.with_payload(b'{"new":1}'))
    assert repository.get(sample.sample_id).status == "CONFLICT"


def test_uploader_retains_network_failures_with_bounded_exponential_backoff(tmp_path) -> None:
    repository = RawSampleRepository(tmp_path / "queue")
    sample = _sample()
    repository.enqueue(sample)

    uploader = RawAnalysisSampleUploader(
        repository, lambda _metadata, _payload: (_ for _ in ()).throw(TimeoutError("offline")), max_backoff_seconds=4
    )

    outcome = uploader.run_once(now_ns=100)
    queued = repository.get(sample.sample_id)
    assert outcome.retried == 1
    assert queued.status == "PENDING"
    assert queued.attempt_count == 1
    assert queued.next_attempt_at_ns == 1_000_000_100
    assert queued.last_error == "TimeoutError: offline"
