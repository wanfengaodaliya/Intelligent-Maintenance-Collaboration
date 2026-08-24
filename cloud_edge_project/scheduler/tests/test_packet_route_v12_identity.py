from __future__ import annotations

from types import SimpleNamespace

import pytest

from scheduler import packet_router as packet_router_module
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from scheduler.deferred_cloud_repository import DeferredCloudRepository
from scheduler.deferred_dispatcher import DeferredCloudDispatcher
from scheduler.packet_router import PacketRouteError, PacketRouter
from scheduler.packet_service import PacketRoutingService


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


class _UnavailableRegistry:
    def snapshot(self, *_args, **_kwargs):
        return None

    def link_snapshot(self, *_args, **_kwargs):
        return None


class _DispatchClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def dispatch(self, _base_url: str, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"accepted": True}


def _packet_route_request() -> dict:
    identity = {
        "device_id": "machine_01",
        "task_id": "task_001",
        "bearing_id": "bearing_02",
        "sender_id": "sender_02",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
    }
    return {
        "device_id": identity["device_id"],
        "task_id": identity["task_id"],
        "bearing_id": identity["bearing_id"],
        "edge_node_id": "edge_01",
        "decision_round_id": build_decision_round_id(
            device_id="machine_01",
            task_id="task_001",
            window_start_sequence=1,
            window_end_sequence=1,
        ),
        "diagnosis_window_id": build_diagnosis_window_id(**identity),
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "error": None,
        "input_ref": {
            "device_id": "machine_01",
            "bearing_id": "bearing_02",
            "sender_id": "sender_02",
            "packet_id": "packet_001",
            "sequence_number": 1,
        },
        "status": "SUCCEEDED",
        "started_at_ns": 1,
        "finished_at_ns": 2,
        "output": {
            "edge_result": "warning",
            "confidence": 0.25,
            "task_complexity": 0.75,
            "edge_risk_level": "medium",
            "model_version": "edge_model_v1",
        },
    }


def _router() -> PacketRouter:
    return _router_with_registry(_ReadyRegistry())


def _router_with_registry(registry) -> PacketRouter:
    return PacketRouter(
        assignment_lookup=lambda _task_id: {
            "task_id": "task_001",
            "device_id": "machine_01",
            "sender_id": "sender_02",
            "bearing_id": "bearing_02",
            "edge_node_id": "edge_01",
            "assignment_status": "ASSIGNED",
        },
        cloud_registry=registry,
        clock_ns=lambda: 3,
    )


def test_cloud_now_route_echoes_v12_window_and_round_identity() -> None:
    request = _packet_route_request()

    decision = _router().decide(request)

    assert decision["route"] == "CLOUD_NOW"
    assert decision["legacy_route"] == "CLOUD_REVIEW_NOW"
    assert decision["decision_round_id"] == request["decision_round_id"]
    assert decision["diagnosis_window_id"] == request["diagnosis_window_id"]
    assert decision["window_start_sequence"] == 1
    assert decision["window_end_sequence"] == 1
    assert decision["result_instruction"] == {
        "result_status": "WAITING_CLOUD",
        "decision_source": "EDGE",
        "review_status": "PENDING_CLOUD",
        "degraded": False,
    }


def test_deferred_task_persists_v12_identity(tmp_path) -> None:
    request = _packet_route_request()
    service = PacketRoutingService(
        _router(), DeferredCloudRepository(tmp_path / "scheduler.db")
    )

    decision = service.route(request)
    task = service.repository.get(decision["decision_id"])

    assert task is not None
    assert task["route"] == "CLOUD_NOW"
    assert task["decision_round_id"] == request["decision_round_id"]
    assert task["diagnosis_window_id"] == request["diagnosis_window_id"]
    assert task["window_start_sequence"] == 1
    assert task["window_end_sequence"] == 1


def test_failed_edge_packets_are_final_and_never_create_cloud_reviews(tmp_path) -> None:
    """本地失败没有有效诊断结果，必须终止在 Scheduler，不能进入云复核。"""
    service = PacketRoutingService(
        _router(), DeferredCloudRepository(tmp_path / "scheduler.db")
    )
    for status, error, reason in (
        ("TIMEOUT", "QUEUE_TIMEOUT", "EDGE_TIMEOUT"),
        ("FAILED", "MODEL_UNAVAILABLE", "EDGE_FAILED"),
    ):
        request = _packet_route_request()
        request["status"] = status
        request["error"] = error
        request.pop("output")
        request["input_ref"]["packet_id"] = f"packet_{status.lower()}"

        decision = service.route(request)

        assert decision["route"] == "EDGE"
        assert decision["legacy_route"] == "DIRECT_FINAL_TO_SUMMARY"
        assert decision["needs_cloud_review"] is False
        assert decision["deferred_cloud_review"] is False
        assert decision["reason_codes"] == [reason]
        assert decision["result_instruction"] == {
            "result_status": "FINAL",
            "decision_source": "EDGE",
            "review_status": "NOT_REQUIRED",
            "degraded": False,
        }
        assert service.repository.get(decision["decision_id"]) is None


def test_deferred_packet_route_rechecks_network_and_recovers_identity(tmp_path) -> None:
    service = PacketRoutingService(
        _router_with_registry(_UnavailableRegistry()),
        DeferredCloudRepository(tmp_path / "scheduler.db"),
    )
    decision = service.route(_packet_route_request())
    assert decision["route"] == "DEFER"
    client = _DispatchClient()
    blocked = DeferredCloudDispatcher(
        service.repository,
        edge_url_lookup=lambda _edge_id: "http://edge.example",
        client=client,
        eligibility_check=lambda _task, _now: (False, "NETWORK_UNAVAILABLE"),
        clock_ns=lambda: 3,
    ).dispatch_once(now_ns=3)
    recovered = DeferredCloudDispatcher(
        service.repository,
        edge_url_lookup=lambda _edge_id: "http://edge.example",
        client=client,
        eligibility_check=lambda _task, _now: (True, None),
        clock_ns=lambda: 5_000_000_003,
    ).dispatch_once(now_ns=5_000_000_003)

    assert blocked is not None and blocked["state"] == "PENDING"
    assert blocked["last_reason_code"] == "NETWORK_UNAVAILABLE"
    assert recovered is not None and recovered["state"] == "WAITING_RESULT"
    assert client.payloads[0]["decision_round_id"] == decision["decision_round_id"]
    assert client.payloads[0]["diagnosis_window_id"] == decision["diagnosis_window_id"]


def test_packet_service_returns_same_legacy_decision_for_generic_input(tmp_path) -> None:
    legacy_request = _packet_route_request()
    generic_request = dict(legacy_request)
    generic_request["unit_id"] = generic_request.pop("bearing_id")
    generic_request["input_ref"] = dict(generic_request["input_ref"])
    generic_request["input_ref"]["unit_id"] = generic_request["input_ref"].pop(
        "bearing_id"
    )
    legacy_service = PacketRoutingService(
        _router(), DeferredCloudRepository(tmp_path / "legacy.db")
    )
    generic_service = PacketRoutingService(
        _router(), DeferredCloudRepository(tmp_path / "generic.db")
    )

    assert legacy_service.route(legacy_request) == generic_service.route(generic_request)


def test_p1_policy_receives_generic_unit_identity(monkeypatch) -> None:
    captured: dict = {}

    def choose(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(route="cloud", reason_codes=("P1_TEST",))

    monkeypatch.setattr(packet_router_module, "_p1_choose_route", choose)

    decision = _router().decide(_packet_route_request())

    assert captured["task"] == {
        "task_id": "task_001",
        "source_node": "machine_01",
        "unit_id": "bearing_02",
    }
    assert decision["route"] == "CLOUD_NOW"


def test_packet_service_translates_alias_conflict_to_route_error(tmp_path) -> None:
    request = _packet_route_request() | {
        "unit_id": "different_unit",
    }
    service = PacketRoutingService(
        _router(), DeferredCloudRepository(tmp_path / "scheduler.db")
    )

    with pytest.raises(PacketRouteError) as captured:
        service.route(request)

    assert captured.value.code == "INVALID_PACKET_RESULT"
    assert captured.value.status_code == 400


def test_packet_legacy_invalid_identity_keeps_legacy_error_message() -> None:
    request = _packet_route_request()
    request["bearing_id"] = ""
    request["input_ref"]["bearing_id"] = ""

    with pytest.raises(PacketRouteError, match="bearing_id must be"):
        _router().decide(request)


def test_malformed_assignment_aliases_are_reported_as_assignment_conflict() -> None:
    router = PacketRouter(
        assignment_lookup=lambda _task_id: {
            "unit_id": "bearing_01",
            "bearing_id": "bearing_02",
        },
        cloud_registry=_ReadyRegistry(),
        clock_ns=lambda: 3,
    )

    with pytest.raises(PacketRouteError) as captured:
        router.decide(_packet_route_request())

    assert captured.value.code == "PACKET_ASSIGNMENT_CONFLICT"
    assert captured.value.status_code == 409


def test_legacy_assignment_conflict_keeps_legacy_identity_field() -> None:
    router = PacketRouter(
        assignment_lookup=lambda _task_id: {
            "task_id": "task_001",
            "device_id": "machine_01",
            "sender_id": "sender_02",
            "bearing_id": "bearing_99",
            "edge_node_id": "edge_01",
            "assignment_status": "ASSIGNED",
        },
        cloud_registry=_ReadyRegistry(),
        clock_ns=lambda: 3,
    )

    with pytest.raises(PacketRouteError, match="assigned bearing_id"):
        router.decide(_packet_route_request())
