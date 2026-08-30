"""Explicit compatibility exports for bearing summary arbitration."""

from scenarios.bearing.cloud.device_arbitration.summary_contract import (
    BINARY_BEARING_STATES,
    EXPECTED_BEARING_IDS,
    EXPECTED_EDGE_NODE_IDS,
    adapt_summary_arbitration_request,
    attach_summary_identity,
)

__all__ = [
    "BINARY_BEARING_STATES",
    "EXPECTED_BEARING_IDS",
    "EXPECTED_EDGE_NODE_IDS",
    "adapt_summary_arbitration_request",
    "attach_summary_identity",
]
