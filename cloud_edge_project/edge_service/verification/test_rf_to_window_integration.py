from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import joblib
import pandas as pd
from sklearn.dummy import DummyClassifier

from core.bearing_workflow_contracts import FINAL_EDGE, FinalPacketResult
from edge_aggregation import BearingWindowAggregator
from edge_diagnosis import RandomForestDiagnosticModel
from edge_diagnosis.random_forest_model import FEATURE_COLUMNS
from edge_model.contracts import PacketInferenceTask
from model_input_contract import model_input_probe


def _runner(tmp_path: Path) -> RandomForestDiagnosticModel:
    labels = ["healthy", "outer_ring_damage", "inner_ring_damage"]
    frame = pd.DataFrame([[0.0] * len(FEATURE_COLUMNS)] * 3, columns=FEATURE_COLUMNS)
    estimator = DummyClassifier(strategy="constant", constant="healthy").fit(frame, labels)
    model_path = tmp_path / "model.joblib"
    joblib.dump(
        {
            "feature_columns": list(FEATURE_COLUMNS),
            "labels": labels,
            "estimator": estimator,
        },
        model_path,
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "model_version": "bearing-rf-50ms-integration-only-v1",
                "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
                "feature_columns": list(FEATURE_COLUMNS),
                "labels": labels,
                "qualified_for_deployment": False,
                "allowed_use": "pipeline_integration_only",
                "locked_test_consumed": False,
            }
        ),
        encoding="utf-8",
    )
    return RandomForestDiagnosticModel(model_path, metadata_path)


def test_eighty_rf_packet_results_form_existing_four_windows(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    aggregator = BearingWindowAggregator()
    windows = []
    for sequence in range(1, 81):
        perception = deepcopy(model_input_probe())
        perception.update(
            device_id="device-1",
            bearing_id="bearing-1",
            task_id="task-1",
            packet_id=f"packet-{sequence}",
            sender_id="sender-1",
            sequence_number=sequence,
        )
        task = PacketInferenceTask(
            request_id=f"request-{sequence}",
            device_id="device-1",
            bearing_id="bearing-1",
            task_id="task-1",
            packet_id=f"packet-{sequence}",
            sender_id="sender-1",
            sequence_number=sequence,
            perception=perception,
        )
        edge = runner.run(task)
        window = aggregator.add_packet(
            FinalPacketResult(
                result_id=f"result-{sequence}",
                device_id="device-1",
                task_id="task-1",
                bearing_id="bearing-1",
                sender_id="sender-1",
                packet_id=f"packet-{sequence}",
                sequence_number=sequence,
                action_grade=0 if edge.edge_result == "normal" else 4,
                confidence=edge.confidence,
                data_quality_score=1.0,
                risk_level=edge.edge_risk_level,
                decision_source=FINAL_EDGE,
                raw_data_ref=f"edge-cache://sender-1/task-1/{sequence}",
            )
        )
        if window is not None:
            windows.append(window)

    assert [window.window_index for window in windows] == [1, 2, 3, 4]
    assert [(window.sequence_start, window.sequence_end) for window in windows] == [
        (1, 20),
        (21, 40),
        (41, 60),
        (61, 80),
    ]
    assert all(window.packet_count == 20 for window in windows)
    assert all(window.review_required is False for window in windows)
