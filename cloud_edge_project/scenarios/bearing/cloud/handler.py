from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.device_arbitration.service import DeviceArbitrationService
from cloud_service.model import infer_cloud
from scenarios.bearing.cloud.device_arbitration.adapter import (
    BearingDeviceArbitrationAdapter,
)


class BearingCloudHandler:
    scenario_type = "bearing"

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return infer_cloud(payload)

    def arbitrate_device_conflict(self, payload: dict[str, Any]) -> dict[str, Any]:
        return DeviceArbitrationService(
            self.database_path,
            adapter=BearingDeviceArbitrationAdapter(),
        ).arbitrate(payload)

    def get_device_arbitration(self, conflict_id: str) -> dict[str, Any] | None:
        return DeviceArbitrationService(
            self.database_path,
            adapter=BearingDeviceArbitrationAdapter(),
        ).get(conflict_id)
