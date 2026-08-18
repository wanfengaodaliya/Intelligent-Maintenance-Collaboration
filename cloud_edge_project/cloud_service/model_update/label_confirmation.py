"""Provider-neutral label confirmation and priority resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol


class LabelConfirmationProvider(Protocol):
    def confirm(self, sample: dict[str, Any]) -> dict[str, Any] | None: ...


LABEL_PRIORITY = {
    "cloud_reference": 1,
    "dataset_ground_truth": 2,
    "human_confirmed": 2,
}


class LabelConfirmationResolver:
    """Select the highest-quality valid confirmation returned by providers."""

    def __init__(
        self,
        providers: Iterable[
            LabelConfirmationProvider
            | Callable[[dict[str, Any]], dict[str, Any] | None]
        ],
    ) -> None:
        self.providers = tuple(providers)

    def confirm(self, sample: dict[str, Any]) -> dict[str, Any] | None:
        selected: dict[str, Any] | None = None
        selected_priority = 0
        for provider in self.providers:
            method = getattr(provider, "confirm", provider)
            confirmation = method(sample)
            if not _valid_confirmation(confirmation, sample.get("packet_id")):
                continue
            priority = LABEL_PRIORITY[confirmation["label_source"]]
            if priority > selected_priority:
                selected = dict(confirmation)
                selected_priority = priority
        return selected


class SnapshotLabelProvider:
    """Freeze one provider result per packet for a single dataset build."""

    def __init__(self, provider: LabelConfirmationProvider) -> None:
        self.provider = provider
        self._confirmations: dict[str, dict[str, Any] | None] = {}

    def confirm(self, sample: dict[str, Any]) -> dict[str, Any] | None:
        packet_id = sample.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            return self.provider.confirm(sample)
        if packet_id not in self._confirmations:
            confirmation = self.provider.confirm(sample)
            self._confirmations[packet_id] = (
                dict(confirmation) if confirmation is not None else None
            )
        stored = self._confirmations[packet_id]
        return dict(stored) if stored is not None else None


class CloudReferenceProvider:
    """Use the cloud review label only as the lowest-priority fallback."""

    def confirm(self, sample: dict[str, Any]) -> dict[str, Any] | None:
        packet_id = sample.get("packet_id")
        label = sample.get("cloud_label")
        if not isinstance(packet_id, str) or not packet_id or not isinstance(label, str) or not label:
            return None
        return {
            "packet_id": packet_id,
            "confirmed_label": label,
            "label_source": "cloud_reference",
        }


def _valid_confirmation(value: Any, packet_id: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("packet_id") == packet_id
        and isinstance(value.get("confirmed_label"), str)
        and bool(value["confirmed_label"])
        and value.get("label_source") in LABEL_PRIORITY
    )
