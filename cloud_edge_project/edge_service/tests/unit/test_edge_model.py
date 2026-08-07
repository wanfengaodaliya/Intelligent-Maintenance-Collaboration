# -*- coding: utf-8 -*-
"""边缘模型运行模块的逐包语义测试。"""
from __future__ import annotations

import copy
import threading
import time

import pytest

from edge_model.code_fallback import TestRuleRunner
from edge_model.config import EdgeModelConfig
from edge_model.contracts import (
    EXECUTION_CODE_FALLBACK,
    EXECUTION_LOCAL_MODEL,
    REASON_BREAKER_OPEN,
    REASON_MODEL_BUSY,
    REASON_MODEL_INFERENCE_TIMEOUT,
    REASON_MODEL_INPUT_INVALID,
    REASON_MODEL_OUTPUT_INVALID,
    REASON_MODEL_UNAVAILABLE,
    REASON_QUEUE_FULL,
    EdgeResult,
    PacketInferenceTask,
)
from edge_model.model_client import ModelInferResult
from edge_model.pipeline import EdgeModelPipeline
from model_input_contract import ModelInputValidationError, model_input_probe


def _perception(packet_id: str, sequence_number: int, sender_id: str = "sender-1",
                task_id: str = "task-1", bearing_id: str = "bearing-1") -> dict:
    result = model_input_probe()
    result.update({
        "device_id": "device-1",
        "bearing_id": bearing_id,
        "task_id": task_id,
        "packet_id": packet_id,
        "sender_id": sender_id,
        "sequence_number": sequence_number,
        "end_generate_timestamp_ns": 1_700_000_000_000_000_000 + sequence_number,
        "feature_generated_at_ns": 1_700_000_000_100_000_000 + sequence_number,
    })
    result["features"]["vibration"]["rms"] = 0.3 + sequence_number / 1000.0
    return result


class FakeModelClient:
    def __init__(self, fail_mode: str = "none", latency_ms: float = 0.0,
                 model_version: str = "fake/v1"):
        self.fail_mode = fail_mode
        self.latency_ms = latency_ms
        self.model_version = model_version
        self.calls = []
        self._lock = threading.Lock()

    def infer(self, payload, timeout_ms=None, request_id=None,
              remaining_timeout_ms=None):
        with self._lock:
            self.calls.append({"payload": payload, "request_id": request_id})
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        if self.fail_mode == "timeout":
            return ModelInferResult(False, timed_out=True, latency_ms=self.latency_ms,
                                    error="MODEL_INFERENCE_TIMEOUT", request_id=request_id)
        if self.fail_mode == "busy":
            return ModelInferResult(False, latency_ms=self.latency_ms,
                                    error="MODEL_BUSY", request_id=request_id)
        if self.fail_mode == "unavailable":
            return ModelInferResult(False, latency_ms=self.latency_ms,
                                    error="MODEL_UNAVAILABLE", request_id=request_id)
        if self.fail_mode == "invalid":
            return ModelInferResult(False, latency_ms=self.latency_ms,
                                    error="MODEL_OUTPUT_INVALID", request_id=request_id)
        if self.fail_mode == "input_invalid":
            return ModelInferResult(False, latency_ms=self.latency_ms,
                                    error="MODEL_INPUT_INVALID", request_id=request_id)
        response_request_id = "wrong-request" if self.fail_mode == "mismatch" else request_id
        return ModelInferResult(
            True,
            edge=EdgeResult("warning", 0.7, "medium", self.model_version),
            latency_ms=self.latency_ms,
            request_id=response_request_id,
        )


class Harness:
    def __init__(self, cfg: EdgeModelConfig | None = None,
                 client: FakeModelClient | None = None):
        self.cfg = cfg or EdgeModelConfig()
        if cfg is None:
            self.cfg.queue.max_waiting_requests = 100
        self.client = client or FakeModelClient()
        self.records = []
        self.packets = []
        self.pipeline = EdgeModelPipeline(
            self.cfg,
            self.client,
            TestRuleRunner(self.cfg.fallback.rule_version),
            self.records.append,
            self.packets.append,
        )
        self.pipeline.start()

    def feed(self, count: int, sender_id: str = "sender-1"):
        request_ids = []
        for sequence_number in range(1, count + 1):
            request_ids.append(self.pipeline.ingest(
                sender_id,
                _perception(f"packet-{sequence_number:03d}", sequence_number, sender_id),
            ))
        assert self.pipeline.wait_idle(10.0)
        return request_ids

    def close(self):
        self.pipeline.stop()


def test_eighty_packets_trigger_eighty_independent_model_calls():
    harness = Harness()
    request_ids = harness.feed(80)
    harness.close()

    assert len(harness.client.calls) == 80
    assert len(harness.records) == 80
    assert len(harness.packets) == 80
    assert len(set(request_ids)) == 80
    assert [packet.sequence_number for packet in harness.packets] == list(range(1, 81))
    assert [record.packet_id for record in harness.records] == [
        f"packet-{sequence_number:03d}" for sequence_number in range(1, 81)
    ]
    assert all(record.execution_mode == EXECUTION_LOCAL_MODEL for record in harness.records)
    assert {call["request_id"] for call in harness.client.calls} == set(request_ids)


def test_packet_identity_is_preserved_and_no_internal_id_leaks():
    harness = Harness()
    request_id = harness.pipeline.ingest(
        "sender-a", _perception("packet-a", 1, "sender-a", "task-a", "bearing-a")
    )
    assert harness.pipeline.wait_idle(5.0)
    harness.close()

    result = harness.packets[0]
    assert result.device_id == "device-1"
    assert result.bearing_id == "bearing-a"
    assert result.task_id == "task-a"
    assert result.packet_id == "packet-a"
    assert result.sender_id == "sender-a"
    assert result.sequence_number == 1
    assert "request_id" not in result.as_dict()
    assert harness.records[0].request_id == request_id


def test_model_response_request_id_mismatch_falls_back_for_current_packet():
    harness = Harness(client=FakeModelClient(fail_mode="mismatch"))
    harness.feed(1)
    harness.close()

    assert harness.records[0].execution_mode == EXECUTION_CODE_FALLBACK
    assert harness.records[0].fallback_reason == REASON_MODEL_OUTPUT_INVALID
    assert harness.packets[0].edge.model_version == "edge_rule_test_v1"


@pytest.mark.parametrize(
    ("fail_mode", "reason"),
    [
        ("timeout", REASON_MODEL_INFERENCE_TIMEOUT),
        ("busy", REASON_MODEL_BUSY),
        ("unavailable", REASON_MODEL_UNAVAILABLE),
        ("invalid", REASON_MODEL_OUTPUT_INVALID),
        ("input_invalid", REASON_MODEL_INPUT_INVALID),
    ],
)
def test_model_failure_falls_back_per_packet(fail_mode, reason):
    cfg = EdgeModelConfig()
    cfg.queue.max_waiting_requests = 10
    cfg.breaker.enabled = False
    harness = Harness(cfg, FakeModelClient(fail_mode=fail_mode))
    harness.feed(3)
    harness.close()

    assert len(harness.records) == 3
    assert len(harness.packets) == 3
    assert all(record.execution_mode == EXECUTION_CODE_FALLBACK for record in harness.records)
    assert all(record.fallback_reason == reason for record in harness.records)
    assert all(packet.edge.model_version == "edge_rule_test_v1" for packet in harness.packets)


def test_replace_policy_does_not_drop_replaced_packets():
    cfg = EdgeModelConfig()
    cfg.queue.max_waiting_requests = 1
    cfg.queue.full_policy = "replace"
    cfg.timeout.queue_wait_ms = 500
    cfg.timeout.inference_ms = 500
    cfg.timeout.total_ms = 1100
    cfg.timeout.fallback_reserve_ms = 50
    harness = Harness(cfg, FakeModelClient(latency_ms=100.0))
    harness.feed(10)
    harness.close()

    assert len(harness.records) == 10
    assert len(harness.packets) == 10
    replaced = [record for record in harness.records
                if record.fallback_reason == REASON_QUEUE_FULL]
    assert replaced
    assert len({record.packet_id for record in harness.records}) == 10


def test_circuit_breaker_is_applied_to_individual_packets_and_recovers():
    cfg = EdgeModelConfig()
    cfg.queue.max_waiting_requests = 2
    cfg.breaker.consecutive_failure_threshold = 2
    cfg.breaker.recovery_probe_interval_s = 0.05
    client = FakeModelClient(fail_mode="unavailable")
    harness = Harness(cfg, client)

    for sequence_number in (1, 2, 3):
        harness.pipeline.ingest(
            "sender-1", _perception(f"packet-{sequence_number}", sequence_number)
        )
        assert harness.pipeline.wait_idle(2.0)
    assert harness.records[2].fallback_reason == REASON_BREAKER_OPEN
    assert len(client.calls) == 2

    time.sleep(0.10)
    client.fail_mode = "none"
    harness.pipeline.ingest("sender-1", _perception("packet-4", 4))
    assert harness.pipeline.wait_idle(2.0)
    harness.close()
    assert harness.records[-1].execution_mode == EXECUTION_LOCAL_MODEL


def test_pipeline_rejects_sender_identity_mismatch():
    harness = Harness()
    with pytest.raises(ValueError, match="sender_id"):
        harness.pipeline.ingest("sender-a", _perception("packet-1", 1, "sender-b"))
    harness.close()


def test_pipeline_copies_perception_before_async_inference():
    harness = Harness(client=FakeModelClient(latency_ms=20.0))
    perception = _perception("packet-1", 1)
    harness.pipeline.ingest("sender-1", perception)
    perception["features"]["vibration"]["rms"] = 99.0
    assert harness.pipeline.wait_idle(2.0)
    harness.close()
    assert harness.client.calls[0]["payload"]["features"]["vibration"]["rms"] != 99.0


def test_pipeline_passes_the_complete_perception_without_field_loss():
    harness = Harness()
    perception = _perception("packet-1", 1)
    expected = copy.deepcopy(perception)
    harness.pipeline.ingest("sender-1", perception)
    assert harness.pipeline.wait_idle(2.0)
    harness.close()

    assert harness.client.calls[0]["payload"] == expected


@pytest.mark.parametrize(
    "path",
    [
        ("features", "vibration"),
        ("features", "phase_current_1"),
        ("features", "phase_current_2"),
        ("features", "current_relationship"),
        ("features", "operating_context", "shaft_speed_rpm"),
        ("features", "operating_context", "load_torque_nm"),
        ("features", "operating_context", "bearing_radial_load_n"),
        ("features", "operating_context", "bearing_module_temperature_c"),
    ],
)
def test_pipeline_rejects_each_missing_feature_group(path):
    harness = Harness()
    perception = _perception("packet-1", 1)
    target = perception
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]

    with pytest.raises(ModelInputValidationError, match="missing"):
        harness.pipeline.ingest("sender-1", perception)
    harness.close()
    assert harness.client.calls == []


def test_rule_runner_uses_current_packet_perception():
    perception = _perception("packet-1", 1)
    task = PacketInferenceTask(
        request_id="request-1",
        device_id=perception["device_id"],
        bearing_id=perception["bearing_id"],
        task_id=perception["task_id"],
        packet_id=perception["packet_id"],
        sender_id=perception["sender_id"],
        sequence_number=perception["sequence_number"],
        perception=perception,
    )
    edge = TestRuleRunner().run(task)
    assert edge.edge_result in ("normal", "warning", "fault")
    assert edge.edge_risk_level in ("low", "medium", "high")
    assert 0.0 <= edge.confidence <= 1.0


def test_rule_runner_rejects_incomplete_perception():
    perception = _perception("packet-1", 1)
    del perception["features"]["phase_current_2"]
    task = PacketInferenceTask(
        request_id="request-1", device_id="device-1", bearing_id="bearing-1",
        task_id="task-1", packet_id="packet-1", sender_id="sender-1",
        sequence_number=1, perception=perception,
    )
    with pytest.raises(ModelInputValidationError, match="phase_current_2"):
        TestRuleRunner().run(task)


def test_config_validation_and_pipeline_start():
    cfg = EdgeModelConfig()
    assert cfg.validate() == []
    cfg.timeout.total_ms = 100
    assert any("超时" in error for error in cfg.validate())

    records, packets = [], []
    pipeline = EdgeModelPipeline(
        cfg, FakeModelClient(), TestRuleRunner(), records.append, packets.append
    )
    with pytest.raises(ValueError, match="配置校验失败"):
        pipeline.start()
