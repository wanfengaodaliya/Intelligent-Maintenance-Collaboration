"""Convert the frozen edge perception contract to the random-forest vector."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np


def feature_vector(
    perception: Mapping[str, Any], feature_columns: Sequence[str]
) -> np.ndarray:
    features = perception.get("features")
    if not isinstance(features, Mapping):
        raise ValueError("perception.features must be an object")

    values: list[float] = []
    for column in feature_columns:
        current: Any = features
        for component in column.split("."):
            if not isinstance(current, Mapping) or component not in current:
                raise ValueError("missing model feature: %s" % column)
            current = current[component]
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError("model feature must be numeric: %s" % column)
        value = float(current)
        if not math.isfinite(value):
            raise ValueError("model feature must be finite: %s" % column)
        values.append(value)
    return np.asarray([values], dtype=np.float64)
