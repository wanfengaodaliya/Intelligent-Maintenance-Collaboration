"""Durable audit log for exhausted packet-routing attempts."""
# 该模块持久化记录包级路由重试耗尽后的结构化错误，便于审计和重放。

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping


class PacketRouteErrorRecorder:
    """Append structured errors to JSONL so retained packets can be replayed."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def __call__(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(
            dict(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
