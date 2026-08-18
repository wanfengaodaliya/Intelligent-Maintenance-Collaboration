"""Keep capture/freeze failures off the real-time diagnosis path."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Mapping

from .contracts import InsertOutcome
from .freezer import RawSampleFreezer
from .policy import RawSampleCapturePolicy
from .repository import RawSampleRepository


class RawSampleCaptureService:
    def __init__(
        self,
        policy: RawSampleCapturePolicy,
        freezer: RawSampleFreezer,
        repository: RawSampleRepository,
        *,
        max_packets_per_subject: int = 32,
    ) -> None:
        self.policy = policy
        self.freezer = freezer
        self.repository = repository
        self._packets: dict[tuple[str, str, str, str], deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=max_packets_per_subject)
        )

    def record_packet(self, packet: Mapping[str, Any]) -> None:
        key = tuple(packet[field] for field in ("device_id", "task_id", "bearing_id", "sender_id"))
        self._packets[key].append(dict(packet))

    def capture(self, event: Mapping[str, Any]) -> InsertOutcome | None:
        decision = self.policy.evaluate(event)
        if decision is None:
            return None
        key = (decision.device_id, decision.task_id, decision.bearing_id, decision.sender_id)
        sample = self.freezer.freeze(decision, tuple(self._packets[key]))
        return self.repository.enqueue(sample)
