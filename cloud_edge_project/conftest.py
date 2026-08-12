"""Test environment shared by project test suites."""

import os
from pathlib import Path


PROJECT_ROOT = str(Path(__file__).resolve().parent)
existing = os.environ.get("PYTHONPATH", "")
if PROJECT_ROOT not in existing.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (PROJECT_ROOT, existing)))
