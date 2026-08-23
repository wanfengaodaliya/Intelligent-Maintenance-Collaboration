from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SENDER_MODULE = Path(__file__).resolve().parents[1]


def test_documented_sender_module_launch_needs_no_pythonpath() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-m", "sender", "--help"],
        cwd=SENDER_MODULE,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Replay three independent bearing senders" in result.stdout
