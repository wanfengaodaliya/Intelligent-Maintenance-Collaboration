"""Aggregate final packet decisions into fixed, non-overlapping 20-packet windows."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean

from core.bearing_workflow_contracts import (
    FINAL_EDGE,
    PACKETS_PER_WINDOW,
    REVIEW_NOT_REQUIRED,
    REVIEW_PENDING,
    BearingWindowResult,
    FinalPacketResult,
)


@dataclass(frozen=True)
class WindowConflictPolicy:
    min_valid_packets: int = 16
    min_data_quality_score: float = 0.80
    min_action_support_ratio: float = 0.20
    min_consensus_ratio: float = 0.60
    conflict_grade_span: int = 2
    high_risk_confidence: float = 0.85


class BearingWindowAggregator:
    def __init__(self, policy: WindowConflictPolicy | None = None):
        self.policy = policy or WindowConflictPolicy()
        self._packets: dict[tuple[str, str, str, int], dict[int, FinalPacketResult]] = {}
        self._final_windows: dict[tuple[str, str, str, int], BearingWindowResult] = {}

    def add_packet(self, packet: FinalPacketResult) -> BearingWindowResult | None:
        window_index = (packet.sequence_number - 1) // PACKETS_PER_WINDOW + 1
        key = (packet.device_id, packet.task_id, packet.bearing_id, window_index)
        bucket = self._packets.setdefault(key, {})
        previous = bucket.get(packet.sequence_number)
        if previous is not None:
            if previous != packet:
                raise ValueError("PACKET_RESULT_CONFLICT")
            return self._final_windows.get(key)
        bucket[packet.sequence_number] = packet
        start = (window_index - 1) * PACKETS_PER_WINDOW + 1
        if any(sequence not in bucket for sequence in range(start, start + PACKETS_PER_WINDOW)):
            return None
        if key in self._final_windows:
            return self._final_windows[key]
        return self._build_window(key, tuple(bucket[index] for index in range(start, start + 20)))

    def finalize_window(self, result: BearingWindowResult) -> BearingWindowResult:
        key = (result.device_id, result.task_id, result.bearing_id, result.window_index)
        if key not in self._packets:
            raise ValueError("WINDOW_NOT_FOUND")
        if result.review_required or result.review_status != "SUCCEEDED":
            raise ValueError("cloud-reviewed window must be final")
        self._final_windows[key] = result
        return result

    def final_windows(self, device_id: str, task_id: str, bearing_id: str) -> tuple[BearingWindowResult, ...]:
        values = [
            self._final_windows.get((device_id, task_id, bearing_id, index))
            for index in range(1, 5)
        ]
        return tuple(value for value in values if value is not None)

    def _build_window(
        self,
        key: tuple[str, str, str, int],
        packets: tuple[FinalPacketResult, ...],
    ) -> BearingWindowResult:
        device_id, task_id, bearing_id, window_index = key
        valid = tuple(packet for packet in packets if packet.valid)
        grades = Counter(packet.action_grade for packet in valid)
        quality = mean(packet.data_quality_score for packet in packets)
        confidence = mean(packet.confidence for packet in valid) if valid else 0.0
        reasons: list[str] = []
        if len(valid) < self.policy.min_valid_packets:
            reasons.append("INSUFFICIENT_VALID_PACKETS")
        if quality < self.policy.min_data_quality_score:
            reasons.append("LOW_DATA_QUALITY")
        if valid:
            supported = [
                grade for grade, count in grades.items()
                if count / len(valid) >= self.policy.min_action_support_ratio
            ]
            if supported and max(supported) - min(supported) >= self.policy.conflict_grade_span:
                reasons.append("ACTION_GRADE_CONFLICT")
            consensus = max(grades.values()) / len(valid)
            if consensus < self.policy.min_consensus_ratio:
                reasons.append("LOW_ACTION_CONSENSUS")
            dominant_grade = max(grades, key=lambda grade: (grades[grade], grade))
            if any(
                packet.action_grade - dominant_grade >= self.policy.conflict_grade_span
                and packet.confidence >= self.policy.high_risk_confidence
                for packet in valid
            ):
                reasons.append("HIGH_RISK_OUTLIER")
            action_grade = max(supported) if supported else max(grades)
        else:
            action_grade = 0
        review_required = bool(reasons)
        result = BearingWindowResult(
            result_id="bearing_window_%s_%s_%d" % (task_id, bearing_id, window_index),
            device_id=device_id,
            task_id=task_id,
            bearing_id=bearing_id,
            sender_id=packets[0].sender_id,
            window_index=window_index,
            sequence_start=(window_index - 1) * PACKETS_PER_WINDOW + 1,
            sequence_end=window_index * PACKETS_PER_WINDOW,
            packet_count=len(packets),
            valid_packet_count=len(valid),
            action_grade=action_grade,
            confidence=round(confidence, 4),
            data_quality_score=round(quality, 4),
            result_source=FINAL_EDGE,
            review_status=REVIEW_PENDING if review_required else REVIEW_NOT_REQUIRED,
            review_required=review_required,
            review_reasons=tuple(dict.fromkeys(reasons)),
            packet_result_ids=tuple(packet.result_id for packet in packets),
            raw_data_refs=tuple(packet.raw_data_ref for packet in packets),
        )
        if not review_required:
            self._final_windows[key] = result
        return result
