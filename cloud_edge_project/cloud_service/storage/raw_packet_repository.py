"""Compressed raw packet files and their SQLite index."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .database import connect


class RawPacketRepository:
    def __init__(self, database_path: Path, raw_root: Path | None = None):
        self.database_path = Path(database_path)
        self.raw_root = Path(raw_root) if raw_root else self.database_path.parent / "raw"

    def store(self, packet: dict) -> dict:
        payload = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        device_id, packet_id = packet["device_id"], packet["packet_id"]
        date = datetime.fromtimestamp(packet["end_timestamp_ns"] / 1_000_000_000, timezone.utc).strftime("%Y-%m-%d")
        path = self.raw_root / device_id / date / f"{packet_id}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(temporary, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        signals = packet["signals"]
        primary = next(iter(signals.values()))
        stored = {"storage_path": str(path.relative_to(self.raw_root)).replace("\\", "/"), "payload_sha256": digest,
                  "compressed_size_bytes": path.stat().st_size}
        with connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO raw_packet_index(device_id,packet_id,task_id,sequence_number,start_timestamp_ns,end_timestamp_ns,sample_rate_hz,sample_count,storage_path,payload_sha256,compressed_size_bytes,validation_status,received_at_ns) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,packet_id) DO UPDATE SET storage_path=excluded.storage_path,payload_sha256=excluded.payload_sha256,compressed_size_bytes=excluded.compressed_size_bytes,validation_status=excluded.validation_status,received_at_ns=excluded.received_at_ns",
                (device_id, packet_id, packet["task_id"], packet["sequence_number"], packet["start_timestamp_ns"], packet["end_timestamp_ns"], primary["sample_rate_hz"], primary["sample_count"], stored["storage_path"], digest, stored["compressed_size_bytes"], packet.get("validation_status", "valid"), time.time_ns()),
            )
        return stored

    def get(self, device_id: str, packet_id: str) -> dict | None:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM raw_packet_index WHERE device_id=? AND packet_id=?", (device_id, packet_id)).fetchone()
        return dict(row) if row else None
