from __future__ import annotations

import pytest

from edge_status_reporter.contracts import AcceleratorSnapshot, EdgeStatusReport, ModelStatus, ResourceSnapshot


def test_report_serializes_all_required_status_dimensions() -> None:
    report = EdgeStatusReport(
        edge_node_id="edge_01",
        reported_at_ns=123,
        resources=ResourceSnapshot(4, 25.5, 2048.0),
        accelerators=AcceleratorSnapshot(False, True),
        queue_length=0,
        models=(ModelStatus("edge_bearing_mock", "loaded"),),
        last_task_activity_ns=100,
    )
    assert report.as_dict() == {
        "edge_node_id": "edge_01",
        "reported_at_ns": 123,
        "resources": {
            "logical_cpu_count": 4,
            "cpu_utilization_percent": 25.5,
            "memory_available_mb": 2048.0,
            "gpu_available": False,
            "npu_available": True,
            "queue_length": 0,
        },
        "models": [{"model_version": "edge_bearing_mock", "load_status": "LOADED"}],
        "last_task_activity_ns": 100,
    }


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: ResourceSnapshot(0, 1.0, 1.0), "logical_cpu_count"),
        (lambda: ResourceSnapshot(1, 100.1, 1.0), "cpu_utilization_percent"),
        (lambda: ResourceSnapshot(1, 1.0, -1.0), "memory_available_mb"),
        (lambda: ModelStatus("model", "UNKNOWN"), "load_status"),
    ],
)
def test_invalid_contract_values_are_rejected(factory, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        factory()
