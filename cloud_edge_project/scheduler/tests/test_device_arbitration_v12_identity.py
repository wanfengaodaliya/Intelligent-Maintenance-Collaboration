from __future__ import annotations

from types import SimpleNamespace

import pytest

from scheduler.deferred_device_dispatcher import DeferredDeviceArbitrationDispatcher
from scheduler.deferred_device_repository import DeferredDeviceArbitrationRepository
from scheduler.device_router import DeviceArbitrationRouteError, DeviceArbitrationRouter
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


class _EdgeResultClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def deliver(self, base_url: str, payload: dict) -> dict:
        self.calls.append((base_url, payload))
        return {"accepted": True}


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
    assert decision["source"]["bearing_results_ref"] == (
        f"summary-store://{request['task_id']}/bearings"
    )
    assert task is not None
    assert task["decision_round_id"] == request["decision_round_id"]
    assert task["device_result_revision"] == 2
    assert task["bearing_result_ids"] == ["bearing_a_r2", "bearing_b_r2"]
    assert task["bearing_results_ref"] == (
        f"summary-store://{request['task_id']}/bearings"
    )


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


def test_deferred_arbitration_returns_cloud_result_to_originating_edge(tmp_path) -> None:
    request = _request() | {"edge_node_id": "edge_01"}
    service = _service(tmp_path)
    service.route(request)
    edge_client = _EdgeResultClient()
    dispatcher = DeferredDeviceArbitrationDispatcher(
        service.repository,
        cloud_url_lookup=lambda _cloud_id: "http://cloud.example",
        edge_url_lookup=lambda edge_node_id: f"http://{edge_node_id}.example",
        client=_Client(),
        edge_result_client=edge_client,
        clock_ns=lambda: 3,
    )

    dispatched = dispatcher.dispatch_once(now_ns=3)

    assert dispatched is not None and dispatched["state"] == "SUCCEEDED"
    assert edge_client.calls == [
        ("http://edge_01.example", {"arbitration_id": "arbitration_01"})
    ]


def test_deferred_device_arbitration_recovers_after_scheduler_restart(tmp_path) -> None:
    database_path = tmp_path / "scheduler.db"
    service = DeviceArbitrationService(
        DeviceArbitrationRouter(cloud_registry=_ReadyRegistry(), clock_ns=lambda: 3),
        DeferredDeviceArbitrationRepository(database_path),
    )
    decision = service.route(_request())
    claimed = service.repository.claim_due(now_ns=3)
    assert claimed is not None and claimed["state"] == "DISPATCHING"
    del service

    recovered_repository = DeferredDeviceArbitrationRepository(database_path)
    assert recovered_repository.recover_non_terminal(now_ns=4) == 1
    recovered = recovered_repository.get(decision["decision_id"])

    assert recovered is not None
    assert recovered["state"] == "PENDING"
    assert recovered["last_reason_code"] == "SCHEDULER_RESTART"
    assert recovered["decision_round_id"] == "round_machine_01_task_001_0001"


def test_device_service_returns_same_legacy_decision_for_generic_input(tmp_path) -> None:
    legacy_request = _request()
    generic_request = dict(legacy_request)
    generic_request["expected_unit_count"] = generic_request.pop(
        "expected_bearing_count"
    )
    generic_request["received_unit_count"] = generic_request.pop(
        "received_bearing_count"
    )
    generic_request["unit_result_ids"] = generic_request.pop("bearing_result_ids")
    generic_request["unit_results"] = []
    for result in generic_request.pop("bearing_results"):
        generic_result = dict(result)
        generic_result["unit_id"] = generic_result.pop("bearing_id")
        generic_result["unit_result_id"] = generic_result.pop("bearing_result_id")
        generic_request["unit_results"].append(generic_result)
    generic_request["comparison"] = dict(generic_request["comparison"])
    generic_request["comparison"]["low_confidence_unit_count"] = generic_request[
        "comparison"
    ].pop("low_confidence_bearing_count")
    generic_request["comparison"]["provisional_unit_count"] = generic_request[
        "comparison"
    ].pop("provisional_bearing_count")

    legacy_path = tmp_path / "legacy"
    generic_path = tmp_path / "generic"
    legacy_path.mkdir()
    generic_path.mkdir()

    assert _service(legacy_path).route(legacy_request) == _service(
        generic_path
    ).route(generic_request)


def test_device_service_translates_alias_conflict_to_route_error(tmp_path) -> None:
    request = _request() | {"expected_unit_count": 3}
    service = _service(tmp_path)

    with pytest.raises(DeviceArbitrationRouteError) as captured:
        service.route(request)

    assert captured.value.code == "INVALID_DEVICE_ARBITRATION_REQUEST"
    assert captured.value.status_code == 400


def test_device_legacy_invalid_count_keeps_legacy_error_message() -> None:
    request = _request() | {"expected_bearing_count": 0}

    with pytest.raises(
        DeviceArbitrationRouteError,
        match="expected_bearing_count must be",
    ):
        DeviceArbitrationRouter(
            cloud_registry=_ReadyRegistry(),
            clock_ns=lambda: 3,
        ).decide(request)


def test_device_legacy_invalid_result_keeps_legacy_object_name() -> None:
    request = _request()
    request["bearing_results"] = ["invalid"]
    request["bearing_result_ids"] = ["invalid"]

    with pytest.raises(DeviceArbitrationRouteError, match="bearing result must be"):
        DeviceArbitrationRouter(
            cloud_registry=_ReadyRegistry(),
            clock_ns=lambda: 3,
        ).decide(request)
