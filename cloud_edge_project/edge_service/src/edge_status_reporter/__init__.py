"""Low-intrusion Edge status reporting integration."""

from .bootstrap import EdgeStatusIntegration, build_edge_status_integration
from .collectors import AcceleratorDetector, build_resource_collector
from .config import (
    AcceleratorConfig,
    EdgeStatusReporterConfig,
    ResourceConfig,
    StatusTargetConfig,
)
from .contracts import (
    AcceleratorSnapshot,
    BusinessStatusSnapshot,
    EdgeStatusReport,
    ModelStatus,
    ResourceSnapshot,
)
from .middleware import EdgeActivityMiddleware
from .reporter import EdgeStatusReporter
from .state import EdgeApplicationState
from .transport import HttpStatusTarget

__all__ = [
    "AcceleratorConfig",
    "AcceleratorDetector",
    "AcceleratorSnapshot",
    "BusinessStatusSnapshot",
    "EdgeActivityMiddleware",
    "EdgeApplicationState",
    "EdgeStatusIntegration",
    "EdgeStatusReport",
    "EdgeStatusReporter",
    "EdgeStatusReporterConfig",
    "HttpStatusTarget",
    "ModelStatus",
    "ResourceConfig",
    "ResourceSnapshot",
    "StatusTargetConfig",
    "build_edge_status_integration",
    "build_resource_collector",
]
