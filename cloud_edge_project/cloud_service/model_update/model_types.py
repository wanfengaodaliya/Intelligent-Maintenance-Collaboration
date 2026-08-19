"""Dual-model registry and active-version pointer for model updates.

The bearing scenario updates two model families: the edge distilled H5 model
(`distilled_h5`) and the cloud MOMENT light-adapt review model
(`moment_light_adapt`). This module is the single source of truth for the
supported model types, their runtime families and seed versions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database


@dataclass(frozen=True)
class ModelTypeSpec:
    family: str
    default_version: str
    description: str


MODEL_TYPE_SPECS: dict[str, ModelTypeSpec] = {
    "distilled_h5": ModelTypeSpec(
        family="edge",
        default_version="distilled_h5_kd_fold3_a9f20442",
        description="edge distilled H5 three-branch realtime model",
    ),
    "moment_light_adapt": ModelTypeSpec(
        family="cloud",
        default_version="moment-scl05-final",
        description="cloud MOMENT light-adapt review model",
    ),
}

VALID_MODEL_TYPES = tuple(MODEL_TYPE_SPECS)


def validate_model_type(model_type: Any) -> str:
    if model_type not in VALID_MODEL_TYPES:
        raise ValueError(f"UNSUPPORTED_MODEL_TYPE={model_type}")
    return model_type


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