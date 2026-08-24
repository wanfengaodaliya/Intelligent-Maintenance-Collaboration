# -*- coding: utf-8 -*-
import json
import sys

from edge_model.config import EdgeModelConfig
from edge_model.contracts import RunRecord
from edge_runtime import PacketRouteErrorRecorder


def test_runtime_environment_overrides_model_timeout_budget(monkeypatch) -> None:
    """部署可提高排队等待，同时保留足够的总处理预算。"""
    monkeypatch.setenv(
        "EDGE_CONTROL_SHARED_SECRET", "test-control-secret-32-bytes-long"
    )
    monkeypatch.setenv("EDGE_MODEL_QUEUE_WAIT_MS", "1000")
    monkeypatch.setenv("EDGE_MODEL_TOTAL_TIMEOUT_MS", "3000")

    from edge_service import app as application

    config = EdgeModelConfig()
    application._apply_model_runtime_env(config)

    assert config.timeout.queue_wait_ms == 1000
    assert config.timeout.total_ms == 3000
    assert config.validate() == []


def test_local_h5_torch_threads_are_configurable(monkeypatch) -> None:
    """多消费者部署必须可限制 PyTorch 的内部并行度。"""
    monkeypatch.setenv(
        "EDGE_CONTROL_SHARED_SECRET", "test-control-secret-32-bytes-long"
    )
    monkeypatch.setenv("EDGE_TORCH_INTRAOP_THREADS", "2")
    monkeypatch.setenv("EDGE_TORCH_INTEROP_THREADS", "1")
    calls: list[tuple[str, int]] = []

    class FakeTorch:
        @staticmethod
        def set_num_threads(value: int) -> None:
            calls.append(("intraop", value))

        @staticmethod
        def set_num_interop_threads(value: int) -> None:
            calls.append(("interop", value))

    monkeypatch.setitem(sys.modules, "torch", FakeTorch)
    from edge_service import app as application

    assert application._configure_local_h5_torch_threads() == {
        "intraop": 2,
        "interop": 1,
    }
    assert calls == [("intraop", 2), ("interop", 1)]


def test_failed_model_run_is_persisted_without_raw_packet(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "EDGE_CONTROL_SHARED_SECRET", "test-control-secret-32-bytes-long"
    )
    from edge_service import app as application

    path = tmp_path / "edge_model_runs.jsonl"
    record = RunRecord(
        request_id="request-1",
        device_id="machine-01",
        bearing_id="bearing-01",
        task_id="task-1",
        packet_id="packet-1",
        sender_id="sender-01",
        sequence_number=1,
        execution_mode="LOCAL_MODEL",
        fallback_reason="QUEUE_TIMEOUT",
        output_valid=False,
        queue_wait_ms=251.5,
        inference_latency_ms=None,
        breaker_state="CLOSED",
        note="model_route_reason=QUEUE_TIMEOUT",
    )

    application._record_failed_model_run(PacketRouteErrorRecorder(path), record)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["packet_id"] == "packet-1"
    assert stored["fallback_reason"] == "QUEUE_TIMEOUT"
    assert stored["queue_wait_ms"] == 251.5
    assert "raw_packet" not in stored
