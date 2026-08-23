"""Stable plugin provider for the verified bearing arbitration rules."""

from scenarios.bearing.cloud.device_arbitration.adapter import (
    BearingDeviceArbitrationAdapter,
)


class BearingArbitrationPolicy(BearingDeviceArbitrationAdapter):
    """Expose the existing bearing adapter as an arbitration capability."""

