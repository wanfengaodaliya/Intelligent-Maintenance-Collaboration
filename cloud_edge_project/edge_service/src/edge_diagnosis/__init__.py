"""Temporary deterministic diagnostic model used until the trained model is ready."""

from .mock_model import MockDiagnosticModel, quality_score_from_perception

__all__ = ["MockDiagnosticModel", "quality_score_from_perception"]
