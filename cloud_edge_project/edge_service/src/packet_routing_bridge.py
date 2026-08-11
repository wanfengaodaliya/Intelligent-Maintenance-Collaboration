"""Adapt the legacy simple edge packet to the current packet-routing contract."""
# 该模块将旧版边缘数据包适配为当前的数据包路由契约。
from __future__ import annotations

from typing import Any, Callable, Mapping

from cloud_review import CloudReviewStore


class PacketRoutingBridge:
    def __init__(self, *, edge_node_id: str, store: CloudReviewStore,
                 post: Callable[[str, Mapping[str, Any]], dict[str, Any]]):
        self.edge_node_id = edge_node_id
        self.store = store
        self.post = post

    def route(self, raw_packet: Mapping[str, Any], simple_result: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(raw_packet)
        identity = self._identity(raw)
        output = self._output(simple_result)
        persisted_result = {**identity, **output}
        self.store.save(raw, persisted_result)
        payload = {
            "device_id": identity["device_id"],
            "task_id": identity["task_id"],
            "bearing_id": identity["bearing_id"],
            "edge_node_id": self.edge_node_id,
            "input_ref": {
                "device_id": identity["device_id"],
                "bearing_id": identity["bearing_id"],
                "sender_id": identity["sender_id"],
                "packet_id": identity["packet_id"],
                "sequence_number": identity["sequence_number"],
            },
            "status": "SUCCEEDED",
            "started_at_ns": int(raw["start_timestamp_ns"]),
            "finished_at_ns": int(raw["end_timestamp_ns"]),
            "error": None,
            "output": output,
        }
        return self.post("/scheduler/packet-route", payload)

    @staticmethod
    def _identity(raw: Mapping[str, Any]) -> dict[str, Any]:
        fields = ("device_id", "task_id", "bearing_id", "sender_id", "packet_id")
        result = {field: raw.get(field) for field in fields}
        if any(not isinstance(value, str) or not value.strip() for value in result.values()):
            raise ValueError("formal edge packet requires complete task identity")
        sequence = raw.get("sequence_number")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("sequence_number must be a positive integer")
        result["sequence_number"] = sequence
        return result

    @staticmethod
    def _output(result: Mapping[str, Any]) -> dict[str, Any]:
        confidence = result.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be within [0, 1]")
        label = result.get("label")
        if label not in {"normal", "abnormal"}:
            raise ValueError("legacy edge label must be normal or abnormal")
        risk = result.get("risk_level")
        if risk not in {"low", "medium", "high"}:
            raise ValueError("invalid risk_level")
        normalized = float(confidence)
        return {
            "edge_result": "normal" if label == "normal" else "fault",
            "confidence": normalized,
            "task_complexity": round(1.0 - normalized, 6),
            "edge_risk_level": risk,
            "model_version": "edge_bearing_mock",
        }
