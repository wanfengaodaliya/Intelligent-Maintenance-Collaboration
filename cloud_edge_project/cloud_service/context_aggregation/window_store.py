from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import numpy as np

from .contracts import AggregationError, RawWindow


class WindowStore:
    def __init__(self, database_path: Path):
        self.root = Path(database_path).parent / "aggregation"

    def write(self, relative_path: str, window: RawWindow) -> tuple[str, str]:
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise AggregationError("WINDOW_FILE_CONFLICT", f"window file already exists: {relative_path}")
        handle = tempfile.NamedTemporaryFile(dir=target.parent, suffix=".npz", delete=False)
        temporary = Path(handle.name)
        handle.close()
        try:
            np.savez(temporary, **window.channels, relative_positions=window.relative_positions, packet_start_samples=window.packet_start_samples, sample_rate_hz=np.asarray([64_000], dtype=np.int64))
            with temporary.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return f"aggregation/{relative_path}", _sha256(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
