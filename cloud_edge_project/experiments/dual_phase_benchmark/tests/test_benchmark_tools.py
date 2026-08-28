from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import build_manifest  # noqa: E402
import dpb_common  # noqa: E402
import memory  # noqa: E402
import paired_eval  # noqa: E402
import snapshot  # noqa: E402
import timeline  # noqa: E402


def test_ground_truth_marks_k004_healthy() -> None:
    ground_truth = json.loads(
        (TOOLS.parent / "ground_truth.json").read_text(encoding="utf-8")
    )
    assert ground_truth["datasets"]["K004"] == {
        "label": "healthy",
        "description": "健康轴承",
        "composite": False,
    }


def test_dataset_parser_accepts_all_ground_truth_codes() -> None:
    for dataset in ("K004", "KA09", "KI08", "KB23", "KB24", "KI21"):
        source = f"D:/test/N09_M07_F10_{dataset}_1.mat"
        assert paired_eval.dataset_of_source(source) == dataset


def test_task_record_delegates_window_loading_to_mat_record() -> None:
    task = object.__new__(paired_eval.TaskRecord)
    task._record = SimpleNamespace(
        windows=lambda *, duration_ms, count: iter([(duration_ms, count)])
    )

    assert list(task.windows(duration_ms=50, count=15)) == [(50, 15)]


def test_build_manifest_is_scoped_and_maps_sources(tmp_path: Path) -> None:
    sender_logs = tmp_path / "sender.jsonl"
    rows = [
        {
            "task_id": "old", "sender_id": "sender_01", "bearing_id": "bearing_01",
            "device_id": "machine_old", "sequence_number": 1,
            "end_generate_timestamp_ns": 9,
        },
        {
            "task_id": "task_01", "sender_id": "sender_01", "bearing_id": "bearing_01",
            "device_id": "machine_01", "sequence_number": 1,
            "end_generate_timestamp_ns": 10,
        },
        {
            "task_id": "task_01", "sender_id": "sender_01", "bearing_id": "bearing_01",
            "device_id": "machine_01", "sequence_number": 2,
            "end_generate_timestamp_ns": 11,
        },
    ]
    sender_logs.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    manifest = build_manifest.build_manifest(
        sender_logs, {"sender_01": "K004/sample.mat"}, 10, 11
    )

    assert manifest == [{
        "task_id": "task_01",
        "sender_id": "sender_01",
        "bearing_id": "bearing_01",
        "device_id": "machine_01",
        "source_file": "K004/sample.mat",
    }]


def test_read_cloud_db_uses_run_scoped_host_database(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "cloud.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('ok')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(dpb_common, "CLOUD_CONTAINER", None)
    monkeypatch.setattr(dpb_common, "CLOUD_DB", database)

    assert dpb_common.read_cloud_db("SELECT value FROM sample") == [("ok",)]


def test_paired_eval_reads_raw_three_class_labels(monkeypatch) -> None:
    edge_payload = {
        "task_id": "task_01",
        "device_id": "machine_01",
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "diagnosis_window_id": "dw_01",
        "bearing_state": "fault",
        "diagnosis_label": "inner_ring_damage",
        "class_probabilities": {"inner_ring_damage": 0.9},
        "confidence": 0.9,
        "created_at_ns": 20,
    }
    monkeypatch.setattr(paired_eval, "EDGE_CONTAINERS", {"edge_01": "unused"})
    monkeypatch.setattr(
        paired_eval, "read_edge_db", lambda *_args: [(json.dumps(edge_payload),)]
    )
    monkeypatch.setattr(paired_eval, "resolve_edge_window", lambda _payload: 1)
    edge = paired_eval.load_edge_predictions(10, 30)
    assert edge[("task_01", 1)]["edge_label"] == "inner_ring_damage"

    monkeypatch.setattr(
        paired_eval,
        "read_cloud_db",
        lambda *_args: [(
            "task_01", 1, 1, "outer_ring_damage",
            '{"outer_ring_damage":0.8}', 0.8, "moment-v1", 25,
        )],
    )
    cloud = paired_eval.load_cloud_predictions(10, 30)
    assert cloud[("task_01", 1)]["cloud_label"] == "outer_ring_damage"


def test_conflict_resolution_separates_transport_and_cloud_resolution() -> None:
    windows = {
        "summary_1": {"has_conflict": True, "excluded_from_formal_metrics": False},
        "summary_2": {"has_conflict": True, "excluded_from_formal_metrics": False},
        "summary_3": {"has_conflict": False, "excluded_from_formal_metrics": False},
        "summary_bad": {"has_conflict": True, "excluded_from_formal_metrics": True},
    }
    metrics = timeline.conflict_resolution_metrics(
        windows,
        [{
            "summary_result_id": "summary_1", "state": "ACKNOWLEDGED",
            "acknowledged_at_ns": 99,
        }],
        [
            {"summary_result_id": "summary_1", "state": "ACKNOWLEDGED"},
            {"summary_result_id": "summary_2", "state": "ACKNOWLEDGED"},
        ],
        [{
            "summary_result_id": "summary_1", "status": "resolved",
        }],
    )
    assert metrics["conflict_rate"] == 0.6667
    assert metrics["arbitration_transport_success_rate"] == 1.0
    assert metrics["cloud_resolution_success_rate"] == 0.5
    assert metrics["final_suggestion_acknowledged"] == 1


def test_memory_report_uses_idle_snapshot_baseline(tmp_path: Path, monkeypatch) -> None:
    baseline = tmp_path / "snapshot.json"
    baseline.write_text(json.dumps({
        "memory_idle_baseline": {
            "stats": {
                "edge_01": {"p50": 100.0},
                "edge_02": {"p50": 200.0},
            }
        }
    }), encoding="utf-8")
    rows = [{
        "ts_ns": 1,
        "edge_01_memory_bytes": 130.0,
        "edge_02_memory_bytes": 260.0,
    }]
    monkeypatch.setattr(memory, "collect", lambda *_args: rows)
    report = tmp_path / "memory-report.json"

    assert memory.cmd_record(SimpleNamespace(
        seconds=1,
        interval_ms=500,
        out=str(tmp_path / "memory.jsonl"),
        baseline=str(baseline),
        report_out=str(report),
    )) == 0

    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["edge_01"]["peak_delta_from_idle_baseline"] == 30.0
    assert result["edge_02"]["peak_delta_from_idle_baseline"] == 60.0


def test_snapshot_queries_current_edge_outbox_table(monkeypatch) -> None:
    queries: list[str] = []

    def fake_edge_read(_container: str, sql: str):
        queries.append(sql)
        return []

    monkeypatch.setattr(snapshot, "EDGE_CONTAINERS", ["edge_01"])
    monkeypatch.setattr(snapshot, "read_edge_db", fake_edge_read)
    monkeypatch.setattr(snapshot, "read_cloud_db", lambda *_args: [])
    monkeypatch.setattr(snapshot, "read_summary_db", lambda *_args: [])

    snapshot.db_rows_snapshot()

    assert "bearing_result_outbox" in queries[0]
    assert "device_result_outbox" not in queries[0]
