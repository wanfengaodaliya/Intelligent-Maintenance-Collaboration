from pathlib import Path

from cloud_service.task_results import TaskResultService


def test_task_result_ingestion_is_idempotent(tmp_path: Path):
    service = TaskResultService(tmp_path / "cloud.db")
    bearing = {
        "device_id": "device_01", "task_id": "task_01", "bearing_id": "bearing_01",
        "edge_state": "warning", "edge_confidence": 0.6, "packet_count": 20,
        "source_packet_manifest": [{"packet_id": "p", "sequence_number": 1}],
        "bearing_state": "warning", "result_source": "edge", "model_version": "bearing_packet_model_v1",
    }
    first = service.ingest_bearing(bearing)
    assert service.ingest_bearing(bearing) == first
    assert first["status"] == "accepted"

    device = {"device_id": "device_01", "task_id": "task_01", "final_state": "warning", "confidence": 0.7, "has_conflict": False}
    assert service.ingest_device(device)["status"] == "accepted"
