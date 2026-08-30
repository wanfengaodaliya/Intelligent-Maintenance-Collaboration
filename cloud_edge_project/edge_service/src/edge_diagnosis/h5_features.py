"""Compatibility shim for scenario-owned bearing H5 features."""

from compatibility.bearing_v12.edge_h5_exports import (
    _compute_single,
    normalize_features,
)

__all__ = ["_compute_single", "normalize_features"]
