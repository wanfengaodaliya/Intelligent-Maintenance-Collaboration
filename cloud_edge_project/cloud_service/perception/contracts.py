"""Contracts shared by cloud perception validation and processing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationResult:
    """Quality outcome for one CloudReviewRequest."""

    valid: bool
    blocking_issues: list[str]
    warning_flags: list[str]
