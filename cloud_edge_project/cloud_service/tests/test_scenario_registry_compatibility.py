from __future__ import annotations

from pathlib import Path

from cloud_service import app as cloud_app  # noqa: F401
from core.scenario_registry import get_scenario_handler


def test_cloud_app_keeps_legacy_core_handler_lookup(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"

    handler = get_scenario_handler("bearing", database_path=database_path)

    assert handler.scenario_type == "bearing"
    assert handler.database_path == database_path
