"""Backward-compatible exports for legacy V1.2 diagnosis identities."""

from compatibility.bearing_v12.diagnosis_identity import (
    DiagnosisIdentity,
    build_decision_round_id,
    build_diagnosis_window_id,
    build_run_id,
    build_summary_window_id,
    canonical_json,
)

__all__ = [
    "DiagnosisIdentity",
    "build_decision_round_id",
    "build_diagnosis_window_id",
    "build_run_id",
    "build_summary_window_id",
    "canonical_json",
]
