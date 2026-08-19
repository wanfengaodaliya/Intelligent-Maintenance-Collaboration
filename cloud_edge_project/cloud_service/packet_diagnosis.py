"""Structured single-packet diagnosis independent of the LLM backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PacketDiagnosis:
    label: str
    confidence: float
    risk_level: str
    recommended_action: str


class DiagnosisModel(Protocol):
    """A replaceable structured model for one packet's documented features."""

    model_version: str

    def predict(self, features: dict[str, Any]) -> PacketDiagnosis:
        ...


class RuleBasedDiagnosisModel:
    """MVP packet diagnosis shared by cloud packet review and future edge adapters."""

    model_version = "bearing_packet_model_v1"

    def predict(self, features: dict[str, Any]) -> PacketDiagnosis:
        vibration = features["vibration"]
        imbalance = features["current_relationship"]["current_imbalance_ratio"]
        fault = vibration["rms"] >= 1.0 or imbalance >= 0.1
        if fault:
            return PacketDiagnosis(
                label="fault",
                confidence=0.93,
                risk_level="high",
                recommended_action="urgent_bearing_attention",
            )
        return PacketDiagnosis(
            label="normal",
            confidence=0.91,
            risk_level="low",
            recommended_action="record_only",
        )
