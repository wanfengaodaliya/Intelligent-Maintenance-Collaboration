"""Durable delivery of exact 20-packet bearing review windows."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from core.bearing_actions import ACTION_TO_STATE, action_for_grade
from core.bearing_workflow_contracts import REVIEW_QUEUED, BearingWindowResult


GIB = 1024**3


class WindowTransferError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 503):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class WindowReviewStore:
    """One atomic JSON bundle per window; delete only after cloud persistence ACK."""

    def __init__(
        self,
        root: Path | str,
        *,
        hard_limit_bytes: int = 20 * GIB,
        warning_bytes: int = 16 * GIB,
        reserved_free_bytes: int = 10 * GIB,
    ) -> None:
        self.root = Path(root)
        self.bundle_root = self.root / "bearing_windows"
        self.bundle_root.mkdir(parents=True, exist_ok=True)
        self.hard_limit_bytes = hard_limit_bytes
        self.warning_bytes = warning_bytes
        self.reserved_free_bytes = reserved_free_bytes
        self._lock = threading.RLock()

    def save(
        self, window: BearingWindowResult, raw_packets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if len(raw_packets) != 20:
            raise WindowTransferError(
                "WINDOW_REQUIRES_20_RAW_PACKETS",
                "bearing review window requires exactly 20 raw packets",
                status_code=400,
            )
        sequences = [int(packet.get("sequence_number", 0)) for packet in raw_packets]
        expected = list(range(window.sequence_start, window.sequence_end + 1))
        if sequences != expected:
            raise WindowTransferError(
                "WINDOW_RAW_SEQUENCE_MISMATCH",
                "raw packet sequence does not match the review window",
                status_code=400,
            )
        window_id = window.result_id
        path = self._path(window_id)
        record = {
            "window_id": window_id,
            "window": window.as_dict(),
            "raw_packets": _json_value(raw_packets),
            "bearing_review_id": None,
            "raw_context_request_id": None,
            "attempt_count": 0,
            "last_error": None,
            "created_at_ns": time.time_ns(),
            "updated_at_ns": time.time_ns(),
        }
        encoded = _encoded(record)
        with self._lock:
            existing = self._read(path)
            if existing is not None:
                if _fingerprint(existing) != _fingerprint(record):
                    raise WindowTransferError(
                        "WINDOW_REVIEW_CONFLICT",
                        "window identity already contains different data",
                        status_code=409,
                    )
                return existing
            self._ensure_capacity(len(encoded))
            self._write(path, encoded)
        return record

    def attach_context_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        requested = request.get("requested_packets")
        if not isinstance(requested, list) or len(requested) != 20:
            raise WindowTransferError(
                "INVALID_RAW_CONTEXT_REQUEST", "requested_packets must contain 20 items", status_code=400
            )
        candidates = self.pending()
        for record in candidates:
            window = record["window"]
            manifest = [
                {"packet_id": packet["packet_id"], "sequence_number": packet["sequence_number"]}
                for packet in record["raw_packets"]
            ]
            if (
                window["device_id"] == request.get("device_id")
                and window["task_id"] == request.get("task_id")
                and window["bearing_id"] == request.get("bearing_id")
                and window["sender_id"] == request.get("sender_id")
                and manifest == requested
            ):
                return self.update(
                    record["window_id"],
                    raw_context_request_id=request.get("request_id"),
                )
        raise WindowTransferError(
            "WINDOW_REVIEW_NOT_FOUND", "no queued window matches the raw-context request", status_code=404
        )

    def preflight(self, additional_bytes: int = 0) -> None:
        with self._lock:
            self._ensure_capacity(max(0, int(additional_bytes)))

    def preflight_packet(self, raw_packet: Mapping[str, Any]) -> None:
        # Reserve conservatively for the complete review window before the
        # ingress adapter acknowledges any contributing MQTT/HTTP packet.
        self.preflight(len(_encoded(raw_packet)) * 20)

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [self._read(path) for path in sorted(self.bundle_root.glob("*.json"))]
        return [record for record in records if record is not None]

    def update(self, window_id: str, **changes: Any) -> dict[str, Any]:
        path = self._path(window_id)
        with self._lock:
            record = self._read(path)
            if record is None:
                raise WindowTransferError("WINDOW_REVIEW_NOT_FOUND", window_id, status_code=404)
            record.update(changes)
            record["updated_at_ns"] = time.time_ns()
            self._write(path, _encoded(record))
        return record

    def release(self, window_id: str) -> bool:
        path = self._path(window_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
        return True

    def usage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.bundle_root.glob("*.json"))

    @property
    def warning(self) -> bool:
        return self.usage_bytes() >= self.warning_bytes

    def _ensure_capacity(self, incoming_bytes: int) -> None:
        used = self.usage_bytes()
        free = shutil.disk_usage(self.root).free
        if used + incoming_bytes > self.hard_limit_bytes or free - incoming_bytes < self.reserved_free_bytes:
            raise WindowTransferError(
                "CLOUD_REVIEW_CACHE_FULL",
                "edge cloud-review cache has reached its capacity guard",
            )

    def _path(self, window_id: str) -> Path:
        if not window_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in window_id):
            raise WindowTransferError("INVALID_WINDOW_ID", "window_id contains unsafe characters", status_code=400)
        return self.bundle_root / f"{window_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise WindowTransferError("WINDOW_REVIEW_STORE_CORRUPT", str(path))
        return value

    @staticmethod
    def _write(path: Path, encoded: bytes) -> None:
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class WindowReviewHttpClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request(path, "POST", payload)

    def get(self, path: str) -> dict[str, Any]:
        return self._request(path, "GET", None)

    def _request(
        self, path: str, method: str, payload: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        data = _encoded(payload) if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise WindowTransferError("CLOUD_HTTP_%d" % error.code, detail) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise WindowTransferError("CLOUD_UNAVAILABLE", str(error)) from error
        result = json.loads(body) if body else {}
        if not isinstance(result, dict):
            raise WindowTransferError("INVALID_CLOUD_RESPONSE", "cloud response must be an object")
        return result


class WindowReviewDispatcher:
    def __init__(
        self,
        store: WindowReviewStore,
        client: WindowReviewHttpClient,
        *,
        interval_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.client = client
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bearing-window-uploader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 2.0)
        self._thread = None

    def dispatch_pending(self) -> int:
        delivered = 0
        for record in self.store.pending():
            try:
                if self._deliver(record):
                    delivered += 1
            except Exception as error:
                self.store.update(
                    record["window_id"],
                    attempt_count=int(record.get("attempt_count", 0)) + 1,
                    last_error=type(error).__name__ + ": " + str(error),
                )
        return delivered

    def _deliver(self, record: dict[str, Any]) -> bool:
        review_id = record.get("bearing_review_id")
        request_id = record.get("raw_context_request_id")
        if review_id:
            status = self.client.get("/cloud/bearing-review/%s" % review_id)
            if status.get("status") == "SUCCEEDED" and status.get("received_packet_count") == 20:
                self.store.release(record["window_id"])
                return True
            request_id = request_id or status.get("raw_context_request_id")
        if not review_id:
            window = record["window"]
            created = self.client.post(
                "/cloud/bearing-review",
                {
                    "scenario_type": "bearing",
                    "device_id": window["device_id"],
                    "task_id": window["task_id"],
                    "bearing_id": window["bearing_id"],
                    "sender_id": window["sender_id"],
                    "edge_bearing_result": {
                        "bearing_state": ACTION_TO_STATE[action_for_grade(int(window["action_grade"]))],
                        "confidence": float(window["confidence"]),
                        "packet_count": 20,
                    },
                    "source_packet_manifest": [
                        {"packet_id": packet["packet_id"], "sequence_number": packet["sequence_number"]}
                        for packet in record["raw_packets"]
                    ],
                },
            )
            review_id = created.get("bearing_review_id")
            request_id = created.get("raw_context_request_id") or request_id
            if not isinstance(review_id, str) or not isinstance(request_id, str):
                raise WindowTransferError("INVALID_CLOUD_RESPONSE", "cloud review identifiers are missing")
            self.store.update(
                record["window_id"],
                bearing_review_id=review_id,
                raw_context_request_id=request_id,
            )
        response = self.client.post(
            "/cloud/raw-context-batches",
            {
                "request_id": request_id,
                "review_type": "bearing_review",
                "device_id": record["window"]["device_id"],
                "task_id": record["window"]["task_id"],
                "bearing_id": record["window"]["bearing_id"],
                "sender_id": record["window"]["sender_id"],
                "packets": record["raw_packets"],
            },
        )
        if response.get("status") != "accepted" or response.get("received_packet_count") != 20:
            raise WindowTransferError("CLOUD_PERSISTENCE_NOT_ACKNOWLEDGED", str(response))
        self.store.release(record["window_id"])
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.dispatch_pending()
            self._stop.wait(self.interval_seconds)


class DurableWindowReviewGateway:
    """Queue window reviews locally while retaining existing packet/device calls."""

    def __init__(self, delegate: Any, store: WindowReviewStore):
        self.delegate = delegate
        self.store = store

    def review_packet(self, packet: Any, raw_packet: dict[str, Any]) -> Any:
        return self.delegate.review_packet(packet, raw_packet)

    def review_bearing_window(
        self, window: BearingWindowResult, raw_packets: list[dict[str, Any]]
    ) -> BearingWindowResult:
        self.store.save(window, raw_packets)
        return replace(
            window,
            review_status=REVIEW_QUEUED,
            review_required=False,
        )

    def review_device(self, result: Any) -> Any:
        return self.delegate.review_device(result)


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _encoded(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(record: Mapping[str, Any]) -> bytes:
    return _encoded(
        {
            "window": record["window"],
            "raw_packets": record["raw_packets"],
        }
    )
