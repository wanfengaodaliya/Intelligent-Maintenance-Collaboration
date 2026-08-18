"""Compatibility shim for the old bearing-scenario validation-metrics import path.

The real implementation now lives in
``cloud_service.model_update.classification_metrics``. This module only re-exports
it so existing imports of ``scenarios.bearing.cloud.model_update.validation_metrics``
keep working.
"""

from __future__ import annotations

from cloud_service.model_update.classification_metrics import (
    RISK_LEVELS,
    RISK_RANK,
    classification_metrics,
)

__all__ = ["RISK_LEVELS", "RISK_RANK", "classification_metrics"]