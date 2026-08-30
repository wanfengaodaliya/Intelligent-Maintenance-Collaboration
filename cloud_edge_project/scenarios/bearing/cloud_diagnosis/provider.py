"""Adapter around the verified bearing cloud handler."""

from __future__ import annotations

from pathlib import Path

from scenarios.bearing.cloud.handler import BearingCloudHandler


class BearingCloudDiagnosisProvider:
    scenario_id = "bearing"

    def build_handler(self, database_path: Path) -> BearingCloudHandler:
        return BearingCloudHandler(database_path)
