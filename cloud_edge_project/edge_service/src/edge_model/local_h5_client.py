"""Legacy import path for the bearing H5 runtime client."""

from compatibility.bearing_v12.edge_h5_runtime_exports import (
    H5ActivationError,
    H5_DIAGNOSIS_LABELS,
    H5_RUNTIME_MODEL_VERSION,
    MODEL_TYPE_DISTILLED_H5,
    LocalH5ClientConfig,
    LocalH5ModelClient,
)

__all__ = [
    "H5ActivationError",
    "H5_DIAGNOSIS_LABELS",
    "H5_RUNTIME_MODEL_VERSION",
    "MODEL_TYPE_DISTILLED_H5",
    "LocalH5ClientConfig",
    "LocalH5ModelClient",
]
