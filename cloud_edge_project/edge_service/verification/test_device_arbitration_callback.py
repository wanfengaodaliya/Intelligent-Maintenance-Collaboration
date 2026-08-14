from __future__ import annotations

from types import SimpleNamespace

from edge_runtime.http import EdgeControlApplication


def test_edge_control_accepts_device_arbitration_callback() -> None:
    payloads: list[dict] = []
    application = EdgeControlApplication(
        SimpleNamespace(),
        on_device_arbitration_result=lambda payload: payloads.append(payload)
        or SimpleNamespace(result_id="device_round_01_r2"),
    )

    status, response = application.handle(
        "/edge/device-arbitration-results", {"arbitration_id": "arbitration_01"}
    )

    assert status == 200
    assert response == {"accepted": True, "device_result_id": "device_round_01_r2"}
    assert payloads == [{"arbitration_id": "arbitration_01"}]
