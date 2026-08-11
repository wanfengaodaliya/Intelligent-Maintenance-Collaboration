from pathlib import Path

from cloud_service.global_analysis.service import GlobalAnalysisService
from scenarios.bearing.cloud.global_analysis.data_source import FakeGlobalAnalysisDataSource


def test_service_persists_v2_result_and_marks_missing_packet_history_not_available(tmp_path: Path):
    source = FakeGlobalAnalysisDataSource(
        device_tasks=[
            {"device_id": "machine_01", "task_id": "t1", "final_state": "normal", "has_conflict": False, "completed_at_ns": 1}
        ]
    )
    service = GlobalAnalysisService(tmp_path / "cloud.db", data_source=source)
    result = service.analyze("bearing", "machine_01", 20)
    assert result["schema_version"] == "global_analysis_result/2.0"
    assert result["packet_diagnosis_analysis"]["status"] == "not_available"
    assert service.repository.get_latest("bearing", "machine_01")["analysis_id"] == result["analysis_id"]
