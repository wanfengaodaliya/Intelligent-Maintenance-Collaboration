"""Trainer boundary retained for a future concrete edge model choice."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class EdgeModelTrainer(Protocol):
    def train(
        self, dataset_manifest: dict[str, Any], output_dir: Path
    ) -> dict[str, Any]: ...
