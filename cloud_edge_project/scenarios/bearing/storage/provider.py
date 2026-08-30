"""Bearing storage provider backed by the unchanged V1.2 schema."""

from compatibility.bearing_v12.storage_mapper import register_bearing_v12_storage
from core.scenario_plugin import StorageRegistrar


class BearingStorageProvider:
    scenario_id = "bearing"

    def initialize(self, registrar: StorageRegistrar) -> None:
        register_bearing_v12_storage(registrar)
