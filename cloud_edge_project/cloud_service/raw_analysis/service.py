"""Cloud-side raw-sample receipt and physical-evidence persistence."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from scipy import stats

from cloud_service.raw_analysis.config import DEFAULT_ANALYSIS_CONFIG
from cloud_service.raw_analysis.envelope import analyze_envelope_spectrum
from cloud_service.raw_analysis.spectrum import analyze_spectrum


class RawAnalysisSampleService:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.payload_directory = self.database_path.parent / "raw_analysis_samples"
        self.payload_directory.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def accept(self, metadata: Mapping[str, Any], payload: bytes, *, received_at_ns: int) -> dict[str, str]:
        _validate(metadata, payload)
        sample_id = str(metadata["sample_id"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_sha256 FROM raw_analysis_sample WHERE sample_id=?", (sample_id,)
            ).fetchone()
            if row is not None:
                return {"status": "duplicate" if row["payload_sha256"] == metadata["payload_sha256"] else "conflict"}
            path = self._write_payload(sample_id, payload)
            connection.execute(
                """INSERT INTO raw_analysis_sample(
                sample_id,metadata_json,payload_sha256,storage_path,status,created_at_ns,received_at_ns
                ) VALUES (?,?,?,?, 'PENDING', ?, ?)""",
                (sample_id, _dump(metadata), metadata["payload_sha256"], str(path), metadata["created_at_ns"], received_at_ns),
            )
        return {"status": "accepted"}

    def get_sample(self, sample_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM raw_analysis_sample WHERE sample_id=?", (sample_id,)).fetchone()
        if row is None:
            return None
        value = json.loads(row["metadata_json"])
        value["status"] = row["status"]
        return value

    def claim_pending(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM raw_analysis_sample WHERE status='PENDING' ORDER BY received_at_ns,sample_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE raw_analysis_sample SET status='RUNNING' WHERE sample_id=?", (row["sample_id"],))
        return dict(row)

    def complete(self, row: Mapping[str, Any], result: Mapping[str, Any], *, now_ns: int) -> None:
        metadata = json.loads(row["metadata_json"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE raw_analysis_sample SET status='SUCCEEDED' WHERE sample_id=?", (row["sample_id"],))
            connection.execute(
                """INSERT INTO physical_evidence_result(
                evidence_id,sample_id,status,result_json,limitations_json,error_code,created_at_ns,updated_at_ns
                ) VALUES (?,?,?,?,?,NULL,?,?)
                ON CONFLICT(sample_id) DO UPDATE SET status=excluded.status,result_json=excluded.result_json,
                limitations_json=excluded.limitations_json,error_code=NULL,updated_at_ns=excluded.updated_at_ns""",
                (
                    "evidence_" + row["sample_id"], row["sample_id"], "SUCCEEDED", _dump(result),
                    _dump(result["limitations"]), now_ns, now_ns,
                ),
            )

    def fail(self, row: Mapping[str, Any], error: Exception, *, now_ns: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE raw_analysis_sample SET status='FAILED' WHERE sample_id=?", (row["sample_id"],))
            connection.execute(
                """INSERT INTO physical_evidence_result(
                evidence_id,sample_id,status,result_json,limitations_json,error_code,created_at_ns,updated_at_ns
                ) VALUES (?,?, 'FAILED',NULL,'[]',?,?,?)
                ON CONFLICT(sample_id) DO UPDATE SET status='FAILED',error_code=excluded.error_code,updated_at_ns=excluded.updated_at_ns""",
                ("evidence_" + row["sample_id"], row["sample_id"], type(error).__name__, now_ns, now_ns),
            )

    def get_evidence(self, sample_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM physical_evidence_result WHERE sample_id=?", (sample_id,)).fetchone()
        if row is None:
            return None
        return {"evidence_id": row["evidence_id"], "sample_id": sample_id, "status": row["status"],
                "result": None if row["result_json"] is None else json.loads(row["result_json"]),
                "error_code": row["error_code"]}

    def payload_for(self, row: Mapping[str, Any]) -> bytes:
        return Path(row["storage_path"]).read_bytes()

    def _write_payload(self, sample_id: str, payload: bytes) -> Path:
        target = self.payload_directory / f"{sample_id}.json"
        temporary = self.payload_directory / f".{sample_id}.tmp"
        temporary.write_bytes(payload)
        os.replace(temporary, target)
        return target

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS raw_analysis_sample(
                sample_id TEXT PRIMARY KEY,metadata_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,status TEXT NOT NULL,created_at_ns INTEGER NOT NULL,received_at_ns INTEGER NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS physical_evidence_result(
                evidence_id TEXT PRIMARY KEY,sample_id TEXT UNIQUE NOT NULL,status TEXT NOT NULL,
                result_json TEXT,limitations_json TEXT NOT NULL,error_code TEXT,created_at_ns INTEGER NOT NULL,updated_at_ns INTEGER NOT NULL)""")
            connection.execute("UPDATE raw_analysis_sample SET status='PENDING' WHERE status='RUNNING'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # AUD-09: commit on success, rollback on error, and always close.
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def build_physical_evidence(metadata: Mapping[str, Any], payload: bytes) -> dict[str, Any]:
    packets = json.loads(payload)["packets"]
    vibration = np.concatenate([np.asarray(packet["data"]["vibration"]["values"], dtype=np.float64) for packet in packets])
    rms = float(np.sqrt(np.mean(vibration * vibration)))
    peak = float(np.max(np.abs(vibration)))
    kurtosis = float(stats.kurtosis(vibration, fisher=False, bias=False)) if vibration.size > 3 and np.std(vibration) else 0.0
    frequencies = np.fft.rfftfreq(vibration.size, d=1.0 / metadata["sample_rate_hz"])
    amplitude = np.abs(np.fft.rfft(vibration))
    indexes = np.argsort(amplitude[1:])[-5:][::-1] + 1 if amplitude.size > 1 else []
    spectrum = analyze_spectrum(vibration, metadata["sample_rate_hz"], DEFAULT_ANALYSIS_CONFIG)
    envelope = analyze_envelope_spectrum(
        vibration, metadata["sample_rate_hz"], None, DEFAULT_ANALYSIS_CONFIG
    )
    return {
        "schema_version": "physical-evidence-result/1.0",
        "sample_id": metadata["sample_id"],
        "time_domain": {"rms": rms, "kurtosis": kurtosis, "crest_factor": peak / max(rms, 1e-12)},
        "frequency_domain": {
            "dominant_peaks_hz": [float(frequencies[index]) for index in indexes],
            "psd_band_energy": spectrum["band_energy"],
        },
        "envelope_spectrum": {"peaks": envelope["peaks"]},
        "bearing_frequency_evidence": [],
        "limitations": [
            {"code": "BEARING_METADATA_UNAVAILABLE", "severity": "warning", "message": "frequency matching was not available"}
        ],
    }


def _validate(metadata: Mapping[str, Any], payload: bytes) -> None:
    required = ("sample_id", "device_id", "task_id", "bearing_id", "sender_id", "decision_round_id", "payload_sha256", "packet_manifest", "sample_rate_hz", "sample_count", "created_at_ns")
    if metadata.get("schema_version") != "raw-analysis-sample/1.0" or any(not metadata.get(field) for field in required):
        raise ValueError("INVALID_RAW_ANALYSIS_SAMPLE")
    if hashlib.sha256(payload).hexdigest() != metadata["payload_sha256"]:
        raise ValueError("PAYLOAD_CHECKSUM_MISMATCH")
    manifest = metadata["packet_manifest"]
    if not isinstance(manifest, list) or any(manifest[index]["sequence_number"] + 1 != manifest[index + 1]["sequence_number"] for index in range(len(manifest) - 1)):
        raise ValueError("INVALID_PACKET_MANIFEST")


def _dump(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
