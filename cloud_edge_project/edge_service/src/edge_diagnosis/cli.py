"""Shared one-packet command-line adapter for the integration-only runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from edge_model.contracts import PacketInferenceTask

from .random_forest_model import RandomForestDiagnosticModel


def diagnose_packet(
    perception: dict[str, Any],
    model_path: Path | str,
    metadata_path: Path | str,
) -> dict[str, Any]:
    task = PacketInferenceTask(
        request_id="cli:%s" % perception.get("packet_id", "unknown"),
        device_id=perception.get("device_id"),
        bearing_id=perception.get("bearing_id"),
        task_id=perception.get("task_id"),
        packet_id=perception.get("packet_id"),
        sender_id=perception.get("sender_id"),
        sequence_number=perception.get("sequence_number"),
        perception=perception,
    )
    runner = RandomForestDiagnosticModel(model_path, metadata_path)
    edge = runner.run(task)
    diagnosis = runner.last_diagnosis(task.request_id)
    if diagnosis is None:
        raise RuntimeError("random_forest: diagnosis record missing")
    return {
        "device_id": task.device_id,
        "bearing_id": task.bearing_id,
        "task_id": task.task_id,
        "packet_id": task.packet_id,
        "sender_id": task.sender_id,
        "sequence_number": task.sequence_number,
        **edge.as_dict(),
        "diagnosis_label": diagnosis["diagnosis_label"],
        "diagnosis_mode": diagnosis["diagnosis_mode"],
        "window_ms": diagnosis["window_ms"],
        "deployment_status": diagnosis["deployment_status"],
        "review_required": diagnosis["review_required"],
        "review_reasons": diagnosis["review_reasons"],
    }
