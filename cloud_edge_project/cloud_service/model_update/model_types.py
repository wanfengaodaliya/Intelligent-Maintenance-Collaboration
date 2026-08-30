"""Legacy model-type view and active-version pointer for model updates."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database
from compatibility.bearing_v12.legacy_exports import BEARING_MODEL_CATALOG
from core.model_lifecycle import ModelCatalog


@dataclass(frozen=True)
class ModelTypeSpec:
    family: str
    default_version: str
    description: str


MODEL_TYPE_SPECS: dict[str, ModelTypeSpec] = {
    model_id: ModelTypeSpec(
        family=descriptor.family,
        default_version=descriptor.default_version,
        description=descriptor.description,
    )
    for model_id, descriptor in BEARING_MODEL_CATALOG.models.items()
}

VALID_MODEL_TYPES = tuple(MODEL_TYPE_SPECS)


def validate_model_type(
    model_type: Any,
    model_catalog: ModelCatalog | None = None,
) -> str:
    catalog = model_catalog or BEARING_MODEL_CATALOG
    return catalog.require(model_type).model_id


class ActiveModelVersionStore:
    """Persisted pointer of the active version per model type."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def get(self, model_type: str) -> str | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT active_version FROM active_model_version WHERE model_type=?",
                (model_type,),
            ).fetchone()
        return row["active_version"] if row else None

    def set(self, model_type: str, active_version: str) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO active_model_version(model_type, active_version, updated_at_ns)
                   VALUES (?,?,?)
                   ON CONFLICT(model_type) DO UPDATE SET
                       active_version=excluded.active_version,
                       updated_at_ns=excluded.updated_at_ns""",
                (model_type, active_version, time.time_ns()),
            )
