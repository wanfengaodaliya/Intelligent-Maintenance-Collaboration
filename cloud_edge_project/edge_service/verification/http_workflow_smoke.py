"""Exercise all three cloud review stages against a running cloud_service."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
for path in (PROJECT, PROJECT / "edge_service" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core.bearing_workflow_contracts import FINAL_EDGE, FinalPacketResult
from edge_aggregation import BearingAggregationWorkflow, HttpCloudReviewGateway
from edge_validation_cache import EdgeValidationCache


class SmokeCache:
    raw_ref_from_uri = staticmethod(EdgeValidationCache.raw_ref_from_uri)
    raw_data_uri = staticmethod(EdgeValidationCache.raw_data_uri)

    def __init__(self):
        self.packets = {}
        self.pins = {}

    def add(self, sender: str, task: str, bearing: str, sequence: int) -> str:
        ref = (sender, task, sequence)
        self.packets[ref] = {
            "device_id": "smoke-device",
            "task_id": task,
            "bearing_id": bearing,
            "sender_id": sender,
            "packet_id": "%s-packet-%d" % (bearing, sequence),
            "sequence_number": sequence,
            "data": {"vibration": {"values": [0.2, -0.2, 0.2, -0.2]}},
        }
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
            if not self.pin(ref):
                for item in pinned:
                    self.unpin(item)
                return False
            pinned.append(ref)
        return True

    def unpin_many(self, refs):
        for ref in refs:
            self.unpin(ref)


def _packet(cache, bearing, sender, sequence, grade, confidence=0.95):
    return FinalPacketResult(
        result_id="smoke-%s-%d" % (bearing, sequence),
        device_id="smoke-device",
        task_id="smoke-task",
        bearing_id=bearing,
        sender_id=sender,
        packet_id="%s-packet-%d" % (bearing, sequence),
        sequence_number=sequence,
        action_grade=grade,
        confidence=confidence,
        data_quality_score=1.0,
        risk_level="low" if grade == 0 else "medium",
        decision_source=FINAL_EDGE,
        raw_data_ref=cache.add(sender, "smoke-task", bearing, sequence),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloud-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    cache = SmokeCache()
    workflow = BearingAggregationWorkflow(
        cache=cache,
        cloud=HttpCloudReviewGateway(args.cloud_url, poll_interval_seconds=0.02),
    )
    workflow.register_task(
        "smoke-device", "smoke-task", ("bearing-1", "bearing-2")
    )
    result = None
    for sequence in range(1, 81):
        confidence = 0.5 if sequence == 1 else 0.95
        result = workflow.accept_packet(
            _packet(cache, "bearing-1", "sender-1", sequence, 0, confidence)
        )
    if result is None or result.status != "WAITING":
        raise RuntimeError("first bearing did not leave the device waiting")
    for sequence in range(1, 81):
        grade = 0 if sequence <= 12 else 2
        result = workflow.accept_packet(
            _packet(cache, "bearing-2", "sender-2", sequence, grade)
        )
    if result is None or result.status != "FINAL" or result.decision_source != "CLOUD":
        raise RuntimeError("device cloud arbitration did not complete")
    if cache.pins:
        raise RuntimeError("raw cache references remained pinned")
    print(json.dumps({
        "status": "PASS",
        "packet_review": "FINAL_CLOUD",
        "bearing_window_review": "FINAL_CLOUD",
        "device_review": result.status,
        "expected_bearing_ids": list(result.expected_bearing_ids),
        "action_grade": result.action_grade,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
