"""Load aggregation windows and device metadata from local SQLite storage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from cloud_service.storage.database import connect

from .config import AnalysisConfig
from .contracts import (
    AnalysisContext,
    BearingMetadata,
    EnhancedAnalysisError,
    LoadedWindow,
    REQUIRED_CHANNELS,
    REQUIRED_NPZ_KEYS,
    limitation,
)
from .repositories import BearingMetadataRepository


class EnhancedAnalysisLoader:
    def __init__(self, database_path: Path, bearing_repository: BearingMetadataRepository | None = None):
        self.database_path = Path(database_path)
        self.root = self.database_path.parent.resolve()
        self.bearing_repository = bearing_repository or BearingMetadataRepository(self.database_path)

    def load(
        self, aggregation: dict[str, Any], config: AnalysisConfig
    ) -> tuple[LoadedWindow, AnalysisContext]:
        review = self._review(aggregation["review_id"])
        relative_path = aggregation["preprocessed_window_path"]
        if not isinstance(relative_path, str) or not relative_path:
            raise EnhancedAnalysisError("WAVEFORM_LOAD_FAILED", "preprocessed_window_path is empty", retryable=True)
        target = self._resolved_window_path(relative_path)
        self._check_sha256(target, aggregation.get("preprocessed_window_sha256"))
        try:
            with np.load(target, allow_pickle=False) as data:
                missing = [name for name in REQUIRED_NPZ_KEYS if name not in data]
                if missing:
                    raise EnhancedAnalysisError(
                        "INVALID_WINDOW_SHAPE", f"missing npz keys: {missing}", retryable=False
                    )
                channels = {name: np.asarray(data[name], dtype=np.float64) for name in REQUIRED_CHANNELS}
                sample_rate = int(np.asarray(data["sample_rate_hz"]).reshape(-1)[0])
                relative_positions = tuple(int(value) for value in np.asarray(data["relative_positions"]).reshape(-1))
                packet_start_samples = tuple(
                    int(value) for value in np.asarray(data["packet_start_samples"]).reshape(-1)
                )
        except EnhancedAnalysisError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise EnhancedAnalysisError(
                "WAVEFORM_LOAD_FAILED", f"cannot parse preprocessed window: {error}", retryable=True
            ) from error

        if sample_rate != config.vibration_sample_rate_hz:
            raise EnhancedAnalysisError(
                "INVALID_WINDOW_SHAPE", "window sample rate does not match analysis config", retryable=False
            )
        sample_count = int(channels["vibration"].size)
        if sample_count < 1:
            raise EnhancedAnalysisError("INVALID_WINDOW_SHAPE", "window is empty", retryable=False)
        if any(channels[name].ndim != 1 or channels[name].size != sample_count for name in REQUIRED_CHANNELS):
            raise EnhancedAnalysisError(
                "INVALID_WINDOW_SHAPE", "high-rate channels must be one-dimensional and equal length",
                retryable=False,
            )

        limitations: list[dict[str, str]] = []
        speed_rpm = self._speed_rpm(
            review["device_id"], review["bearing_id"], review["sender_id"], aggregation
        )
        radial_load_n = self._radial_load_n(
            review["device_id"], review["bearing_id"], review["sender_id"], aggregation
        )
        if speed_rpm is None:
            limitations.append(limitation("speed_unavailable", "operating speed is unavailable"))
        timestamp_ns = review.get("start_timestamp_ns") or review["created_at_ns"]
        bearing_row = self.bearing_repository.active_for_bearing_at(
            review["device_id"], review["bearing_id"], timestamp_ns
        )
        bearing = None
        if bearing_row is None:
            limitations.append(limitation("bearing_metadata_missing", "bearing geometry metadata is missing"))
        else:
            bearing = BearingMetadata.from_mapping(bearing_row)
            if not bearing.valid_geometry():
                bearing = None
                limitations.append(limitation("bearing_metadata_missing", "bearing geometry metadata is invalid"))

        loaded_window = LoadedWindow(
            channels=channels,
            relative_positions=relative_positions,
            packet_start_samples=packet_start_samples,
            sample_rate_hz=sample_rate,
            start_timestamp_ns=review.get("start_timestamp_ns"),
            speed_rpm=speed_rpm,
            radial_load_n=radial_load_n,
            bearing=bearing,
            limitations=limitations,
        )
        context = AnalysisContext(
            review_id=aggregation["review_id"],
            device_id=review["device_id"],
            task_id=review["task_id"],
            bearing_id=review["bearing_id"],
            sender_id=review["sender_id"],
            anchor_packet_id=review["anchor_packet_id"],
            aggregation_result_id=aggregation["aggregation_id"],
            context_status=aggregation["context_status"],
            preprocessed_window_path=relative_path,
            preprocessed_window_sha256=aggregation.get("preprocessed_window_sha256") or "",
            sample_rate_hz=sample_rate,
            sample_count=sample_count,
            window_duration_ms=round(sample_count / sample_rate * 1000),
            frequency_resolution_hz=sample_rate / sample_count,
            relative_positions=relative_positions,
            packet_start_samples=packet_start_samples,
            start_timestamp_ns=review.get("start_timestamp_ns"),
            speed_rpm=speed_rpm,
            radial_load_n=radial_load_n,
            bearing=bearing,
            limitations=limitations,
        )
        return loaded_window, context

    def _review(self, review_id: str) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM cloud_review WHERE review_id=?", (review_id,)
            ).fetchone()
        if row is None:
            raise EnhancedAnalysisError("REVIEW_NOT_FOUND", "review does not exist", retryable=False)
        return dict(row)

    def _resolved_window_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise EnhancedAnalysisError("WAVEFORM_LOAD_FAILED", "absolute window path is not allowed", retryable=True)
        target = (self.root / candidate).resolve()
        if not target.is_relative_to(self.root):
            raise EnhancedAnalysisError("WAVEFORM_LOAD_FAILED", "window path escapes database directory", retryable=True)
        if not target.is_file():
            raise EnhancedAnalysisError("WAVEFORM_LOAD_FAILED", "preprocessed window file is missing", retryable=True)
        return target

    @staticmethod
    def _check_sha256(target: Path, expected: str | None) -> None:
        if not expected:
            return
        expected = expected.removeprefix("sha256:").lower()
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise EnhancedAnalysisError(
                "WAVEFORM_LOAD_FAILED", "preprocessed window SHA-256 mismatch", retryable=True
            )

    def _manifest_packet_ids(self, aggregation: dict[str, Any]) -> list[str]:
        manifest = json.loads(aggregation.get("packet_manifest_json") or "[]")
        return [str(item["packet_id"]) for item in manifest if item.get("packet_id")]

    def _edge_rows(self, device_id: str, bearing_id: str, sender_id: str, aggregation: dict[str, Any]) -> list[dict[str, Any]]:
        packet_ids = self._manifest_packet_ids(aggregation)
        if not packet_ids:
            return []
        placeholders = ",".join("?" for _ in packet_ids)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM edge_packet_summary WHERE device_id=? AND bearing_id=? AND sender_id=? AND packet_id IN ("
                + placeholders
                + ") AND processing_status='perception_completed'",
                (device_id, bearing_id, sender_id, *packet_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def _speed_rpm(self, device_id: str, bearing_id: str, sender_id: str, aggregation: dict[str, Any]) -> float | None:
        values = [
            row["shaft_speed_rpm_mean"]
            for row in self._edge_rows(device_id, bearing_id, sender_id, aggregation)
            if row.get("shaft_speed_rpm_mean") is not None and row["shaft_speed_rpm_mean"] > 0
        ]
        return float(np.median(values)) if values else None

    def _radial_load_n(self, device_id: str, bearing_id: str, sender_id: str, aggregation: dict[str, Any]) -> float | None:
        values = [
            row["bearing_radial_load_n_mean"]
            for row in self._edge_rows(device_id, bearing_id, sender_id, aggregation)
            if row.get("bearing_radial_load_n_mean") is not None
            and row["bearing_radial_load_n_mean"] >= 0
        ]
        return float(np.median(values)) if values else None
