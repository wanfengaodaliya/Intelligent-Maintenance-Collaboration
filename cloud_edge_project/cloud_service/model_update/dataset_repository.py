"""SQLite repositories for source proofs, labels and dataset manifests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database


class PacketSourceRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        with connect(self.database_path) as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS packet_source_mapping (
                       packet_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                       bearing_id TEXT NOT NULL,dataset_name TEXT NOT NULL,
                       dataset_version TEXT NOT NULL,source_file TEXT NOT NULL,
                       source_bearing_code TEXT NOT NULL,start_index INTEGER NOT NULL,
                       end_index INTEGER NOT NULL,window_index INTEGER NOT NULL,
                       created_at_ns INTEGER NOT NULL
                   );
                   CREATE INDEX IF NOT EXISTS idx_packet_source_task
                   ON packet_source_mapping(task_id,source_file);"""
            )

    def save(self, mapping: dict[str, Any]) -> dict[str, Any]:
        required = (
            "packet_id", "task_id", "bearing_id", "dataset_name",
            "dataset_version", "source_file", "source_bearing_code",
            "start_index", "end_index", "window_index",
        )
        if any(key not in mapping for key in required):
            raise ValueError("INVALID_PACKET_SOURCE_MAPPING")
        created_at_ns = mapping.get("created_at_ns", time.time_ns())
        with connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO packet_source_mapping(
                       packet_id,task_id,bearing_id,dataset_name,dataset_version,
                       source_file,source_bearing_code,start_index,end_index,
                       window_index,created_at_ns
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(packet_id) DO UPDATE SET
                       task_id=excluded.task_id,bearing_id=excluded.bearing_id,
                       dataset_name=excluded.dataset_name,dataset_version=excluded.dataset_version,
                       source_file=excluded.source_file,source_bearing_code=excluded.source_bearing_code,
                       start_index=excluded.start_index,end_index=excluded.end_index,
                       window_index=excluded.window_index""",
                tuple(mapping[key] for key in required) + (created_at_ns,),
            )
        return self.get_by_packet_id(mapping["packet_id"])

    def get_by_packet_id(self, packet_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM packet_source_mapping WHERE packet_id=?",
                (packet_id,),
            ).fetchone()
        return dict(row) if row else None


class LabelConfirmationRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def save(self, confirmation: dict[str, Any]) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO label_confirmation(
                       packet_id,confirmed_label,label_source,confirmed_at_ns
                   ) VALUES (?,?,?,?)
                   ON CONFLICT(packet_id) DO UPDATE SET
                       confirmed_label=excluded.confirmed_label,
                       label_source=excluded.label_source,
                       confirmed_at_ns=excluded.confirmed_at_ns""",
                (
                    confirmation["packet_id"], confirmation["confirmed_label"],
                    confirmation["label_source"],
                    confirmation.get("confirmed_at_ns", time.time_ns()),
                ),
            )
        return self.get(confirmation["packet_id"])

    def get(self, packet_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM label_confirmation WHERE packet_id=?", (packet_id,)
            ).fetchone()
        return dict(row) if row else None


class DatasetManifestRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def save(self, manifest: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO model_update_dataset_manifest(
                       dataset_id,update_id,baseline_version,
                       feature_pipeline_version,manifest_json,created_at_ns
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    manifest["dataset_id"], manifest["update_id"],
                    manifest["baseline_version"],
                    manifest["feature_pipeline_version"], serialized,
                    manifest["created_at_ns"],
                ),
            )
        return manifest

    def get_by_update(self, update_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT manifest_json FROM model_update_dataset_manifest WHERE update_id=?",
                (update_id,),
            ).fetchone()
        return json.loads(row["manifest_json"]) if row else None
