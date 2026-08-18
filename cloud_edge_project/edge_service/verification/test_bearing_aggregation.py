from __future__ import annotations

from dataclasses import replace

from core.bearing_workflow_contracts import (
    FINAL_CLOUD,
    FINAL_EDGE,
    REVIEW_SUCCEEDED,
    FinalPacketResult,
)
from edge_aggregation import BearingAggregationWorkflow, BearingWindowAggregator
from edge_validation_cache import EdgeValidationCache


class FakeCache:
    def __init__(self):
        self.packets = {}
        self.pins = {}

    raw_ref_from_uri = staticmethod(EdgeValidationCache.raw_ref_from_uri)
    raw_data_uri = staticmethod(EdgeValidationCache.raw_data_uri)

    def add(self, sender: str, task: str, sequence: int) -> str:
        ref = (sender, task, sequence)
        self.packets[ref] = {"sequence_number": sequence, "data": {"vibration": {"values": [0.1, -0.1]}}}
        return self.raw_data_uri(ref)

    def read(self, ref):
        return self.packets.get(ref)

    def pin(self, ref):
        if ref not in self.packets:
            return False
        self.pins[ref] = self.pins.get(ref, 0) + 1
        return True

    def unpin(self, ref):
        count = self.pins.get(ref, 0)
        if count <= 1:
            self.pins.pop(ref, None)
        else:
            self.pins[ref] = count - 1
        return bool(count)

    def pin_many(self, refs):
        pinned = []
        for ref in refs:
            if self.pin(ref):
                pinned.append(ref)
            else:
                for item in pinned:
                    self.unpin(item)
                return False
        return True

    def unpin_many(self, refs):
        for ref in refs:
            self.unpin(ref)


class FakeCloud:
    def __init__(self):
        self.packet_reviews = 0
        self.window_reviews = 0
        self.device_reviews = 0

    def review_packet(self, packet, raw_packet):
        self.packet_reviews += 1
        return replace(packet, decision_source=FINAL_CLOUD, confidence=0.95)

    def review_bearing_window(self, window, raw_packets):
        self.window_reviews += 1
        assert len(raw_packets) == 20
        return replace(
            window,
            result_source=FINAL_CLOUD,
            review_status=REVIEW_SUCCEEDED,
            review_required=False,
            review_reasons=(),
            action_grade=max(window.action_grade, 2),
        )

    def review_device(self, result):
        self.device_reviews += 1
        return replace(result, status="FINAL", decision_source="CLOUD")


def packet(cache, bearing, sender, sequence, grade=0, confidence=0.9):
    raw_ref = cache.add(sender, "task-1", sequence)
    return FinalPacketResult(
        result_id="result-%s-%d" % (bearing, sequence),
        device_id="device-1",
        task_id="task-1",
        bearing_id=bearing,
        sender_id=sender,
        packet_id="packet-%s-%d" % (bearing, sequence),
        sequence_number=sequence,
        action_grade=grade,
        confidence=confidence,
        data_quality_score=1.0,
        risk_level="low" if grade < 2 else "medium",
        decision_source=FINAL_EDGE,
        raw_data_ref=raw_ref,
    )


def test_window_conflict_uses_supported_grade_span():
    cache = FakeCache()
    aggregator = BearingWindowAggregator()
    result = None
    for sequence in range(1, 21):
        result = aggregator.add_packet(
            packet(cache, "bearing-1", "sender-1", sequence, 0 if sequence <= 12 else 2)
        )
    assert result is not None
    assert result.review_required is True
    assert "ACTION_GRADE_CONFLICT" in result.review_reasons


def test_workflow_waits_for_expected_bearings_and_reviews_device_conflict():
    cache = FakeCache()
    cloud = FakeCloud()
    workflow = BearingAggregationWorkflow(cache=cache, cloud=cloud)
    workflow.register_task("device-1", "task-1", ("bearing-1", "bearing-2"))

    result = None
    for sequence in range(1, 81):
        result = workflow.accept_packet(packet(cache, "bearing-1", "sender-1", sequence, 0))
    assert result is not None and result.status == "WAITING"

    for sequence in range(1, 81):
        result = workflow.accept_packet(packet(cache, "bearing-2", "sender-2", sequence, 2))
    assert result is not None and result.status == "FINAL"
    assert result.conflict is True
    assert cloud.device_reviews == 1
    assert cloud.window_reviews == 0


def test_conflicting_window_pins_exact_twenty_until_cloud_returns():
    cache = FakeCache()
    cloud = FakeCloud()
    workflow = BearingAggregationWorkflow(cache=cache, cloud=cloud)
    workflow.register_task("device-1", "task-1", ("bearing-1",))
    result = None
    for sequence in range(1, 81):
        grade = 0 if sequence <= 12 else 2
        result = workflow.accept_packet(packet(cache, "bearing-1", "sender-1", sequence, grade))
    assert result is not None and result.status == "READY"
    assert cloud.window_reviews == 1
    assert cache.pins == {}
