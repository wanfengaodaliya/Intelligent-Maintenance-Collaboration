from __future__ import annotations

import re
import threading
from pathlib import Path


SENDER_ID_PATTERN = re.compile(r"^sender_(\d{2,})$")


class TaskIdStore:
    def __init__(self, counter_path: Path | str, sender_id: str) -> None:
        match = SENDER_ID_PATTERN.fullmatch(sender_id)
        if not match:
            raise ValueError("sender_id must match sender_<number>")
        self.counter_path = Path(counter_path)
        self.sender_number = match.group(1)
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
            return f"sd_{self.sender_number}_tk_{next_number:04d}"
