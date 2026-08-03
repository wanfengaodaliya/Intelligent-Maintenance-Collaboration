from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class LocalLogError(RuntimeError):
    pass


class LocalLogSink:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.packet_path = self.directory / "packet_logs.jsonl"
        self.task_path = self.directory / "task_logs.jsonl"
        self._lock = threading.Lock()

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            with self._lock, path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
        except (OSError, TypeError, ValueError) as exc:
            raise LocalLogError(f"cannot write local log: {exc}") from exc

    def write_packet(self, record: dict[str, Any]) -> None:
        self._append(self.packet_path, record)

    def write_task(self, record: dict[str, Any]) -> None:
        self._append(self.task_path, record)
