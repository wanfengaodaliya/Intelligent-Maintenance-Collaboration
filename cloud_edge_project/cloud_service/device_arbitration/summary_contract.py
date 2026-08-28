"""Compatibility exports for the bearing summary arbitration contract."""

from compatibility.bearing_v12.summary_arbitration_exports import (
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
