from scenarios.bearing.cloud.global_analysis.data_source import FakeGlobalAnalysisDataSource


def test_fake_source_limits_tasks_and_filters_related_rows():
    source = FakeGlobalAnalysisDataSource(
        device_tasks=[
            {"device_id": "machine_01", "task_id": "t1", "completed_at_ns": 1},
            {"device_id": "machine_01", "task_id": "t2", "completed_at_ns": 2},
        ],
        bearing_tasks=[
            {"device_id": "machine_01", "task_id": "t1"},
            {"device_id": "machine_01", "task_id": "t2"},
        ],
    )

    data = source.load("machine_01", 1)

    assert [row["task_id"] for row in data["device_tasks"]] == ["t2"]
    assert [row["task_id"] for row in data["bearing_tasks"]] == ["t2"]
    assert data["packet_review_pairs"] == []
