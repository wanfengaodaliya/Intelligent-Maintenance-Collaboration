"""Persist real packet completion events before requesting scheduler routing."""

from __future__ import annotations

from statistics import fmean, pstdev
from typing import Any, Callable, Mapping

import numpy as np

from cloud_review import CloudReviewStore
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from edge_model.contracts import PacketExecutionCompleted


class PacketRoutingBridge:
    def __init__(
        self,
        *,
        edge_node_id: str,
        store: CloudReviewStore,
        post: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    ) -> None:
        self.edge_node_id = edge_node_id
        self.store = store
        self.post = post

    def route(
        self,
        raw_packet: Mapping[str, Any],
        completion: PacketExecutionCompleted,
        *,
        diagnosis_window: Any | None = None,
    ) -> dict[str, Any]:
        raw = _json_value(dict(raw_packet))
        identity = self._identity(raw, completion)
        start_sequence = (
            diagnosis_window.window_start_sequence
            if diagnosis_window is not None else completion.sequence_number
        )
        end_sequence = (
            diagnosis_window.window_end_sequence
            if diagnosis_window is not None else completion.sequence_number
        )
        window_identity = {
            "decision_round_id": (
                diagnosis_window.decision_round_id
                if diagnosis_window is not None
                else build_decision_round_id(
                    device_id=completion.device_id,
                    task_id=completion.task_id,
                    window_start_sequence=start_sequence,
                    window_end_sequence=end_sequence,
                )
            ),
            "diagnosis_window_id": (
                diagnosis_window.diagnosis_window_id
                if diagnosis_window is not None
                else build_diagnosis_window_id(
                    device_id=completion.device_id,
                    task_id=completion.task_id,
                    bearing_id=completion.bearing_id,
                    sender_id=completion.sender_id,
                    window_start_sequence=start_sequence,
                    window_end_sequence=end_sequence,
                )
            ),
            "window_start_sequence": start_sequence,
            "window_end_sequence": end_sequence,
        }
        payload: dict[str, Any] = {
            "device_id": completion.device_id,
            "task_id": completion.task_id,
            "bearing_id": completion.bearing_id,
            "edge_node_id": self.edge_node_id,
            **window_identity,
            "input_ref": {
                "device_id": completion.device_id,
                "bearing_id": completion.bearing_id,
                "sender_id": completion.sender_id,
                "packet_id": completion.packet_id,
                "sequence_number": completion.sequence_number,
            },
            "status": completion.status,
            "started_at_ns": completion.started_at_ns,
            "finished_at_ns": completion.finished_at_ns,
            "error": completion.error_code,
        }
        persisted = self._edge_perception_result(raw, completion, identity)
        if completion.status == "SUCCEEDED":
            if completion.edge is None:
                raise ValueError("successful packet completion requires edge output")
            confidence = float(completion.edge.confidence)
            output = {
                **completion.edge.as_dict(),
                "task_complexity": round(1.0 - confidence, 6),
            }
            payload["output"] = output
            persisted["edge_inference"] = completion.edge.as_dict()
            persisted["edge_model_version"] = completion.edge.model_version
        elif completion.status not in {"FAILED", "TIMEOUT"}:
            raise ValueError("unsupported packet completion status")
        elif not completion.error_code:
            raise ValueError("failed packet completion requires error_code")

        self.store.save(raw, persisted)
        return self.post("/scheduler/packet-route", payload)

    @staticmethod
    def _edge_perception_result(
        raw: Mapping[str, Any],
        completion: PacketExecutionCompleted,
        identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        perception = completion.perception
        if isinstance(perception, Mapping):
            result = _json_value(dict(perception))
            for field, value in identity.items():
                if result.get(field) != value:
                    raise ValueError(f"perception conflicts with completion {field}")
            result.update(
                {
                    "edge_inference": None,
                    "edge_model_version": None,
                    "execution_status": completion.status,
                    "error_code": completion.error_code,
                }
            )
            return result

        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        end_ns = int(raw.get("end_generate_timestamp_ns") or completion.started_at_ns)
        operating_context = {
            name: _series_statistics(data.get(name))
            for name in (
                "shaft_speed_rpm",
                "load_torque_nm",
                "bearing_radial_load_n",
            )
        }
        temperature = data.get("bearing_module_temperature_c", 0.0)
        operating_context["bearing_module_temperature_c"] = float(temperature)
        quality_good = completion.data_quality_score >= 1.0
        return {
            **identity,
            "end_generate_timestamp_ns": end_ns,
            "feature_generated_at_ns": max(end_ns, completion.finished_at_ns),
            "features": {"operating_context": operating_context},
            "perception_quality": {
                "status": "good" if quality_good else "warning",
                "flags": [] if quality_good else ["EDGE_DATA_QUALITY_DEGRADED"],
            },
            "edge_inference": None,
            "edge_model_version": None,
            "execution_status": completion.status,
            "error_code": completion.error_code,
        }

    @staticmethod
    def _identity(
        raw: Mapping[str, Any], completion: PacketExecutionCompleted
    ) -> dict[str, Any]:
        identity = {
            "device_id": completion.device_id,
            "task_id": completion.task_id,
            "bearing_id": completion.bearing_id,
            "sender_id": completion.sender_id,
            "packet_id": completion.packet_id,
            "sequence_number": completion.sequence_number,
        }
        for field, value in identity.items():
            if raw.get(field) != value:
                raise ValueError(f"raw packet conflicts with completion {field}")
        return identity


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _series_statistics(value: Any) -> dict[str, float]:
    source = value if isinstance(value, Mapping) else {}
    values = source.get("values")
    numbers = [float(item) for item in values] if isinstance(values, list) and values else [0.0]
    return {
        "mean": fmean(numbers),
        "last": numbers[-1],
        "minimum": min(numbers),
        "maximum": max(numbers),
        "standard_deviation": pstdev(numbers),
    }
