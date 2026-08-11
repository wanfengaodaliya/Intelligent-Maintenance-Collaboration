from __future__ import annotations

from dataclasses import dataclass

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
