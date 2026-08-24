"""Session-scoped dashboard state and evaluation-only accuracy metrics."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import closing
from pathlib import Path
from typing import Any


EVENT_TO_COUNTER = {
    "device-result": "results",
    "suggestion": "suggestions",
    "input-packet": "packets",
}


class DashboardSession:
    """Keep one frontend-process session without retaining raw signal arrays."""

    def __init__(self, *, max_recent: int = 200, max_buckets: int = 60) -> None:
        self.session_id = uuid.uuid4().hex
        self.started_at_ns = time.time_ns()
        self.max_buckets = max_buckets
        self.stats = {"results": 0, "faults": 0, "suggestions": 0, "packets": 0}
        self.recent = {
            event_type: deque(maxlen=max_recent) for event_type in EVENT_TO_COUNTER
        }
        self.buckets: OrderedDict[int, dict[str, int]] = OrderedDict()

    def record(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        counter = EVENT_TO_COUNTER.get(event_type)
        if counter is None:
            return
        self.stats[counter] += 1
        is_fault = event_type == "device-result" and _is_fault(event.get("payload"))
        if is_fault:
            self.stats["faults"] += 1
        compact = {
            "type": event_type,
            "topic": event.get("topic"),
            "payload": _compact_payload(event_type, event.get("payload")),
            "ts": float(event.get("ts", time.time())),
        }
        self.recent[event_type].append(compact)
        bucket_ms = int(compact["ts"] * 1000) // 5000 * 5000
        bucket = self.buckets.setdefault(
            bucket_ms,
            {"packets": 0, "results": 0, "faults": 0, "suggestions": 0},
        )
        bucket[counter] += 1
        if is_fault:
            bucket["faults"] += 1
        while len(self.buckets) > self.max_buckets:
            self.buckets.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at_ns": self.started_at_ns,
            "stats": dict(self.stats),
            "recent": {key: list(values) for key, values in self.recent.items()},
            "buckets": [
                {"timestamp_ms": timestamp_ms, **counts}
                for timestamp_ms, counts in self.buckets.items()
            ],
        }


class BinaryAccuracyEvaluator:
    """Compare Edge binary decisions with sender-only Paderborn source proofs."""

    HEALTHY_CODE = re.compile(r"^K0\d{2}$", re.IGNORECASE)
    FAULT_CODE = re.compile(r"^K(?:A|I|B)\d{2}$", re.IGNORECASE)

    def __init__(
        self,
        sender_database: Path,
        cloud_database: Path,
        *,
        started_at_ns: int,
    ) -> None:
        self.sender_database = Path(sender_database)
        self.cloud_database = Path(cloud_database)
        self.started_at_ns = started_at_ns
        self._lock = threading.Lock()
        self._prediction_cursor = 0
        self._truth_connection: sqlite3.Connection | None = None
        self._truth_data_version: int | None = None
        self._truths: dict[tuple[str, str], str] = {}
        self._predictions: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._correct = 0
        self._total = 0
        self._unmatched = 0
        self._unknown_labels = 0

    def evaluate(self) -> dict[str, Any]:
        if not self.sender_database.is_file() or not self.cloud_database.is_file():
            return self._result(False, "database_not_found")
        with self._lock:
            try:
                changed = self._load_truths()
                changed = self._load_predictions() or changed
                if changed:
                    self._recompute()
            except sqlite3.Error as error:
                self._close_truth_connection()
                return self._result(False, type(error).__name__)
            return self._result(True, None)

    def close(self) -> None:
        with self._lock:
            self._close_truth_connection()

    def _close_truth_connection(self) -> None:
        if self._truth_connection is not None:
            self._truth_connection.close()
            self._truth_connection = None
        self._truth_data_version = None

    def _result(self, available: bool, error: str | None) -> dict[str, Any]:
        return {
            "available": available,
            "error": error,
            "accuracy": None if self._total == 0 else self._correct / self._total,
            "correct": self._correct,
            "total": self._total,
            "unmatched": self._unmatched,
            "unknown_labels": self._unknown_labels,
            "session_started_ns": self.started_at_ns,
        }

    def _load_truths(self) -> bool:
        if self._truth_connection is None:
            self._truth_connection = _read_only(self.sender_database)
        version = int(
            self._truth_connection.execute("PRAGMA data_version").fetchone()[0]
        )
        if version == self._truth_data_version:
            return False
        rows = self._truth_connection.execute(
            """SELECT task_id,bearing_id,MIN(source_bearing_code)
               FROM packet_source_mapping
               GROUP BY task_id,bearing_id
               HAVING COUNT(DISTINCT source_bearing_code)=1"""
        ).fetchall()
        self._truths = {
            (str(task_id), str(bearing_id)): str(source_code).upper()
            for task_id, bearing_id, source_code in rows
        }
        self._truth_data_version = version
        return True

    def _load_predictions(self) -> bool:
        with closing(_read_only(self.cloud_database)) as connection:
            rows = connection.execute(
                """SELECT rowid,revision,received_at_ns,payload_json
                   FROM cloud_bearing_diagnosis_result WHERE rowid>? ORDER BY rowid""",
                (self._prediction_cursor,),
            ).fetchall()
        for rowid, revision, received_at_ns, raw_payload in rows:
            self._prediction_cursor = max(self._prediction_cursor, int(rowid))
            if int(revision) != 1 or int(received_at_ns) < self.started_at_ns:
                continue
            try:
                payload = json.loads(str(raw_payload))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            key = (
                str(payload.get("device_id", "")),
                str(payload.get("task_id", "")),
                str(payload.get("decision_round_id", "")),
                str(payload.get("bearing_id", "")),
            )
            self._predictions.setdefault(key, payload)
        return bool(rows)

    def _recompute(self) -> None:
        self._correct = 0
        self._total = 0
        self._unmatched = 0
        self._unknown_labels = 0
        for key, payload in self._predictions.items():
            truth_key = (key[1], key[3])
            if truth_key not in self._truths:
                self._unmatched += 1
                continue
            expected = self._expected_state(self._truths[truth_key])
            if expected is None:
                self._unknown_labels += 1
                continue
            self._total += 1
            if str(payload.get("bearing_state", "")).lower() == expected:
                self._correct += 1

    @classmethod
    def _expected_state(cls, source_code: str) -> str | None:
        if cls.HEALTHY_CODE.fullmatch(source_code):
            return "normal"
        if cls.FAULT_CODE.fullmatch(source_code):
            return "fault"
        return None


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _is_fault(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    state = payload.get("final_state", payload.get("status"))
    return str(state).lower() == "fault"


def _compact_payload(event_type: str, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if event_type == "device-result":
        fields = (
            "final_state", "status", "confidence", "device_id", "risk_level",
            "has_conflict", "final_action_grade", "decision_round_id",
        )
        return {field: payload.get(field) for field in fields}
    if event_type == "suggestion":
        fields = ("priority", "device_id", "suggestion_type", "suggestion")
        return {field: payload.get(field) for field in fields}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    vibration = data.get("vibration") if isinstance(data.get("vibration"), dict) else {}
    speed = data.get("shaft_speed_rpm")
    if isinstance(speed, dict):
        values = speed.get("values")
        speed = values[-1] if isinstance(values, list) and values else None
    return {
        "device_id": payload.get("device_id"),
        "packet_id": payload.get("packet_id"),
        "edge_result": payload.get("edge_result"),
        "label": payload.get("label"),
        "data": {
            "vibration_sample_count": vibration.get("sample_count"),
            "speed": speed,
            "temperature": data.get("bearing_module_temperature_c"),
        },
    }
