from __future__ import annotations

from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from packet_routing_bridge import PacketRoutingBridge
from diagnosis_window import DiagnosisWindowAssembler


class _Store:
    def save(self, raw_packet, edge_perception_result):
        self.raw_packet = raw_packet
        self.edge_perception_result = edge_perception_result


def test_bridge_sends_deterministic_window_and_round_identity() -> None:
    captured: dict = {}
    store = _Store()
    bridge = PacketRoutingBridge(
        edge_node_id="edge_01",
        store=store,
        post=lambda _path, payload: captured.update(payload) or {"route": "EDGE"},
    )
    completion = PacketExecutionCompleted(
        request_id="request_01", device_id="machine_01", bearing_id="bearing_02",
        task_id="task_001", packet_id="packet_001", sender_id="sender_02",
        sequence_number=1, status="SUCCEEDED", error_code=None,
        started_at_ns=1, finished_at_ns=2,
        edge=EdgeResult("normal", 0.95, "low", "edge_model_v1"),
        perception={
            "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_02",
            "sender_id": "sender_02", "packet_id": "packet_001", "sequence_number": 1,
        },
    )
    raw = {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_02",
        "sender_id": "sender_02", "packet_id": "packet_001", "sequence_number": 1,
    }

    bridge.route(raw, completion)

    assert captured["decision_round_id"] == build_decision_round_id(
        device_id="machine_01", task_id="task_001", window_start_sequence=1, window_end_sequence=1,
    )
    assert captured["diagnosis_window_id"] == build_diagnosis_window_id(
        device_id="machine_01", task_id="task_001", bearing_id="bearing_02", sender_id="sender_02",
        window_start_sequence=1, window_end_sequence=1,
    )
    assert captured["window_start_sequence"] == captured["window_end_sequence"] == 1


def test_bridge_transmits_a_diagnosis_window_identity_without_rebuilding_it() -> None:
    captured: dict = {}
    bridge = PacketRoutingBridge(
        edge_node_id="edge_01", store=_Store(),
        post=lambda _path, payload: captured.update(payload) or {"route": "EDGE"},
    )
    packets = [
        {
            "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_02",
            "sender_id": "sender_02", "packet_id": f"packet_{sequence:03d}",
            "sequence_number": sequence,
            "start_generate_timestamp_ns": (sequence - 1) * 50_000_000,
            "end_generate_timestamp_ns": sequence * 50_000_000,
            "data": {"vibration": {"sample_rate_hz": 64_000}},
        }
        for sequence in (1,)
    ]
    assembler = DiagnosisWindowAssembler(window_ms=50)
    window = next(result for packet in packets for result in assembler.append(packet))
    raw = {
        **packets[-1], "window_start_ns": 0, "window_end_ns": 50_000_000,
        "contributing_packet_ids": ["packet_001"],
    }
    completion = PacketExecutionCompleted(
        request_id="request_01", device_id="machine_01", bearing_id="bearing_02",
        task_id="task_001", packet_id="packet_001", sender_id="sender_02",
        sequence_number=1, status="SUCCEEDED", error_code=None,
        started_at_ns=1, finished_at_ns=2,
        edge=EdgeResult("normal", 0.95, "low", "edge_model_v1"),
    )

    bridge.route(raw, completion, diagnosis_window=window)

    assert captured["decision_round_id"] == window.decision_round_id
    assert captured["diagnosis_window_id"] == window.diagnosis_window_id
    assert captured["window_start_sequence"] == 1
    assert captured["window_end_sequence"] == 1
