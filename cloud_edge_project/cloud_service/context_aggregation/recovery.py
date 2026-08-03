from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from cloud_service.storage.database import connect


class WindowRecoveryScanner:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.root = self.database_path.parent / "aggregation"

    def warn_orphan_files(self) -> int:
        if not self.root.exists():
            return 0
        with connect(self.database_path) as connection:
            rows = connection.execute("SELECT raw_window_path,preprocessed_window_path FROM aggregation_result").fetchall()
        known = {path for row in rows for path in row if path}
        count = 0
        for path in self.root.rglob("*.npz"):
            relative = str(path.relative_to(self.database_path.parent)).replace("\\", "/")
            if relative not in known:
                logging.getLogger(__name__).warning("orphan aggregation window path=%s bytes=%s sha256=%s", relative, path.stat().st_size, _sha256(path))
                count += 1
        return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest
