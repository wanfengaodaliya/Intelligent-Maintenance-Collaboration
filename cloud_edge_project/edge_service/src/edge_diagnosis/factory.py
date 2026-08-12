"""Select the in-process edge diagnostic runner explicitly."""

from __future__ import annotations

import os
from collections.abc import Mapping

from edge_model.config import EdgeModelConfig

from .mock_model import MockDiagnosticModel
from .random_forest_model import RandomForestDiagnosticModel


def diagnostic_runner_from_environment(
    config: EdgeModelConfig,
    environ: Mapping[str, str] | None = None,
):
    values = os.environ if environ is None else environ
    if config.diagnostic_backend in {"mock", "http"}:
        return MockDiagnosticModel(config.fallback.rule_version)
    if config.diagnostic_backend == "rf_50ms_integration":
        model_path = values.get("EDGE_RF_MODEL_PATH")
        metadata_path = values.get("EDGE_RF_METADATA_PATH")
        if not model_path or not metadata_path:
            raise ValueError(
                "rf_50ms_integration requires EDGE_RF_MODEL_PATH and "
                "EDGE_RF_METADATA_PATH"
            )
        return RandomForestDiagnosticModel(model_path, metadata_path)
    raise ValueError("unsupported diagnostic backend: %s" % config.diagnostic_backend)
