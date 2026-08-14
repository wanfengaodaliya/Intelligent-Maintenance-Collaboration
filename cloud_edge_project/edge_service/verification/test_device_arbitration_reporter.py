from __future__ import annotations

from edge_runtime.packet_route_reporter import DeviceArbitrationReporter


def test_device_arbitration_reporter_targets_scheduler_v12_route() -> None:
    calls: list[tuple[str, dict]] = []
    reporter = DeviceArbitrationReporter(
        lambda path, payload: calls.append((path, dict(payload))) or {"route": "CLOUD_ARBITRATION_NOW"},
        wait=lambda _seconds: None,
    )

    response = reporter.report({"decision_round_id": "round_01"})

    assert response == {"route": "CLOUD_ARBITRATION_NOW"}
    assert calls == [
        ("/scheduler/device-arbitration-route", {"decision_round_id": "round_01"})
    ]
