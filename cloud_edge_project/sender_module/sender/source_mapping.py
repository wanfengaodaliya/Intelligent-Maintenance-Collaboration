"""Local packet-to-Paderborn source proof persisted when packets are created."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path


PADERBORN_FILENAME = re.compile(
    r"^[^_]+_[^_]+_[^_]+_([A-Za-z](?:[A-Za-z]\d{2}|\d{3}))_\d+\.mat$",
    re.IGNORECASE,
)


def extract_paderborn_bearing_code(source_file: str) -> str:
    match = PADERBORN_FILENAME.fullmatch(Path(source_file).name)
    if match is None:
        raise ValueError("source file is not a supported Paderborn MAT filename")
    return match.group(1).upper()


class PacketSourceMappingStore:
    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS packet_source_mapping (
                       packet_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                       bearing_id TEXT NOT NULL,dataset_name TEXT NOT NULL,
                       dataset_version TEXT NOT NULL,source_file TEXT NOT NULL,
                       source_bearing_code TEXT NOT NULL,start_index INTEGER NOT NULL,
                       end_index INTEGER NOT NULL,window_index INTEGER NOT NULL,
                       created_at_ns INTEGER NOT NULL
                   )"""
            )

    def save(
        self,
        *,
        packet_id: str,
        task_id: str,
        bearing_id: str,
        source_path: Path,
        start_index: int,
        end_index: int,
        window_index: int,
    ) -> None:
        source_file = Path(source_path).name
        values = (
            packet_id, task_id, bearing_id, "paderborn", "paderborn_v1",
            source_file, extract_paderborn_bearing_code(source_file),
            start_index, end_index, window_index, time.time_ns(),
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO packet_source_mapping VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(packet_id) DO UPDATE SET
                       task_id=excluded.task_id,bearing_id=excluded.bearing_id,
                       dataset_name=excluded.dataset_name,dataset_version=excluded.dataset_version,
                       source_file=excluded.source_file,source_bearing_code=excluded.source_bearing_code,
                       start_index=excluded.start_index,end_index=excluded.end_index,
                       window_index=excluded.window_index""",
                values,
            )

    def get(self, packet_id: str) -> dict[str, object] | None:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM packet_source_mapping WHERE packet_id=?", (packet_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value.pop("created_at_ns")
        return value
