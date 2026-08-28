"""Compatibility exports for the bearing summary outbox."""

from scenarios.bearing.summary_service.outbox import (
    ArbitrationOutbox,
    PermanentDeliveryError,
)

__all__ = ["ArbitrationOutbox", "PermanentDeliveryError"]
