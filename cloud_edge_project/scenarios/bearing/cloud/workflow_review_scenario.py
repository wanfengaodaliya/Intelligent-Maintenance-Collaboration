"""Bearing scenario workflow-review processors for the cloud review facade.

Implements the generic ``WorkflowReviewScenario`` protocol from
``cloud_service.workflow_review.service``. It is assembled in the cloud entry
point (``cloud_service/app.py``) when the default bearing scenario is selected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.device_arbitration.service import DeviceArbitrationService
from scenarios.bearing.cloud.device_arbitration.adapter import (
    BearingDeviceArbitrationAdapter,
)
from scenarios.bearing.cloud.workflow_review import (
    device_arbitration_request,
    review_packet,
    review_window,
    reviewed_device_result,
)


class BearingWorkflowReviewScenario:
    scenario_type = "bearing"

    def review_packet(self, request: dict[str, Any]) -> dict[str, Any]:
        return review_packet(request)

    def review_window(
        self, request: dict[str, Any], raw_packets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return review_window(request, raw_packets)

    def review_device(
        self, request: dict[str, Any], review_id: str, database_path: Path
    ) -> dict[str, Any]:
        arbitration = DeviceArbitrationService(
            database_path, BearingDeviceArbitrationAdapter()
        ).arbitrate(device_arbitration_request(request, review_id))
        return reviewed_device_result(request, arbitration)