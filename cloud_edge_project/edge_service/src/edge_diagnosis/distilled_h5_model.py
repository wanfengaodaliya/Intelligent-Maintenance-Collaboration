"""Compatibility shim for the scenario-owned distilled bearing H5 runner."""

from compatibility.bearing_v12.edge_h5_exports import (
    H5_LABELS,
    RUNTIME_MODEL_VERSION,
    DistilledH5DiagnosticModel,
    H5ModelArtifactError,
)

__all__ = [
    "H5_LABELS",
    "RUNTIME_MODEL_VERSION",
    "H5ModelArtifactError",
    "DistilledH5DiagnosticModel",
]
