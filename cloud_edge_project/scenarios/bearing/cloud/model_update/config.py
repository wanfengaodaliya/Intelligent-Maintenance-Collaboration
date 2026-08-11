"""Bearing scenario configuration loaders for offline update data."""

from __future__ import annotations

import json
from pathlib import Path


def load_label_mapping(path: Path | None) -> dict[str, object]:
    if path is None or not Path(path).is_file():
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("PADERBORN_LABEL_MAPPING_MUST_BE_OBJECT")
    return {str(code).upper(): mapping for code, mapping in value.items()}
