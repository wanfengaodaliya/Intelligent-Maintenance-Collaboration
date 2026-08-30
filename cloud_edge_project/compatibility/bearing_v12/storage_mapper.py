"""Register the unchanged V1.2 deployment schema through a generic boundary."""

from core.scenario_plugin import StorageRegistrar
from cloud_service.storage.schema import DDL


def register_bearing_v12_storage(registrar: StorageRegistrar) -> None:
    registrar.execute_schema(DDL)
