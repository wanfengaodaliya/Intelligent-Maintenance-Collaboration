"""Compressed sender raw packets and their SQLite index."""

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
        digest, sender_id, packet_id = hashlib.sha256(payload).hexdigest(), packet["sender_id"], packet["packet_id"]
        date = datetime.fromtimestamp(packet["end_generate_timestamp_ns"] / 1_000_000_000, timezone.utc).strftime("%Y-%m-%d")
        path = self.raw_root / sender_id / date / f"{packet_id}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.gz.tmp")
        with gzip.open(temporary, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
        vibration = packet["data"]["vibration"]
        stored = {"storage_path": str(path.relative_to(self.raw_root)).replace("\\", "/"), "payload_sha256": digest, "compressed_size_bytes": path.stat().st_size}
        with connect(self.database_path) as connection:
            connection.execute("INSERT INTO raw_packet_index(sender_id,packet_id,task_id,sequence_number,start_timestamp_ns,end_generate_timestamp_ns,sample_rate_hz,sample_count,storage_path,payload_sha256,compressed_size_bytes,validation_status,received_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sender_id,packet_id) DO UPDATE SET storage_path=excluded.storage_path,payload_sha256=excluded.payload_sha256,compressed_size_bytes=excluded.compressed_size_bytes,validation_status=excluded.validation_status,received_at_ns=excluded.received_at_ns", (sender_id, packet_id, packet["task_id"], packet["sequence_number"], packet["end_generate_timestamp_ns"] - 50_000_000, packet["end_generate_timestamp_ns"], vibration["sample_rate_hz"], vibration["sample_count"], stored["storage_path"], digest, stored["compressed_size_bytes"], packet.get("validation_status", "valid"), time.time_ns()))
        return stored
