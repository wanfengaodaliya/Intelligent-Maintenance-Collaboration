from __future__ import annotations

import sys
from pathlib import Path


SENDER_MODULE = Path(__file__).resolve().parents[1]
if str(SENDER_MODULE) not in sys.path:
    sys.path.insert(0, str(SENDER_MODULE))
