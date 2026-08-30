"""Standalone sender module for the cloud-edge project."""

from __future__ import annotations

import sys
from pathlib import Path


# Preserve ``cd sender_module; python -m sender`` while the sender assembly
# resolves project-level scenario plugins.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

