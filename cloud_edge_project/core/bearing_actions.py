"""Compatibility shim — re-exports from the bearing scenario implementation.

The real implementation lives in scenarios/bearing/_compat/bearing_actions.py.
This file keeps old import paths (from core.bearing_actions import ...) working.
"""

from scenarios.bearing._compat.bearing_actions import *  # noqa: F401, F403