"""Regression tests for edge-summary cloud ingestion."""

from __future__ import annotations

import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from common.schemas import ContractError, validate_edge_feature_summary_batch
from cloud_service.storage.database import connect, initialize_database
from cloud_service.storage.edge_feature_repository import EdgeFeatureRepository
from cloud_service.app import app, edge_feature_summaries
from fastapi.testclient import TestClient


def completed_summary() -> dict:
    return {
        "summary_id": "sender_01:task_01:packet_01",
        "task_id": "task_01",
        "packet_id": "packet_01",
        "sender_id": "sender_01",
        "sequence_number": 1,
        "edge_node_id": "edge_01",
        "end_timestamp_ns": 100,
        "summary_generated_at_ns": 101,
        "processing_status": "perception_completed",
        "perception_quality": {"status": "good", "flags": []},
        "features": {
            "vibration": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "unit": "mm/s",
                "rms": 0.34,
                "absolute_peak": 1.82,
                "kurtosis": 3.21,
                "dominant_frequency_hz": 245.1,
                "band_power_ratio_500_2000": 0.31,
                "spectral_entropy": 0.64,
            },
            "phase_current_1": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "unit": "A",
                "rms_a": 2.35,
                "absolute_peak_a": 3.42,
            },
            "phase_current_2": {
                "source_sample_rate_hz": 64000,
                "analysis_sample_rate_hz": 16000,
                "unit": "A",
                "rms_a": 2.29,
                "absolute_peak_a": 3.38,
            },
            "current_relationship": {"current_imbalance_ratio": 0.026},
            "operating_context": {
                "shaft_speed_rpm": statistics(899.7),
                "load_torque_nm": statistics(0.70),
                "bearing_radial_load_n": statistics(1000.0),
                "bearing_module_temperature_c": 46.3,
            },
        },
        "edge_inference": {
            "edge_result": "warning",
            "confidence": 0.87,
            "edge_risk_level": "medium",
        },
        "edge_model_version": "edge_model_v1",
    }


def statistics(value: float) -> dict:
    return {
        "mean": value,
        "last": value,
        "minimum": value,
        "maximum": value,
        "standard_deviation": 0.0,
    }


def batch(summary: dict) -> dict:
    return {
        "batch_id": "upload_01",
        "edge_node_id": "edge_01",
        "sent_at_ns": 102,
        "item_count": 1,
        "summaries": [summary],
    }


class EdgeSummaryValidationTests(unittest.TestCase):
    def test_accepts_completed_summary_with_documented_features(self) -> None:
        payload = batch(completed_summary())

        self.assertEqual(validate_edge_feature_summary_batch(payload), payload)

    def test_rejects_completed_summary_with_out_of_range_confidence(self) -> None:
        payload = batch(completed_summary())
        payload["summaries"][0]["edge_inference"]["confidence"] = 1.1

        with self.assertRaisesRegex(ContractError, "confidence") as context:
            validate_edge_feature_summary_batch(payload)

        self.assertEqual(context.exception.code, "INVALID_EDGE_INFERENCE")

    def test_accepts_rejected_summary_without_features(self) -> None:
        summary = completed_summary()
        for field in ("features", "edge_inference", "edge_model_version", "perception_quality", "summary_generated_at_ns"):
            summary.pop(field)
        summary["processing_status"] = "perception_rejected"
        summary["perception_error_codes"] = ["INVALID_SAMPLE_COUNT"]

        self.assertEqual(validate_edge_feature_summary_batch(batch(summary)), batch(summary))


class EdgeSummaryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "cloud.db"
        initialize_database(self.database_path)
        self.repository = EdgeFeatureRepository(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_marks_an_identical_retry_as_duplicate(self) -> None:
        summary = completed_summary()

        self.assertEqual(self.repository.ingest_summary(summary), ("accepted", None))
        self.assertEqual(self.repository.ingest_summary(summary), ("duplicate", None))

    def test_records_different_content_for_same_packet_as_conflict(self) -> None:
        self.repository.ingest_summary(completed_summary())
        conflicting_summary = completed_summary()
        conflicting_summary["edge_inference"]["confidence"] = 0.86

        self.assertEqual(
            self.repository.ingest_summary(conflicting_summary),
            ("conflict", "PACKET_CONTENT_CONFLICT"),
        )
        with connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM summary_ingestion_conflicts").fetchone()[0], 1
            )

    def test_accepts_rejected_summary_without_feature_columns(self) -> None:
        summary = completed_summary()
        for field in ("features", "edge_inference", "edge_model_version", "perception_quality", "summary_generated_at_ns"):
            summary.pop(field)
        summary["processing_status"] = "perception_rejected"
        summary["perception_error_codes"] = ["INVALID_SAMPLE_COUNT"]

        self.assertEqual(self.repository.ingest_summary(summary), ("accepted", None))
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT processing_status, vibration_rms, perception_error_codes_json "
                "FROM edge_packet_summary WHERE sender_id='sender_01' AND packet_id='packet_01'"
            ).fetchone()
        self.assertEqual(row["processing_status"], "perception_rejected")
        self.assertIsNone(row["vibration_rms"])
        self.assertEqual(row["perception_error_codes_json"], '["INVALID_SAMPLE_COUNT"]')


class EdgeSummaryHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "cloud.db"
        os.environ["CLOUD_SUMMARY_DATABASE_PATH"] = str(self.database_path)

    def tearDown(self) -> None:
        os.environ.pop("CLOUD_SUMMARY_DATABASE_PATH", None)
        self.temporary_directory.cleanup()

    def test_route_returns_per_item_accept_reject_and_duplicate_results(self) -> None:
        accepted = completed_summary()
        rejected = completed_summary()
        rejected["packet_id"] = "packet_02"
        rejected["summary_id"] = "sender_01:task_01:packet_02"
        rejected["sequence_number"] = 2
        rejected["edge_inference"]["confidence"] = 1.1
        payload = batch(accepted)
        payload["item_count"] = 2
        payload["summaries"].append(rejected)

        response = edge_feature_summaries(payload)
        retry_response = edge_feature_summaries(batch(accepted))

        self.assertTrue(any(route.path == "/cloud/edge-feature-summaries" for route in app.routes))
        self.assertEqual(response["results"], [
            {"summary_id": "sender_01:task_01:packet_01", "status": "accepted"},
            {"summary_id": "sender_01:task_01:packet_02", "status": "rejected", "error_code": "INVALID_EDGE_INFERENCE"},
        ])
        self.assertEqual(retry_response["results"], [
            {"summary_id": "sender_01:task_01:packet_01", "status": "duplicate"},
        ])

    def test_route_returns_http_400_for_invalid_batch_envelope(self) -> None:
        payload = batch(completed_summary())
        payload["item_count"] = 2

        response = edge_feature_summaries(payload)

        self.assertEqual(response.status_code, 400)

    def test_http_post_binds_json_body_and_returns_confirmation(self) -> None:
        client = TestClient(app)

        response = client.post("/cloud/edge-feature-summaries", json=batch(completed_summary()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "batch_id": "upload_01",
            "results": [{"summary_id": "sender_01:task_01:packet_01", "status": "accepted"}],
        })
