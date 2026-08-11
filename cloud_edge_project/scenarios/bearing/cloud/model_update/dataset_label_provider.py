"""Paderborn ground-truth labels resolved through packet source proofs."""

from __future__ import annotations

import re
from typing import Any

from cloud_service.model_update.dataset_repository import PacketSourceRepository


PADERBORN_FILENAME = re.compile(
    r"^[^_]+_[^_]+_[^_]+_([A-Za-z](?:[A-Za-z]\d{2}|\d{3}))_\d+\.mat$",
    re.IGNORECASE,
)


def extract_paderborn_bearing_code(source_file: str) -> str | None:
    match = PADERBORN_FILENAME.fullmatch(source_file)
    return match.group(1).upper() if match else None


class DatasetLabelProvider:
    def __init__(
        self,
        source_repository: PacketSourceRepository,
        label_mapping: dict[str, Any],
    ) -> None:
        self.source_repository = source_repository
        self.label_mapping: dict[str, dict[str, str]] = {}
        for code, value in label_mapping.items():
            if isinstance(value, str) and value:
                self.label_mapping[str(code).upper()] = {
                    "confirmed_label": value
                }
            elif (
                isinstance(value, dict)
                and isinstance(value.get("confirmed_label"), str)
                and value["confirmed_label"]
                and value.get("confirmed_risk_level")
                in {None, "normal", "warning", "abnormal"}
            ):
                self.label_mapping[str(code).upper()] = dict(value)

    def confirm(self, sample: dict[str, Any]) -> dict[str, Any] | None:
        packet_id = sample.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            return None
        source = self.source_repository.get_by_packet_id(packet_id)
        if source is None:
            return None
        parsed_code = extract_paderborn_bearing_code(source["source_file"])
        stored_code = str(source["source_bearing_code"]).upper()
        if parsed_code is None or parsed_code != stored_code:
            return None
        mapped = self.label_mapping.get(stored_code)
        if mapped is None:
            return None
        confirmation = {
            "packet_id": packet_id,
            "confirmed_label": mapped["confirmed_label"],
            "label_source": "dataset_ground_truth",
        }
        if mapped.get("confirmed_risk_level") is not None:
            confirmation["confirmed_risk_level"] = mapped[
                "confirmed_risk_level"
            ]
        return confirmation
