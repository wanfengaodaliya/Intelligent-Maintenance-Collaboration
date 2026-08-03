from __future__ import annotations

import json
import hashlib
import time
from collections.abc import Callable
from pathlib import Path

from cloud_service.storage.database import connect
import numpy as np


class AggregationReadyDispatcher:
    def __init__(self, database_path: Path, handler: Callable[[dict], None] | None = None):
        self.database_path, self.handler = Path(database_path), handler

    def dispatch_pending(self, *, limit: int = 20) -> int:
        if self.handler is None:
            return 0
        with connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM aggregation_outbox WHERE dispatch_status IN ('pending','failed') ORDER BY updated_at_ns LIMIT ?", (limit,)).fetchall()
        delivered = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                self._verify_artifact(payload)
                self.handler(payload)
                status, error = "delivered", None
                delivered += 1
            except Exception as exc:
                status, error = "failed", str(exc)
            with connect(self.database_path) as connection:
                connection.execute("UPDATE aggregation_outbox SET dispatch_status=?,attempt_count=attempt_count+1,last_error=?,updated_at_ns=? WHERE outbox_id=?", (status, error, time.time_ns(), row["outbox_id"]))
        return delivered

    def _verify_artifact(self, payload: dict) -> None:
        relative_path = payload["preprocessed_window_path"]
        path = self.database_path.parent / relative_path
        with np.load(path) as window:
            required = {"vibration", "phase_current_1_A", "phase_current_2_A", "relative_positions", "packet_start_samples", "sample_rate_hz"}
            if not required.issubset(window.files):
                raise ValueError("WINDOW_ARTIFACT_CORRUPT")
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT preprocessed_window_sha256 FROM aggregation_result WHERE aggregation_id=?", (payload["aggregation_id"],)).fetchone()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if row is None or row["preprocessed_window_sha256"] != digest:
            raise ValueError("WINDOW_ARTIFACT_CORRUPT")
