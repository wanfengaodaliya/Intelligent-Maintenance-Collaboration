"""Compressed sender raw packets and their SQLite index."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .database import connect


_SAFE_PATH_COMPONENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z"
)


def _bind_sender(
    connection,
    *,
    sender_id: str,
    device_id: str,
    bearing_id: str,
) -> None:
    existing = connection.execute(
        "SELECT device_id,bearing_id FROM senders WHERE sender_id=?",
        (sender_id,),
    ).fetchone()
    if existing and (
        existing["device_id"] is not None or existing["bearing_id"] is not None
    ) and (
        existing["device_id"] != device_id or existing["bearing_id"] != bearing_id
    ):
        raise ValueError("SENDER_BINDING_CONFLICT")
    now = time.time_ns()
    connection.execute(
        "INSERT INTO senders("
        "sender_id,created_at_ns,updated_at_ns,device_id,bearing_id"
        ") VALUES (?,?,?,?,?) ON CONFLICT(sender_id) DO UPDATE SET "
        "updated_at_ns=excluded.updated_at_ns,"
        "device_id=COALESCE(senders.device_id,excluded.device_id),"
        "bearing_id=COALESCE(senders.bearing_id,excluded.bearing_id)",
        (sender_id, now, now, device_id, bearing_id),
    )


class RawPacketRepository:
    def __init__(self, database_path: Path, raw_root: Path | None = None):
        self.database_path = Path(database_path)
        self.raw_root = Path(raw_root) if raw_root else self.database_path.parent / "raw"

    def store(self, packet: dict) -> dict:
        payload = canonical_payload(packet)
        digest, sender_id, packet_id = hashlib.sha256(payload).hexdigest(), packet["sender_id"], packet["packet_id"]
        device_id, bearing_id = packet["device_id"], packet["bearing_id"]
        _require_safe_path_component(sender_id, "sender_id")
        _require_safe_path_component(packet_id, "packet_id")
        with connect(self.database_path) as connection:
            _bind_sender(
                connection,
                sender_id=sender_id,
                device_id=device_id,
                bearing_id=bearing_id,
            )
            existing = connection.execute(
                "SELECT device_id,bearing_id,payload_sha256 FROM raw_packet_index "
                "WHERE sender_id=? AND packet_id=?",
                (sender_id, packet_id),
            ).fetchone()
            if existing and (
                existing["device_id"] != device_id
                or existing["bearing_id"] != bearing_id
                or existing["payload_sha256"] != digest
            ):
                raise ValueError("PACKET_CONTENT_CONFLICT")
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
            connection.execute("INSERT INTO raw_packet_index(sender_id,packet_id,device_id,task_id,bearing_id,sequence_number,start_timestamp_ns,end_generate_timestamp_ns,sample_rate_hz,sample_count,storage_path,payload_sha256,compressed_size_bytes,validation_status,received_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sender_id,packet_id) DO UPDATE SET storage_path=excluded.storage_path,payload_sha256=excluded.payload_sha256,compressed_size_bytes=excluded.compressed_size_bytes,validation_status=excluded.validation_status,received_at_ns=excluded.received_at_ns", (sender_id, packet_id, device_id, packet["task_id"], bearing_id, packet["sequence_number"], packet["end_generate_timestamp_ns"] - 50_000_000, packet["end_generate_timestamp_ns"], vibration["sample_rate_hz"], vibration["sample_count"], stored["storage_path"], digest, stored["compressed_size_bytes"], packet.get("validation_status", "valid"), time.time_ns()))
        return stored

    def ingest_context(
        self,
        packet: dict,
        *,
        review_id: str,
        relative_position: int,
        role: str,
    ) -> tuple[str, str | None]:
        """Atomically index and link one immutable context packet."""

        payload = _canonical_payload(packet)
        digest = hashlib.sha256(payload).hexdigest()
        sender_id = packet["sender_id"]
        packet_id = packet["packet_id"]
        device_id = packet["device_id"]
        bearing_id = packet["bearing_id"]
        _require_safe_path_component(sender_id, "sender_id")
        _require_safe_path_component(packet_id, "packet_id")
        date = datetime.fromtimestamp(
            packet["end_generate_timestamp_ns"] / 1_000_000_000,
            timezone.utc,
        ).strftime("%Y-%m-%d")
        path = (
            self.raw_root
            / sender_id
            / date
            / f"{packet_id}.{digest}.json.gz"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        vibration = packet["data"]["vibration"]
        storage_path = str(path.relative_to(self.raw_root)).replace("\\", "/")
        created_file = False
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            with connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _bind_sender(
                    connection,
                    sender_id=sender_id,
                    device_id=device_id,
                    bearing_id=bearing_id,
                )
                existing = connection.execute(
                    "SELECT storage_path,device_id,bearing_id FROM raw_packet_index "
                    "WHERE sender_id=? AND packet_id=?",
                    (sender_id, packet_id),
                ).fetchone()
                raw_status = "accepted"
                if existing:
                    existing_packet = self._read_packet(
                        existing["storage_path"]
                    )
                    if (
                        existing["device_id"] != device_id
                        or existing["bearing_id"] != bearing_id
                        or existing_packet is None
                        or _canonical_payload(existing_packet) != payload
                    ):
                        return "conflict", "PACKET_CONTENT_CONFLICT"
                    raw_status = "duplicate"
                else:
                    sequence_row = connection.execute(
                        "SELECT packet_id FROM raw_packet_index "
                        "WHERE sender_id=? AND task_id=? AND sequence_number=?",
                        (
                            sender_id,
                            packet["task_id"],
                            packet["sequence_number"],
                        ),
                    ).fetchone()
                    if sequence_row:
                        return "conflict", "TASK_SEQUENCE_CONFLICT"

                existing_link = connection.execute(
                    "SELECT relative_position,role "
                    "FROM review_context_packets "
                    "WHERE review_id=? AND sender_id=? AND packet_id=?",
                    (review_id, sender_id, packet_id),
                ).fetchone()
                link_status = "accepted"
                if existing_link:
                    if (
                        existing_link["relative_position"]
                        != relative_position
                        or existing_link["role"] != role
                    ):
                        return "conflict", "CONTEXT_POSITION_CONFLICT"
                    link_status = "duplicate"
                else:
                    occupied = connection.execute(
                        "SELECT packet_id FROM review_context_packets "
                        "WHERE review_id=? AND relative_position=?",
                        (review_id, relative_position),
                    ).fetchone()
                    if occupied:
                        return "conflict", "CONTEXT_POSITION_CONFLICT"

                try:
                    if raw_status == "accepted":
                        _write_durable_gzip(temporary, payload)
                        if path.exists():
                            existing_packet = self._read_packet(
                                storage_path
                            )
                            if (
                                existing_packet is None
                                or _canonical_payload(existing_packet)
                                != payload
                            ):
                                raise OSError(
                                    "immutable raw packet path collision"
                                )
                            temporary.unlink(missing_ok=True)
                        else:
                            os.replace(temporary, path)
                            _fsync_directory(path.parent)
                            created_file = True
                        connection.execute(
                            "INSERT INTO raw_packet_index("
                            "sender_id,packet_id,device_id,task_id,bearing_id,sequence_number,"
                            "start_timestamp_ns,end_generate_timestamp_ns,"
                            "sample_rate_hz,sample_count,storage_path,"
                            "payload_sha256,compressed_size_bytes,"
                            "validation_status,received_at_ns"
                            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                sender_id,
                                packet_id,
                                device_id,
                                packet["task_id"],
                                bearing_id,
                                packet["sequence_number"],
                                packet["end_generate_timestamp_ns"]
                                - 50_000_000,
                                packet["end_generate_timestamp_ns"],
                                vibration["sample_rate_hz"],
                                vibration["sample_count"],
                                storage_path,
                                digest,
                                path.stat().st_size,
                                "valid",
                                time.time_ns(),
                            ),
                        )
                    if link_status == "accepted":
                        connection.execute(
                            "INSERT INTO review_context_packets("
                            "review_id,sender_id,packet_id,"
                            "relative_position,role"
                            ") VALUES (?,?,?,?,?)",
                            (
                                review_id,
                                sender_id,
                                packet_id,
                                relative_position,
                                role,
                            ),
                        )
                    connection.commit()
                    created_file = False
                except Exception:
                    if created_file:
                        path.unlink(missing_ok=True)
                        _fsync_directory(path.parent)
                    raise
            return (
                "duplicate"
                if raw_status == "duplicate"
                and link_status == "duplicate"
                else "accepted",
                None,
            )
        except Exception:
            if created_file:
                path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def end_timestamp(
        self, *, sender_id: str, packet_id: str
    ) -> int | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT end_generate_timestamp_ns FROM raw_packet_index "
                "WHERE sender_id=? AND packet_id=?",
                (sender_id, packet_id),
            ).fetchone()
        return row["end_generate_timestamp_ns"] if row else None

    def load_indexed_packet(
        self, sender_id: str, packet_id: str
    ) -> tuple[dict, dict]:
        """Load an indexed packet and verify its immutable payload hash."""
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM raw_packet_index WHERE sender_id=? AND packet_id=?",
                (sender_id, packet_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"raw packet not found: {sender_id}/{packet_id}")
        index = dict(row)
        try:
            root = self.raw_root.resolve()
            path = (root / index["storage_path"]).resolve()
            path.relative_to(root)
        except (OSError, ValueError) as error:
            raise ValueError("indexed raw packet path escapes raw root") from error
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                packet = json.load(stream)
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("indexed raw packet is unavailable") from error
        if not isinstance(packet, dict):
            raise OSError("indexed raw packet is not an object")
        digest = hashlib.sha256(canonical_payload(packet)).hexdigest()
        if digest != index["payload_sha256"]:
            raise PayloadHashMismatch(sender_id, packet_id)
        return packet, index

    def _read_packet(self, storage_path: str) -> dict | None:
        path = self.raw_root / storage_path
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


class PayloadHashMismatch(ValueError):
    """Raised when an indexed raw packet differs from its recorded hash."""


def canonical_payload(packet: dict) -> bytes:
    normalized = {
        key: value for key, value in packet.items()
        if key != "validation_status"
    }
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


_canonical_payload = canonical_payload


def _require_safe_path_component(value: str, field: str) -> None:
    if _SAFE_PATH_COMPONENT.fullmatch(value) is None:
        raise ValueError(f"{field} contains unsupported path characters")


def _write_durable_gzip(path: Path, payload: bytes) -> None:
    with path.open("xb") as file_stream:
        with gzip.GzipFile(fileobj=file_stream, mode="wb") as gzip_stream:
            gzip_stream.write(payload)
        file_stream.flush()
        os.fsync(file_stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
