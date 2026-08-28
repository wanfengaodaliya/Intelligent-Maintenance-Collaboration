from __future__ import annotations

import sqlite3

import pytest

from cloud_service.storage.database import initialize_database
from cloud_service.storage.schema import DDL
from cloud_service.device_arbitration.repository import DeviceArbitrationRepository
from core.scenario_plugin import StorageRegistrar
from scenarios.bearing.storage import BearingStorageProvider


class _LegacySchemaProvider:
    scenario_id = "inspection"

    def __init__(self) -> None:
        self.calls = 0

    def initialize(self, registrar: StorageRegistrar) -> None:
        self.calls += 1
        registrar.execute_schema(DDL)


def _object_counts(database_path) -> tuple[int, int, int]:
    with sqlite3.connect(database_path) as connection:
        tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        indexes = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        migrations = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    return tables, indexes, migrations


def _schema_snapshot(database_path) -> dict[str, object]:
    with sqlite3.connect(database_path) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        table_names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        tables = {
            name: {
                "columns": tuple(connection.execute(f'PRAGMA table_info("{name}")')),
                "indexes": tuple(connection.execute(f'PRAGMA index_list("{name}")')),
                "foreign_keys": tuple(
                    connection.execute(f'PRAGMA foreign_key_list("{name}")')
                ),
            }
            for name in table_names
        }
        migrations = tuple(
            connection.execute(
                "SELECT version, description FROM schema_migrations ORDER BY version"
            )
        )
    return {"objects": objects, "tables": tables, "migrations": migrations}


def test_initialize_database_executes_explicit_storage_provider(tmp_path) -> None:
    database_path = tmp_path / "provider.db"
    provider = _LegacySchemaProvider()

    initialize_database(database_path, storage_providers=(provider,))

    assert provider.calls == 1
    assert _object_counts(database_path) == (22, 15, 6)


def test_initialize_database_keeps_legacy_no_provider_behavior(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"

    initialize_database(database_path)

    assert _object_counts(database_path) == (22, 15, 6)


def test_bearing_provider_produces_the_exact_legacy_schema(tmp_path) -> None:
    legacy_path = tmp_path / "legacy.db"
    provider_path = tmp_path / "provider.db"

    initialize_database(legacy_path)
    initialize_database(
        provider_path,
        storage_providers=(BearingStorageProvider(),),
    )

    assert _schema_snapshot(provider_path) == _schema_snapshot(legacy_path)


def test_initialize_database_rejects_explicit_empty_provider_collection(
    tmp_path,
) -> None:
    database_path = tmp_path / "empty.db"

    with pytest.raises(ValueError, match="storage_providers"):
        initialize_database(database_path, storage_providers=())

    assert not database_path.exists()


def test_provider_initialization_preserves_history_writes_and_idempotency(
    tmp_path,
) -> None:
    database_path = tmp_path / "stage6-history.db"
    initialize_database(database_path)
    repository = DeviceArbitrationRepository(database_path)
    historical_request = {"conflict_id": "conflict-before-stage7", "payload": "old"}
    historical_result = {
        "arbitration_id": "arbitration-before-stage7",
        "conflict_id": "conflict-before-stage7",
        "scenario_type": "bearing",
        "subject_id": "machine-1",
        "task_id": "task-1",
        "status": "resolved",
        "final_action": "continue_operation",
        "confidence": 0.9,
        "created_at_ns": 10,
    }
    saved_historical = repository.save(
        request=historical_request,
        result=historical_result,
    )
    schema_before = _schema_snapshot(database_path)

    provider = BearingStorageProvider()
    initialize_database(database_path, storage_providers=(provider,))
    initialize_database(database_path, storage_providers=(provider,))

    assert repository.get_by_conflict_id("conflict-before-stage7") == saved_historical
    assert _schema_snapshot(database_path) == schema_before

    new_request = {"conflict_id": "conflict-after-stage7", "payload": "new"}
    new_result = {
        "arbitration_id": "arbitration-after-stage7",
        "conflict_id": "conflict-after-stage7",
        "scenario_type": "bearing",
        "subject_id": "machine-1",
        "task_id": "task-2",
        "status": "resolved",
        "final_action": "scheduled_inspection",
        "confidence": 0.8,
        "created_at_ns": 20,
    }
    saved_new = repository.save(request=new_request, result=new_result)

    assert repository.get_by_conflict_id("conflict-after-stage7") == saved_new
    with sqlite3.connect(database_path) as connection:
        arbitration_count = connection.execute(
            "SELECT COUNT(*) FROM device_arbitration_record"
        ).fetchone()[0]
        migration_counts = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT version) FROM schema_migrations"
        ).fetchone()
    assert arbitration_count == 2
    assert migration_counts == (6, 6)
