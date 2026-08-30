from __future__ import annotations

from core.scenario_plugin import StorageProvider, StorageRegistrar


class _Registrar:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def execute_schema(self, script: str) -> None:
        self.scripts.append(script)


class _Provider:
    scenario_id = "inspection"

    def initialize(self, registrar: StorageRegistrar) -> None:
        registrar.execute_schema("CREATE TABLE inspection_result(id TEXT);")


def test_storage_contract_is_scenario_neutral_and_executable() -> None:
    registrar = _Registrar()
    provider = _Provider()

    assert isinstance(registrar, StorageRegistrar)
    assert isinstance(provider, StorageProvider)
    provider.initialize(registrar)

    assert registrar.scripts == ["CREATE TABLE inspection_result(id TEXT);"]
