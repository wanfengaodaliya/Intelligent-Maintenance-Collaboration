from __future__ import annotations

from dataclasses import dataclass

from common.control_auth import (
    ControlAuthVerifier,
    encode_control_json,
    sign_control_request,
)
from edge_runtime.http import EdgeControlApplication


@dataclass(frozen=True)
class _Ack:
    ack_status: str = "ACCEPTED"

    def as_dict(self) -> dict[str, object]:
        return {"ack_status": self.ack_status}


class _Ingress:
    def register_task(self, payload: dict[str, object]) -> _Ack:
        return _Ack()

    def revoke_dispatch(
        self,
        dispatch_id: object,
        *,
        reason_code: object,
        revoked_at_ns: object,
    ) -> bool:
        return dispatch_id == "dispatch-1"


def test_control_application_keeps_task_lifecycle_endpoints() -> None:
    application = EdgeControlApplication(_Ingress())

    status, body = application.handle("/edge/tasks", {"dispatch_id": "dispatch-1"})
    assert status == 200
    assert body == {"ack_status": "ACCEPTED"}

    status, body = application.handle(
        "/edge/task-revocations",
        {"dispatch_id": "dispatch-1", "reason_code": "CANCELLED", "revoked_at_ns": 1},
    )
    assert status == 200
    assert body == {"dispatch_id": "dispatch-1", "revoked": True}


def test_old_packet_scheduler_endpoints_are_removed() -> None:
    application = EdgeControlApplication(_Ingress())

    for path in (
        "/edge/packet-route-decisions",
        "/edge/cloud-review-instructions",
    ):
        status, body = application.handle(path, {})
        assert status == 404
        assert body["error"]["code"] == "NOT_FOUND"


def test_control_application_requires_a_valid_non_replayed_signature() -> None:
    secret = b"test-control-secret-that-is-at-least-32-bytes"
    application = EdgeControlApplication(
        _Ingress(),
        control_auth_verifier=ControlAuthVerifier(secret, clock=lambda: 1_000),
    )
    path = "/edge/tasks"
    payload = {"task_id": "task-1"}
    raw_body = encode_control_json(payload)

    status, body = application.handle(path, payload, raw_body=raw_body)
    assert status == 401
    assert body["error"]["code"] == "CONTROL_AUTH_REQUIRED"

    headers = sign_control_request(
        secret,
        method="POST",
        path=path,
        body=raw_body,
        timestamp=1_000,
        nonce="00112233445566778899aabbccddeeff",
    )
    status, body = application.handle(
        path, payload, raw_body=raw_body, headers=headers
    )
    assert status == 200
    assert body == {"ack_status": "ACCEPTED"}

    status, body = application.handle(
        path, payload, raw_body=raw_body, headers=headers
    )
    assert status == 409
    assert body["error"]["code"] == "CONTROL_REPLAY_DETECTED"
