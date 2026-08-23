"""Legacy edge H5 exports backed by scenario-owned implementations."""

from scenarios.bearing.edge_inference.h5.features import (
    _compute_single,
    normalize_features,
)
from scenarios.bearing.edge_inference.h5.network import PhysicalFusionModel

__all__ = ["_compute_single", "normalize_features", "PhysicalFusionModel"]
