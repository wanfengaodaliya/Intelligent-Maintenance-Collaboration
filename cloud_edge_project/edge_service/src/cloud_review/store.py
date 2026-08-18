"""Atomic edge-disk storage for raw packets and lightweight decision checkpoints."""
# 该模块以原子方式保存原始数据包和轻量级决策检查点。

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .contracts import CloudReviewError, validate_record


class CloudReviewStore:
    def __init__(self, root: Path | str, *, retention_ns: int = 86_400_000_000_000) -> None:
        self.root = Path(root)
        self.packet_root = self.root / "packets"
        self.decision_root = self.root / "decisions"
        self.packet_root.mkdir(parents=True, exist_ok=True)
        self.decision_root.mkdir(parents=True, exist_ok=True)
        self.retention_ns = retention_ns
        self._lock = threading.RLock()

    def save(
        self,
        raw_packet: Mapping[str, Any],
        edge_perception_result: Mapping[str, Any],
        *,
        stored_at_ns: int | None = None,
    ) -> dict[str, Any]:
        raw, edge = validate_record(raw_packet, edge_perception_result)
        stored_at = time.time_ns() if stored_at_ns is None else stored_at_ns
        record = {
            "stored_at_ns": stored_at,
            "expires_at_ns": stored_at + self.retention_ns,
            "raw_packet": raw,
            "edge_perception_result": edge,
        }
        path = self._packet_path(raw["task_id"], raw["bearing_id"], raw["packet_id"])
        with self._lock:
            existing = self._read(path)
            if existing is not None:
                existing_content = {
                    "raw_packet": existing.get("raw_packet"),
                    "edge_perception_result": existing.get("edge_perception_result"),
                }
                requested_content = {
                    "raw_packet": raw,
                    "edge_perception_result": edge,
                }
                if _canonical(existing_content) != _canonical(requested_content):
                    raise CloudReviewError("CLOUD_REVIEW_RECORD_CONFLICT", "packet identity already has different data", 409)
                return existing
            self._write(path, record)
        return record

    def get(self, task_id: str, bearing_id: str, packet_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(self._packet_path(task_id, bearing_id, packet_id))

    def release(self, task_id: str, bearing_id: str, packet_id: str) -> bool:
        path = self._packet_path(task_id, bearing_id, packet_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
        return True

    def cleanup_expired(self, *, now_ns: int | None = None) -> int:
        now = time.time_ns() if now_ns is None else now_ns
        removed = 0
        with self._lock:
            for path in self.packet_root.rglob("*.json"):
                record = self._read(path)
                if record is not None and int(record.get("expires_at_ns", 0)) <= now:
                    path.unlink()
                    removed += 1
        return removed

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(self._decision_path(decision_id))

    def list_decisions(self, *, phase: str | None = None) -> tuple[dict[str, Any], ...]:
        """列出当前决策检查点，可按阶段过滤。"""
        records: list[dict[str, Any]] = []
        with self._lock:
            for path in sorted(self.decision_root.glob("*.json")):
                record = self._read(path)
                if record is None:
                    continue
                if phase is not None and record.get("phase") != phase:
                    continue
                records.append(record)
        return tuple(records)

    def save_decision(
        self,
        control: Mapping[str, Any],
        *,
        phase: str,
        review_id: str | None = None,
        response: Mapping[str, Any] | None = None,
        attempt_count: int | None = None,
        next_retry_at_ns: int | None = None,
    ) -> dict[str, Any]:
        decision_id = str(control["decision_id"])
        path = self._decision_path(decision_id)
        with self._lock:
            existing = self._read(path)
            if existing is not None and _canonical(existing["control"]) != _canonical(control):
                raise CloudReviewError("CLOUD_REVIEW_DECISION_CONFLICT", "decision_id has different control data", 409)
            record = {
                "control": dict(control),
                "phase": phase,
                "review_id": review_id,
                "response": dict(response) if response is not None else None,
                "attempt_count": attempt_count,
                "next_retry_at_ns": next_retry_at_ns,
                "updated_at_ns": time.time_ns(),
            }
            self._write(path, record)
        return record

    def _packet_path(self, task_id: str, bearing_id: str, packet_id: str) -> Path:
        for value in (task_id, bearing_id, packet_id):
            if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in value):
                raise CloudReviewError("INVALID_CLOUD_REVIEW_KEY", "packet key contains unsafe characters")
        return self.packet_root / task_id / bearing_id / f"{packet_id}.json"

    def _decision_path(self, decision_id: str) -> Path:
        if not decision_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in decision_id):
            raise CloudReviewError("INVALID_CLOUD_REVIEW_KEY", "decision_id contains unsafe characters")
        return self.decision_root / f"{decision_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise CloudReviewError("CLOUD_REVIEW_STORE_CORRUPT", f"invalid record: {path}", 500)
        return value

    @staticmethod
    def _write(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
