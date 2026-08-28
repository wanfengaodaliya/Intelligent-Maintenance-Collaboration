from __future__ import annotations

from dataclasses import replace

import pytest

from summary_service.outbox import ArbitrationOutbox
from summary_service.runtime import SummaryRuntime, load_summary_settings
from summary_service.service import SummaryService
from summary_service.suggestion_llm import (
    SuggestionLlmResult,
    normalize_suggestion,
)


ACTIONS = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "urgent_intervention",
    4: "shutdown",
}

LEVEL_PROBS = {
    0: ({"healthy": 1.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0}, "low"),
    1: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "low"),
    2: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "high"),
    3: ({"healthy": 0.0, "outer_ring_damage": 1.0, "inner_ring_damage": 0.0}, "high"),
}

LEGACY_GRADE = {0: 0, 1: 1, 2: 2, 3: 4}


def bearing_result(
    bearing_id: str,
    edge_node_id: str,
    state: str = "normal",
    action_level: int = 0,
) -> dict:
    suffix = bearing_id[-2:]
    probabilities, risk_level = LEVEL_PROBS[action_level]
    grade = LEGACY_GRADE[action_level]
    return {
        "result_id": f"result_{suffix}",
        "device_id": "machine_01",
        "task_id": f"sd_{suffix}_tk_0001",
        "bearing_id": bearing_id,
        "sender_id": f"sender_{suffix}",
        "edge_node_id": edge_node_id,
        "decision_round_id": f"round_{suffix}",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "bearing_state": state,
        "risk_level": risk_level,
        "action_grade": grade,
        "recommended_action": ACTIONS[grade],
        "confidence": 0.9,
        "data_quality_score": 1.0,
        "model_version": "model-test",
        "created_at_ns": 100,
        "class_probabilities": probabilities,
    }


class RecordingLlm:
    def __init__(self, text: str = "建议尽快安排专业人员检查设备运行状态。") -> None:
        self.text = text
        self.calls = 0

    def translate(self, **kwargs) -> SuggestionLlmResult:
        self.calls += 1
        return SuggestionLlmResult(
            text=normalize_suggestion(self.text, kwargs["fallback_text"]),
            success=True,
        )


class FailingLlm:
    def __init__(self) -> None:
        self.calls = 0

    def translate(self, **kwargs) -> SuggestionLlmResult:
        self.calls += 1
        raise RuntimeError("llm unavailable")


def runtime_with(tmp_path, **overrides) -> SummaryRuntime:
    settings = replace(
        load_summary_settings(),
        database_path=tmp_path / "summary.db",
        suggestion_max_attempts=3,
        suggestion_retry_delay_seconds=0.0,
        **overrides,
    )
    return SummaryRuntime(settings)


def test_settings_load_expected_edge_nodes_from_environment(monkeypatch):
    monkeypatch.setenv("SUMMARY_EXPECTED_EDGE_NODE_IDS", "edge_01,edge_02")
    monkeypatch.setenv("SUMMARY_SUGGESTION_MAX_ATTEMPTS", "4")

    settings = load_summary_settings()

    assert settings.expected_edge_node_ids == ("edge_01", "edge_02")
    assert settings.expected_bearing_ids == ("bearing_01", "bearing_02")
    assert settings.suggestion_max_attempts == 4


def test_settings_default_to_the_dual_edge_contract(monkeypatch):
    monkeypatch.delenv("SUMMARY_EXPECTED_EDGE_NODE_IDS", raising=False)
    monkeypatch.delenv("SUMMARY_EXPECTED_BEARING_IDS", raising=False)

    settings = load_summary_settings()

    assert settings.expected_edge_node_ids == ("edge_01", "edge_02")
    assert settings.expected_bearing_ids == ("bearing_01", "bearing_02")


def test_runtime_service_uses_binary_edge_contract(tmp_path):
    runtime = runtime_with(tmp_path)

    assert runtime.service.expected_edge_node_ids == ("edge_01", "edge_02")


def test_final_window_suggestion_is_generated_asynchronously(tmp_path):
    runtime = runtime_with(tmp_path)
    llm = RecordingLlm()
    runtime.suggestion_client = llm

    result = None
    for payload in (
        bearing_result("bearing_01", "edge_01", "fault", 3),
        bearing_result("bearing_02", "edge_02", "fault", 3),
    ):
        result = runtime.service.ingest(payload)

    assert result is not None
    assert result["result_status"] == "FINAL"
    # Ingest never calls the LLM; the suggestion task waits for the worker.
    assert llm.calls == 0

    assert runtime.process_suggestion_tasks() == 1
    assert llm.calls == 1
    suggestion = runtime.repository.get_suggestion(result["summary_result_id"])
    assert suggestion is not None
    assert suggestion["generated_by"] == "llm"

    # Re-processing is a no-op once the task completed.
    assert runtime.process_suggestion_tasks() == 0
    assert llm.calls == 1


def test_suggestion_task_retries_then_dead_letters(tmp_path):
    runtime = runtime_with(tmp_path)
    llm = FailingLlm()
    runtime.suggestion_client = llm
    for payload in (
        bearing_result("bearing_01", "edge_01", "normal", 0),
        bearing_result("bearing_02", "edge_02", "normal", 1),
    ):
        runtime.service.ingest(payload)

    assert runtime.process_suggestion_tasks() == 0  # attempt 1 → retry
    assert runtime.process_suggestion_tasks() == 0  # attempt 2 → retry
    assert runtime.process_suggestion_tasks() == 0  # attempt 3 → dead letter
    assert llm.calls == 3
    metrics = runtime.repository.metrics()
    assert metrics["suggestion_tasks"]["dead_letter"] == 1
    assert runtime.process_suggestion_tasks() == 0
    assert llm.calls == 3


def test_suggestion_publish_retry_does_not_call_llm_again(tmp_path):
    runtime = runtime_with(tmp_path)
    llm = RecordingLlm("设备存在异常，请安排检查。")
    runtime.suggestion_client = llm
    for payload in (
        bearing_result("bearing_01", "edge_01", "fault", 2),
        bearing_result("bearing_02", "edge_02", "fault", 3),
    ):
        runtime.service.ingest(payload)
    runtime.process_suggestion_tasks()

    attempts = {"count": 0}

    def flaky_publish(payload):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary MQTT failure")
        return {"published": True, "result_id": payload["result_id"]}

    outbox = ArbitrationOutbox(
        runtime.repository,
        flaky_publish,
        retry_delay_ns=0,
        table_name="summary_suggestion_outbox",
    )
    assert outbox.run_due() == 0
    assert outbox.run_due() == 1
    assert attempts["count"] == 2
    assert llm.calls == 1


def test_disabled_llm_uses_rule_fallback(tmp_path):
    runtime = runtime_with(tmp_path, suggestion_llm_enabled=False)
    runtime.suggestion_client = None
    for payload in (
        bearing_result("bearing_01", "edge_01", "fault", 3),
        bearing_result("bearing_02", "edge_02", "fault", 3),
    ):
        runtime.service.ingest(payload)

    assert runtime.process_suggestion_tasks() == 1
    result = runtime.repository.list_window_results()[0]
    suggestion = runtime.repository.get_suggestion(result["summary_result_id"])
    assert suggestion["generated_by"] == "rule"
    assert suggestion["suggestion"] == "设备故障风险高，请立即停机。"


def test_arbitration_success_writes_back_final_and_generates_suggestion(tmp_path):
    runtime = runtime_with(tmp_path)
    llm = RecordingLlm("设备风险较高，请尽快干预。")
    runtime.suggestion_client = llm
    result = None
    for payload in (
        bearing_result("bearing_01", "edge_01", "normal", 0),
        bearing_result("bearing_02", "edge_02", "fault", 3),
    ):
        result = runtime.service.ingest(payload)

    assert result["result_status"] == "PENDING_ARBITRATION"
    assert llm.calls == 0
    cloud_response = {
        "arbitration_id": "arbitration_01",
        "status": "resolved",
        "final_state": "fault",
        "final_action": "shutdown",
        "confidence": 0.91,
    }
    runtime._post_json = lambda _url, _payload: dict(cloud_response)

    requests: list[dict] = []

    def transport(payload):
        requests.append(dict(payload))
        return runtime._post_arbitration(payload)

    outbox = ArbitrationOutbox(runtime.repository, transport, now_ns=lambda: 2_000)
    assert outbox.run_due() == 1

    stored = runtime.repository.get_window_result_by_id(result["summary_result_id"])
    assert stored["result_status"] == "FINAL"
    assert stored["final_state"] == "fault"
    assert stored["final_source"] == "cloud_arbitration"
    assert stored["revision"] == 2
    assert requests[0]["comparison"]["state_mismatch"] is True

    # The arbitration write-back schedules the suggestion asynchronously.
    assert llm.calls == 0
    assert runtime.process_suggestion_tasks() == 1
    assert llm.calls == 1
    suggestion = runtime.repository.get_suggestion(result["summary_result_id"])
    assert suggestion["recommended_action"] == "shutdown"

    # Redelivery of the same arbitration stays idempotent.
    assert outbox.run_due() == 0
    runtime._post_arbitration(requests[0])
    assert llm.calls == 1
    assert runtime.repository.list_window_results()[0]["revision"] == 2


def test_manual_review_arbitration_does_not_finalize(tmp_path):
    runtime = runtime_with(tmp_path)
    for payload in (
        bearing_result("bearing_01", "edge_01", "normal", 0),
        bearing_result("bearing_02", "edge_02", "fault", 3),
    ):
        result = runtime.service.ingest(payload)

    runtime._post_json = lambda _url, _payload: {
        "arbitration_id": "arbitration_02",
        "status": "manual_review",
        "final_state": None,
        "final_action": None,
    }

    outbox = ArbitrationOutbox(runtime.repository, runtime._post_arbitration)
    assert outbox.run_due() == 1

    stored = runtime.repository.get_window_result_by_id(result["summary_result_id"])
    assert stored["result_status"] == "MANUAL_REVIEW"
    metrics = runtime.repository.metrics()
    assert metrics["counters"]["arbitration_manual_review_windows"] == 1


def test_mqtt_poison_message_is_acked_and_exposed(tmp_path):
    runtime = runtime_with(tmp_path)
    acknowledged: list[int] = []

    class FakeMessage:
        mid = 7
        qos = 1
        payload = b"{not json"

    class FakeClient:
        def ack(self, mid, qos):
            acknowledged.append(mid)

    runtime._on_message(FakeClient(), None, FakeMessage())

    assert acknowledged == [7]
    assert runtime.last_error is not None


def test_service_ingest_still_uses_runtime_defaults(tmp_path):
    runtime = runtime_with(tmp_path)
    assert isinstance(runtime.service, SummaryService)
    assert runtime.service.expected_bearing_ids == ("bearing_01", "bearing_02")
