"""V1.2 bearing-result lifecycle and edge-authoritative persistence."""

from .manager import BearingResultLifecycleManager
from .repository import BearingResultRepository

__all__ = ["BearingResultLifecycleManager", "BearingResultRepository"]
