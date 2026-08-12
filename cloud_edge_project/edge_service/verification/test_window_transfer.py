from __future__ import annotations

from dataclasses import replace

import pytest

from core.bearing_workflow_contracts import FINAL_EDGE, REVIEW_PENDING, BearingWindowResult
from edge_aggregation.window_transfer import (
    WindowReviewDispatcher,
    WindowReviewStore,
    WindowTransferError,
)
from edge_runtime.config import MqttConfig
from edge_runtime.mqtt import MqttIngress


def _window() -> BearingWindowResult:
    return BearingWindowResult(
        result_id="window-task-bearing-1",
        device_id="device-1",
        task_id="task-1",
        bearing_id="bearing-1",
        sender_id="sender-1",
        window_index=1,
        sequence_start=1,
        sequence_end=20,
        packet_count=20,
        valid_packet_count=20,
        action_grade=2,
        confidence=0.7,
        data_quality_score=1.0,
        result_source=FINAL_EDGE,
        review_status=REVIEW_PENDING,
        review_required=True,
        review_reasons=("ACTION_GRADE_CONFLICT",),
    )


def _packets() -> list[dict]:
    return [
        {
            "device_id": "device-1",
            "task_id": "task-1",
            "bearing_id": "bearing-1",
            "sender_id": "sender-1",
            "packet_id": f"packet-{sequence}",
            "sequence_number": sequence,
            "data": {},
        }
        for sequence in range(1, 21)
    ]


def test_window_store_keeps_old_bundle_when_capacity_rejects_new_one(tmp_path):
    store = WindowReviewStore(
        tmp_path,
        hard_limit_bytes=10_000,
        warning_bytes=5_000,
        reserved_free_bytes=0,
    )
    store.save(_window(), _packets())
    store.hard_limit_bytes = store.usage_bytes() + 1

    with pytest.raises(WindowTransferError, match="capacity guard") as error:
        store.save(replace(_window(), result_id="window-task-bearing-2"), _packets())

    assert error.value.status_code == 503
    assert [item["window_id"] for item in store.pending()] == ["window-task-bearing-1"]


class _Client:
    def __init__(self):
        self.available = False
        self.created = False

    def post(self, path, payload):
        if not self.available:
            raise OSError("offline")
        if path == "/cloud/bearing-review":
            self.created = True
            return {"bearing_review_id": "review-1", "raw_context_request_id": "request-1"}
        return {"status": "accepted", "received_packet_count": 20}

    def get(self, path):
        return {"status": "WAITING_FOR_CONTEXT", "received_packet_count": 0}


def test_dispatcher_retries_persisted_window_and_deletes_only_after_cloud_ack(tmp_path):
    store = WindowReviewStore(tmp_path, reserved_free_bytes=0)
    store.save(_window(), _packets())
    client = _Client()
    dispatcher = WindowReviewDispatcher(store, client)

    assert dispatcher.dispatch_pending() == 0
    assert len(store.pending()) == 1

    client.available = True
    assert dispatcher.dispatch_pending() == 1
    assert store.pending() == []


class _Reason:
    is_failure = False


class _MqttClient:
    def __init__(self):
        self.acks = []

    def manual_ack_set(self, enabled):
        self.manual_ack = enabled

    def reconnect_delay_set(self, **kwargs):
        return None

    def connect(self, host, port, keepalive):
        self.on_connect(self, None, None, _Reason(), None)

    def subscribe(self, topic, qos):
        return 0, 1

    def loop_start(self):
        return None

    def disconnect(self):
        return None

    def loop_stop(self):
        return None

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return 0


class _Message:
    payload = b'{"packet_id":"packet-1"}'
    mid = 7
    qos = 1


def test_mqtt_does_not_ack_when_cache_guard_rejects_packet():
    client = _MqttClient()

    def reject(_):
        raise WindowTransferError("CLOUD_REVIEW_CACHE_FULL", "full")

    ingress = MqttIngress(MqttConfig(), reject, client=client)
    ingress.start()
    try:
        ingress._on_message(client, None, _Message())
        ingress._queue.join()
    finally:
        ingress.stop()

    assert client.acks == []
