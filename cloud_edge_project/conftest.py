"""Test environment shared by project test suites."""

import os
import sys
from pathlib import Path


PROJECT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = str(PROJECT_PATH)
existing = os.environ.get("PYTHONPATH", "")
if PROJECT_ROOT not in existing.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (PROJECT_ROOT, existing)))

for import_root in (
    PROJECT_PATH / "sender_module",
    PROJECT_PATH / "edge_service" / "src",
):
    import_path = str(import_root)
    if import_path not in sys.path:
        sys.path.insert(0, import_path)
