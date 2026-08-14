"""轴承场景的边缘感知装配。"""

from .config import build_bearing_perception_config
from .handler import BearingEdgePerceptionHandler

__all__ = [
    "BearingEdgePerceptionHandler",
    "build_bearing_perception_config",
]
