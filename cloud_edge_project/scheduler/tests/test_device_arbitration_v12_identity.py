from __future__ import annotations

from types import SimpleNamespace

from scheduler.deferred_device_dispatcher import DeferredDeviceArbitrationDispatcher
from scheduler.deferred_device_repository import DeferredDeviceArbitrationRepository
from scheduler.device_router import DeviceArbitrationRouter
from scheduler.device_service import DeviceArbitrationService


class _ReadyRegistry:
    def snapshot(self, *_args, **_kwargs):
        return SimpleNamespace(
            is_fresh=True,
            health_status="ONLINE",
            queue_length=0,
            status_message_id="cloud-status-1",
            model_loaded=lambda _model: True,
        )

    def link_snapshot(self, *_args, **_kwargs):
        return SimpleNamespace(
            measurement_status="AVAILABLE",
            connected=True,
            goodput_mbps=20.0,
            rtt_ms_p95=10.0,
            loss_rate=0.0,
            link_id="link-1",
        )


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def dispatch(self, base_url: str, payload: dict) -> dict:
        self.calls.append((base_url, payload))
        return {"arbitration_id": "arbitration_01"}


def _request() -> dict:
    return {
        "device_id": "machine_01",
        "task_id": "task_001",
        "decision_round_id": "round_machine_01_task_001_0001",
        "device_result_revision": 2,
        "bearing_result_ids": ["bearing_a_r2", "bearing_b_r2"],
        "expected_bearing_count": 2,
        "received_bearing_count": 2,
        "bearing_results": [
            {
                "bearing_id": "bearing_a",
                "bearing_result_id": "bearing_a_r2",
                "result": "warning",
                "confidence": 0.7,
                "risk_level": "MEDIUM",
                "action_level": 1,
                "result_status": "FINAL",
            },
            {
                "bearing_id": "bearing_b",
                "bearing_result_id": "bearing_b_r2",
                "result": "failure",
                "confidence": 0.6,
                "risk_level": "HIGH",
                "action_level": 3,
                "result_status": "FINAL",
            },
        ],
        "comparison": {
            "conflict": True,
            "conflict_type": "ACTION_SPAN",
            "action_level_min": 1,
            "action_level_max": 3,
            "action_level_span": 2,
            "aggregate_confidence": 0.6,
            "low_confidence_bearing_count": 1,
            "provisional_bearing_count": 0,
            "data_complete": True,
        },
        "task_complexity": 0.4,
        "local_arbitration_supported": True,
    }


def _service(tmp_path) -> DeviceArbitrationService:
    return DeviceArbitrationService(
        DeviceArbitrationRouter(cloud_registry=_ReadyRegistry(), clock_ns=lambda: 3),
        DeferredDeviceArbitrationRepository(tmp_path / "scheduler.db"),
    )


def test_v12_device_route_and_deferred_task_preserve_round_identity(tmp_path) -> None:
    request = _request()
    service = _service(tmp_path)

    decision = service.route(request)
    task = service.repository.get(decision["decision_id"])

    assert decision["decision_round_id"] == request["decision_round_id"]
    assert decision["device_result_revision"] == 2
    assert decision["bearing_result_ids"] == ["bearing_a_r2", "bearing_b_r2"]
    assert decision["conflict_id"]
    assert task is not None
    assert task["decision_round_id"] == request["decision_round_id"]
    assert task["device_result_revision"] == 2
    assert task["bearing_result_ids"] == ["bearing_a_r2", "bearing_b_r2"]


def test_deferred_device_dispatcher_delivers_v12_cloud_arbitration_contract(tmp_path) -> None:
    service = _service(tmp_path)
    decision = service.route(_request())
    client = _Client()
    dispatcher = DeferredDeviceArbitrationDispatcher(
        service.repository,
        summary_url_lookup=lambda _summary_id: "http://cloud.example",
        client=client,
        clock_ns=lambda: 3,
    )

    dispatched = dispatcher.dispatch_once(now_ns=3)

    assert dispatched is not None
    assert dispatched["state"] == "SUCCEEDED"
    assert dispatched["arbitration_id"] == "arbitration_01"
    assert client.calls == [
        (
            "http://cloud.example",
            {
                "conflict_id": decision["conflict_id"],
                "device_id": "machine_01",
                "task_id": "task_001",
                "decision_round_id": "round_machine_01_task_001_0001",
                "device_result_revision": 2,
                "bearing_result_ids": ["bearing_a_r2", "bearing_b_r2"],
                "bearing_results": _request()["bearing_results"],
                "comparison": _request()["comparison"],
                "local_arbitration_supported": True,
            },
        )
    ]


def test_deferred_device_arbitration_rechecks_eligibility_before_recovery(tmp_path) -> None:
    service = _service(tmp_path)
    decision = service.route(_request())
    client = _Client()
    blocked = DeferredDeviceArbitrationDispatcher(
        service.repository,
        cloud_url_lookup=lambda _cloud_id: "http://cloud.example",
        client=client,
        eligibility_check=lambda _task, _now: (False, "NETWORK_UNAVAILABLE"),
        clock_ns=lambda: 3,
    )

    retry = blocked.dispatch_once(now_ns=3)
    recovered = DeferredDeviceArbitrationDispatcher(
        service.repository,
        cloud_url_lookup=lambda _cloud_id: "http://cloud.example",
        client=client,
        eligibility_check=lambda _task, _now: (True, None),
        clock_ns=lambda: 5_000_000_003,
    ).dispatch_once(now_ns=5_000_000_003)

    assert retry is not None
    assert retry["state"] == "PENDING"
    assert retry["last_reason_code"] == "NETWORK_UNAVAILABLE"
    assert recovered is not None
    assert recovered["state"] == "SUCCEEDED"
    assert client.calls[0][1]["conflict_id"] == decision["conflict_id"]
    assert client.calls[0][1]["decision_round_id"] == decision["decision_round_id"]
