from __future__ import annotations

import threading
from pathlib import Path


class TaskIdStore:
    def __init__(self, counter_path: Path | str) -> None:
        self.counter_path = Path(counter_path)
        self._lock = threading.Lock()

    def next_task_id(self) -> str:
        with self._lock:
            self.counter_path.parent.mkdir(parents=True, exist_ok=True)
            current = 0
            if self.counter_path.exists():
                text = self.counter_path.read_text(encoding="ascii").strip()
                if text:
                    current = int(text)
            next_number = current + 1
            self.counter_path.write_text(str(next_number), encoding="ascii")
            return f"task_{next_number:05d}"

