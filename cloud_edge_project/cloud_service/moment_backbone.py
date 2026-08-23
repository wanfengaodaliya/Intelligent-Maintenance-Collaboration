"""Compatibility shim for the scenario-owned MOMENT backbone loader."""

from compatibility.bearing_v12.cloud_moment_exports import (
    load_moment_backbone,
)


__all__ = ["load_moment_backbone"]
