"""Compatibility shim — re-exports from the bearing scenario implementation.

The real implementation lives in scenarios/bearing/_compat/bearing_workflow_contracts.py.
This file keeps old import paths (from core.bearing_workflow_contracts import ...) working.
"""

from scenarios.bearing._compat.bearing_workflow_contracts import *  # noqa: F401, F403