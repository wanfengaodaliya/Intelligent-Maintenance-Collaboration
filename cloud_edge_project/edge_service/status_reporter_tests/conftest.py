from __future__ import annotations

import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
for path in (PROJECT, PROJECT / "edge_service" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
