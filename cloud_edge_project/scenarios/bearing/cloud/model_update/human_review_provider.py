"""Confirmed maintenance labels stored independently from online packets."""

from __future__ import annotations

from typing import Any

from cloud_service.model_update.dataset_repository import LabelConfirmationRepository


class HumanReviewProvider:
    def __init__(self, repository: LabelConfirmationRepository):
        self.repository = repository

    def confirm(self, sample: dict[str, Any]) -> dict[str, Any] | None:
        packet_id = sample.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            return None
        value = self.repository.get(packet_id)
        if value is None or value["label_source"] != "human_confirmed":
            return None
        confirmation = {
            "packet_id": packet_id,
            "confirmed_label": value["confirmed_label"],
            "label_source": "human_confirmed",
        }
        if value.get("confirmed_risk_level") in {"normal", "warning", "fault"}:
            confirmation["confirmed_risk_level"] = value["confirmed_risk_level"]
        return confirmation
