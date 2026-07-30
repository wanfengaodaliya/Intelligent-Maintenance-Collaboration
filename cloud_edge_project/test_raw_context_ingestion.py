"""Tests for cloud raw-context request and ingestion."""

from __future__ import annotations

import tempfile
import unittest
import copy
import gzip
import hashlib
import json
import os
import sqlite3
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

import requests
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from cloud_service.app import (
    _raw_context_transport,
    app,
    raw_context_batches,
)
from cloud_service.config import CloudSettings
from cloud_service.perception import pipeline as perception_pipeline
from cloud_service.raw_context.coordinator import RawContextCoordinator
from cloud_service.raw_context.receiver import RawContextReceiver, _context_status
from cloud_service.raw_context.transport import HttpRawContextTransport
from cloud_service.service import infer_cloud as infer_cloud_service
from cloud_service.storage import CloudReviewRepository, connect, initialize_database
from cloud_service.storage.raw_context_repository import RawContextRequestRepository
from cloud_service.storage.raw_packet_repository import RawPacketRepository
from common.schemas import ContractError


def create_review(database_path: Path, *, anchor_sequence_number: int = 101) -> str:
    initialize_database(database_path)
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO senders(sender_id,created_at_ns,updated_at_ns) VALUES (?,?,?)",
            ("sender_01", 1, 1),
        )
        connection.execute(
            "INSERT INTO edge_packet_summary("
            "sender_id,packet_id,task_id,sequence_number,edge_node_id,end_timestamp_ns,"
            "received_at_ns,processing_status,summary_json,payload_sha256"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "sender_01", "batch_000101", "task_00001", anchor_sequence_number,
                "edge_01", 5_050_000_000, 6_000_000_000,
                "perception_completed", "{}", "anchor-digest",
            ),
        )
        connection.execute(
            "INSERT INTO raw_packet_index("
            "sender_id,packet_id,task_id,sequence_number,start_timestamp_ns,"
            "end_generate_timestamp_ns,sample_rate_hz,sample_count,storage_path,"
            "payload_sha256,compressed_size_bytes,validation_status,received_at_ns"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sender_01", "batch_000101", "task_00001",
                anchor_sequence_number, 5_000_000_000, 5_050_000_000,
                64_000, 3_200, "anchor.json.gz", "anchor-raw-digest",
                1, "valid", 6_000_000_000,
            ),
        )
    return CloudReviewRepository(database_path).upsert_preliminary(
        sender_id="sender_01",
        anchor_packet_id="batch_000101",
        task_id="task_00001",
        feature_extractor_version="cloud_high_rate_feature_v1",
        schema_version="cloud_perception_result/2.0",
        data_quality_valid=True,
        data_quality={"valid": True},
        start_timestamp_ns=5_000_000_000,
        end_timestamp_ns=5_050_000_000,
    )


def create_context_request(
    database_path: Path,
    review_id: str,
    *,
    deadline_at_ns: int | None = None,
) -> dict:
    deadline = (
        time.time_ns() + 10_000_000_000
        if deadline_at_ns is None
        else deadline_at_ns
    )
    return RawContextRequestRepository(database_path).create_or_get(
        request_id="ctx_req_001",
        review_id=review_id,
        task_id="task_00001",
        sender_id="sender_01",
        anchor_packet_id="batch_000101",
        anchor_sequence_number=101,
        before_packet_count=20,
        after_packet_count=0,
        minimum_context_packet_count=16,
        requested_at_ns=1_000_000_000,
        deadline_at_ns=deadline,
    )


def raw_packet(sequence_number: int, *, packet_id: str | None = None) -> dict:
    return {
        "task_id": "task_00001",
        "packet_id": packet_id or f"batch_{sequence_number:06d}",
        "sender_id": "sender_01",
        "sequence_number": sequence_number,
        "end_generate_timestamp_ns": sequence_number * 50_000_000,
        "data": {
            "vibration": {
                "unit": "mm/s", "sample_rate_hz": 64_000,
                "sample_count": 3_200, "values": [0.1] * 3_200,
            },
            "phase_current_1_A": {
                "unit": "A", "sample_rate_hz": 64_000,
                "sample_count": 3_200, "values": [1.0] * 3_200,
            },
            "phase_current_2_A": {
                "unit": "A", "sample_rate_hz": 64_000,
                "sample_count": 3_200, "values": [0.98] * 3_200,
            },
            "shaft_speed_rpm": {
                "sample_rate_hz": 4_000,
                "sample_count": 200, "values": [900.0] * 200,
            },
            "load_torque_nm": {
                "sample_rate_hz": 4_000,
                "sample_count": 200, "values": [12.0] * 200,
            },
            "bearing_radial_load_n": {
                "sample_rate_hz": 4_000,
                "sample_count": 200, "values": [80.0] * 200,
            },
            "bearing_module_temperature_c": 46.3,
        },
    }


def cloud_review_request(sequence_number: int = 101) -> dict:
    raw = raw_packet(
        sequence_number,
        packet_id=f"batch_{sequence_number:06d}",
    )
    operating_context = {}
    for name in (
        "shaft_speed_rpm",
        "load_torque_nm",
        "bearing_radial_load_n",
    ):
        value = raw["data"][name]["values"][0]
        operating_context[name] = {
            "mean": value,
            "last": value,
            "minimum": value,
            "maximum": value,
            "standard_deviation": 0.0,
        }
    operating_context["bearing_module_temperature_c"] = 46.3
    edge = {
        "task_id": raw["task_id"],
        "packet_id": raw["packet_id"],
        "sender_id": raw["sender_id"],
        "sequence_number": raw["sequence_number"],
        "end_generate_timestamp_ns": raw["end_generate_timestamp_ns"],
        "feature_generated_at_ns": raw["end_generate_timestamp_ns"] + 1,
        "edge_node_id": "edge_01",
        "edge_model_version": "edge_model_v1",
        "perception_quality": {"status": "good", "flags": []},
        "features": {
            "vibration": {
                "rms": 0.1,
                "absolute_peak": 0.1,
                "kurtosis": 3.0,
                "dominant_frequency_hz": 1_000.0,
                "band_power_ratio_500_2000": 0.5,
                "spectral_entropy": 0.4,
            },
            "phase_current_1": {
                "rms_a": 1.0,
                "absolute_peak_a": 1.0,
            },
            "phase_current_2": {
                "rms_a": 0.98,
                "absolute_peak_a": 0.98,
            },
            "current_relationship": {
                "current_imbalance_ratio": 0.02,
            },
            "operating_context": operating_context,
        },
        "edge_inference": {
            "edge_result": "abnormal",
            "confidence": 0.9,
            "edge_risk_level": "high",
        },
    }
    return {
        "cloud_raw_packet": raw,
        "edge_perception_result": edge,
    }


def context_batch(
    sequences: list[int],
    *,
    position: str,
    status: str = "pending_context",
    missing: list[int] | None = None,
) -> dict:
    return {
        "batch_id": f"ctx_req_001:{position}:{sequences[0]}",
        "request_id": "ctx_req_001",
        "task_id": "task_00001",
        "sender_id": "sender_01",
        "anchor_packet_id": "batch_000101",
        "anchor_sequence_number": 101,
        "context_position": position,
        "context_status": status,
        "first_sequence_number": sequences[0],
        "last_sequence_number": sequences[-1],
        "item_count": len(sequences),
        "packets": [raw_packet(sequence) for sequence in sequences],
        "missing_sequence_numbers": missing or [],
        "sent_at_ns": 2_000_000_000,
    }


class RawContextRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "cloud.db"
        self.review_id = create_review(self.database_path)
        self.repository = RawContextRequestRepository(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migrates_v5_context_requests_without_data_or_foreign_key_loss(
        self,
    ) -> None:
        database_path = Path(self.temporary_directory.name) / "legacy-v5.db"
        with closing(sqlite3.connect(database_path)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at_ns INTEGER NOT NULL,
                    description TEXT NOT NULL
                );
                CREATE TABLE edge_packet_summary (
                    sender_id TEXT NOT NULL,
                    packet_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    edge_node_id TEXT NOT NULL,
                    end_timestamp_ns INTEGER NOT NULL,
                    received_at_ns INTEGER NOT NULL,
                    processing_status TEXT NOT NULL,
                    PRIMARY KEY (sender_id, packet_id)
                );
                CREATE TABLE cloud_review (
                    review_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    anchor_packet_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    feature_extractor_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    review_status TEXT NOT NULL CHECK (
                        review_status IN (
                            'preliminary', 'complete',
                            'insufficient_context', 'invalid'
                        )
                    ),
                    context_status TEXT NOT NULL CHECK (
                        context_status IN (
                            'pending_context', 'complete',
                            'insufficient_context', 'not_requested', 'invalid'
                        )
                    ),
                    data_quality_valid INTEGER NOT NULL CHECK (
                        data_quality_valid IN (0, 1)
                    ),
                    start_timestamp_ns INTEGER,
                    end_timestamp_ns INTEGER,
                    packet_count INTEGER NOT NULL DEFAULT 1 CHECK (
                        packet_count > 0
                    ),
                    data_quality_json TEXT NOT NULL,
                    cloud_recomputed_features_json TEXT,
                    cloud_enhanced_features_json TEXT,
                    advanced_features_json TEXT,
                    context_features_json TEXT,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    UNIQUE (
                        sender_id, anchor_packet_id,
                        feature_extractor_version
                    ),
                    FOREIGN KEY (sender_id, anchor_packet_id)
                        REFERENCES edge_packet_summary(sender_id, packet_id)
                );
                CREATE INDEX idx_cloud_review_sender_time
                    ON cloud_review(sender_id, updated_at_ns);
                CREATE TABLE raw_context_request (
                    request_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    anchor_packet_id TEXT NOT NULL,
                    anchor_sequence_number INTEGER NOT NULL CHECK (
                        anchor_sequence_number > 0
                    ),
                    before_packet_count INTEGER NOT NULL CHECK (
                        before_packet_count > 0
                    ),
                    after_packet_count INTEGER NOT NULL CHECK (
                        after_packet_count > 0
                    ),
                    request_status TEXT NOT NULL CHECK (
                        request_status IN (
                            'created', 'dispatched', 'pending_context',
                            'complete', 'insufficient_context',
                            'dispatch_failed'
                        )
                    ),
                    requested_at_ns INTEGER NOT NULL,
                    deadline_at_ns INTEGER NOT NULL,
                    edge_response_json TEXT,
                    last_error_code TEXT,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    FOREIGN KEY (review_id)
                        REFERENCES cloud_review(review_id) ON DELETE CASCADE
                );
                CREATE INDEX idx_raw_context_request_deadline
                    ON raw_context_request(request_status, deadline_at_ns);
                INSERT INTO schema_migrations VALUES (
                    5, 1, 'raw context request and ingestion schema'
                );
                INSERT INTO edge_packet_summary VALUES
                    ('sender_01', 'batch_000101', 'task_00001', 101,
                     'edge_01', 5050000000, 6000000000,
                     'perception_completed'),
                    ('sender_01', 'batch_000201', 'task_00002', 201,
                     'edge_01', 10050000000, 11000000000,
                     'perception_completed');
                INSERT INTO cloud_review (
                    review_id, sender_id, anchor_packet_id, task_id,
                    feature_extractor_version, schema_version,
                    review_status, context_status, data_quality_valid,
                    start_timestamp_ns, end_timestamp_ns, packet_count,
                    data_quality_json, created_at_ns, updated_at_ns
                ) VALUES
                    ('review_v5', 'sender_01', 'batch_000101', 'task_00001',
                     'cloud_high_rate_feature_v1',
                     'cloud_perception_result/2.0',
                     'preliminary', 'pending_context', 1,
                     5000000000, 5050000000, 1, '{}', 1, 1),
                    ('review_v6', 'sender_01', 'batch_000201', 'task_00002',
                     'cloud_high_rate_feature_v1',
                     'cloud_perception_result/2.0',
                     'preliminary', 'not_requested', 1,
                     10000000000, 10050000000, 1, '{}', 1, 1);
                INSERT INTO raw_context_request (
                    request_id, review_id, task_id, sender_id,
                    anchor_packet_id, anchor_sequence_number,
                    before_packet_count, after_packet_count, request_status,
                    requested_at_ns, deadline_at_ns, created_at_ns,
                    updated_at_ns
                ) VALUES (
                    'ctx_req_v5', 'review_v5', 'task_00001', 'sender_01',
                    'batch_000101', 101, 10, 10, 'pending_context',
                    1000, 3000001000, 1000, 1000
                );
                """
            )

        initialize_database(database_path)
        migrated = RawContextRequestRepository(database_path).get(
            "ctx_req_v5"
        )
        created = RawContextRequestRepository(database_path).create_or_get(
            request_id="ctx_req_v6",
            review_id="review_v6",
            task_id="task_00002",
            sender_id="sender_01",
            anchor_packet_id="batch_000201",
            anchor_sequence_number=201,
            before_packet_count=20,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=2_000,
            deadline_at_ns=3_000_002_000,
        )
        initialize_database(database_path)

        self.assertEqual(migrated["minimum_context_packet_count"], 16)
        self.assertEqual(created["after_packet_count"], 0)
        self.assertEqual(created["minimum_context_packet_count"], 16)
        with connect(database_path) as connection:
            connection.execute(
                "UPDATE raw_context_request "
                "SET request_status='partial_context' "
                "WHERE request_id='ctx_req_v6'"
            )
            connection.execute(
                "UPDATE cloud_review SET context_status='partial_context' "
                "WHERE review_id='review_v6'"
            )
            reviews = connection.execute(
                "SELECT review_id FROM cloud_review ORDER BY review_id"
            ).fetchall()
            requests = connection.execute(
                "SELECT request_id FROM raw_context_request "
                "ORDER BY request_id"
            ).fetchall()
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        self.assertEqual(
            [row["review_id"] for row in reviews],
            ["review_v5", "review_v6"],
        )
        self.assertEqual(
            [row["request_id"] for row in requests],
            ["ctx_req_v5", "ctx_req_v6"],
        )
        self.assertEqual(foreign_key_violations, [])

    def test_create_or_get_reuses_one_request_per_review(self) -> None:
        first = self.repository.create_or_get(
            request_id="ctx_req_001",
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
            before_packet_count=20,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=10_000,
            deadline_at_ns=3_000_010_000,
        )
        retried = self.repository.create_or_get(
            request_id="ctx_req_other",
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
            before_packet_count=20,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=20_000,
            deadline_at_ns=3_000_020_000,
        )

        self.assertEqual(first["request_id"], "ctx_req_001")
        self.assertEqual(retried["request_id"], "ctx_req_001")
        self.assertEqual(retried["request_status"], "created")
        self.assertEqual(retried["minimum_context_packet_count"], 16)
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(review["review_status"], "preliminary")
        self.assertEqual(review["context_status"], "pending_context")

    def test_concurrent_creation_reuses_one_request_without_error(self) -> None:
        barrier = Barrier(2)

        def create(request_id: str) -> dict:
            barrier.wait()
            return RawContextRequestRepository(
                self.database_path
            ).create_or_get(
                request_id=request_id,
                review_id=self.review_id,
                task_id="task_00001",
                sender_id="sender_01",
                anchor_packet_id="batch_000101",
                anchor_sequence_number=101,
                before_packet_count=20,
                after_packet_count=0,
                minimum_context_packet_count=16,
                requested_at_ns=10_000,
                deadline_at_ns=3_000_010_000,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            created = list(
                executor.map(create, ("ctx_req_a", "ctx_req_b"))
            )

        self.assertEqual(
            {item["request_id"] for item in created},
            {created[0]["request_id"]},
        )
        with connect(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM raw_context_request "
                "WHERE review_id=?",
                (self.review_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_expire_due_marks_request_and_review_insufficient(self) -> None:
        self.repository.create_or_get(
            request_id="ctx_req_001",
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
            before_packet_count=20,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=10_000,
            deadline_at_ns=20_000,
        )

        expired = self.repository.expire_due(now_ns=20_001)

        request = self.repository.get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(expired, ["ctx_req_001"])
        self.assertEqual(request["request_status"], "insufficient_context")
        self.assertEqual(request["last_error_code"], "CONTEXT_DEADLINE_EXCEEDED")
        self.assertEqual(review["context_status"], "insufficient_context")
        self.assertEqual(review["review_status"], "insufficient_context")

    def test_expire_due_skips_request_with_active_processing_lease(
        self,
    ) -> None:
        self.repository.create_or_get(
            request_id="ctx_req_001",
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
            before_packet_count=20,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=10_000,
            deadline_at_ns=20_000,
        )
        acquired = self.repository.acquire_processing_lease(
            "ctx_req_001",
            lease_until_ns=30_000,
            updated_at_ns=15_000,
        )

        expired = self.repository.expire_due(now_ns=20_001)

        request = self.repository.get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertTrue(acquired)
        self.assertEqual(expired, [])
        self.assertEqual(request["request_status"], "created")
        self.assertEqual(review["context_status"], "pending_context")
        self.assertEqual(review["review_status"], "preliminary")

    def test_expire_due_marks_contiguous_minimum_before_context_partial(
        self,
    ) -> None:
        create_context_request(
            self.database_path,
            self.review_id,
            deadline_at_ns=2_000_000_000,
        )
        receiver = RawContextReceiver(
            self.database_path,
            clock_ns=lambda: 1_500_000_000,
        )
        receiver.receive_batch(
            context_batch(list(range(85, 95)), position="before")
        )
        receiver.receive_batch(
            context_batch(list(range(95, 101)), position="before")
        )

        self.repository.expire_due(now_ns=2_000_000_001)

        request = self.repository.get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(request["request_status"], "partial_context")
        self.assertEqual(review["context_status"], "partial_context")
        self.assertEqual(review["review_status"], "preliminary")

    def test_expire_due_recovers_full_pending_context_as_complete(
        self,
    ) -> None:
        create_context_request(
            self.database_path,
            self.review_id,
            deadline_at_ns=2_000_000_000,
        )
        raw_packets = RawPacketRepository(self.database_path)
        for sequence_number in range(81, 101):
            raw_packets.ingest_context(
                raw_packet(sequence_number),
                review_id=self.review_id,
                relative_position=sequence_number - 101,
                role="before",
            )
        self.repository.update_dispatch(
            "ctx_req_001",
            request_status="pending_context",
            last_error_code="EDGE_INSUFFICIENT_CONTEXT",
            updated_at_ns=1_500_000_000,
        )

        expired = self.repository.expire_due(now_ns=2_000_000_001)

        request = self.repository.get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(expired, ["ctx_req_001"])
        self.assertEqual(request["request_status"], "complete")
        self.assertIsNone(request["last_error_code"])
        self.assertEqual(review["context_status"], "complete")
        self.assertEqual(review["review_status"], "preliminary")

    def test_expire_due_recovers_stale_full_processing_lease_as_complete(
        self,
    ) -> None:
        create_context_request(
            self.database_path,
            self.review_id,
            deadline_at_ns=2_000_000_000,
        )
        raw_packets = RawPacketRepository(self.database_path)
        for sequence_number in range(81, 101):
            raw_packets.ingest_context(
                raw_packet(sequence_number),
                review_id=self.review_id,
                relative_position=sequence_number - 101,
                role="before",
            )
        acquired = self.repository.acquire_processing_lease(
            "ctx_req_001",
            lease_until_ns=2_000_000_000,
            updated_at_ns=1_500_000_000,
        )

        expired = self.repository.expire_due(now_ns=2_000_000_001)

        request = self.repository.get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertTrue(acquired)
        self.assertEqual(expired, ["ctx_req_001"])
        self.assertEqual(request["request_status"], "complete")
        self.assertIsNone(request["last_error_code"])
        self.assertEqual(review["context_status"], "complete")
        self.assertEqual(review["review_status"], "preliminary")

    def test_expire_due_rejects_noncontiguous_minimum_before_context(
        self,
    ) -> None:
        create_context_request(
            self.database_path,
            self.review_id,
            deadline_at_ns=2_000_000_000,
        )
        receiver = RawContextReceiver(
            self.database_path,
            clock_ns=lambda: 1_500_000_000,
        )
        receiver.receive_batch(
            context_batch(list(range(84, 94)), position="before")
        )
        receiver.receive_batch(
            context_batch(
                list(range(94, 98)),
                position="before",
            )
        )
        receiver.receive_batch(
            context_batch([99, 100], position="before")
        )

        self.repository.expire_due(now_ns=2_000_000_001)

        request = self.repository.get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(request["request_status"], "insufficient_context")
        self.assertEqual(review["context_status"], "insufficient_context")
        self.assertEqual(review["review_status"], "insufficient_context")


class FakeTransport:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests: list[dict] = []

    def send(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


class EchoTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def send(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return {
            "request_id": request["request_id"],
            "anchor_packet_id": request["anchor_packet_id"],
            "status": "pending_context",
            "before_context": {
                "expected_count": 20,
                "available_count": 20,
                "upload_status": "queued",
                "missing_sequence_numbers": [],
            },
            "after_context": {
                "expected_count": 0,
                "available_count": 0,
                "upload_status": "waiting_until_complete",
                "missing_sequence_numbers": [],
            },
        }


class RawContextCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "cloud.db"
        self.review_id = create_review(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_dispatches_exact_request_and_persists_pending_edge_response(self) -> None:
        transport = FakeTransport({
            "request_id": "ctx_req_001",
            "anchor_packet_id": "batch_000101",
            "status": "pending_context",
            "before_context": {
                "expected_count": 20, "available_count": 20,
                "upload_status": "queued", "missing_sequence_numbers": [],
            },
            "after_context": {
                "expected_count": 0, "available_count": 0,
                "upload_status": "waiting_until_complete",
                "missing_sequence_numbers": [],
            },
        })
        coordinator = RawContextCoordinator(
            self.database_path,
            transport=transport,
            clock_ns=lambda: 1_000_000_000,
            request_id_factory=lambda: "ctx_req_001",
        )

        result = coordinator.create_and_dispatch(
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
        )

        self.assertEqual(transport.requests, [{
            "request_id": "ctx_req_001",
            "task_id": "task_00001",
            "sender_id": "sender_01",
            "anchor_packet_id": "batch_000101",
            "anchor_sequence_number": 101,
            "before_packet_count": 20,
            "after_packet_count": 0,
            "requested_at_ns": 1_000_000_000,
            "deadline_at_ns": 4_000_000_000,
        }])
        self.assertEqual(result["request_status"], "pending_context")
        stored = RawContextRequestRepository(self.database_path).get("ctx_req_001")
        self.assertEqual(stored["request_status"], "pending_context")
        self.assertEqual(stored["minimum_context_packet_count"], 16)
        self.assertIn('"status":"pending_context"', stored["edge_response_json"])

    def test_transport_failure_is_persisted_and_retry_reuses_request_id(self) -> None:
        failing = FakeTransport(error=TimeoutError("edge timeout"))
        coordinator = RawContextCoordinator(
            self.database_path,
            transport=failing,
            clock_ns=lambda: 1_000_000_000,
            request_id_factory=lambda: "ctx_req_001",
        )

        failed = coordinator.create_and_dispatch(
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
        )

        self.assertEqual(failed["request_status"], "dispatch_failed")
        self.assertEqual(failed["last_error_code"], "EDGE_UNAVAILABLE")

        succeeding = FakeTransport({
            "request_id": "ctx_req_001",
            "anchor_packet_id": "batch_000101",
            "status": "complete",
            "before_context": {
                "expected_count": 20, "available_count": 20,
                "upload_status": "queued", "missing_sequence_numbers": [],
            },
            "after_context": {
                "expected_count": 0, "available_count": 0,
                "upload_status": "queued", "missing_sequence_numbers": [],
            },
        })
        retried = RawContextCoordinator(
            self.database_path,
            transport=succeeding,
            clock_ns=lambda: 2_000_000_000,
            request_id_factory=lambda: "ctx_req_should_not_replace",
        ).create_and_dispatch(
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
        )

        self.assertEqual(succeeding.requests[0]["request_id"], "ctx_req_001")
        self.assertEqual(retried["request_id"], "ctx_req_001")
        self.assertEqual(retried["request_status"], "pending_context")

    def test_edge_insufficient_response_keeps_request_pending_until_deadline(
        self,
    ) -> None:
        transport = FakeTransport({
            "request_id": "ctx_req_001",
            "anchor_packet_id": "batch_000101",
            "status": "insufficient_context",
            "before_context": {
                "expected_count": 20, "available_count": 15,
                "upload_status": "unavailable", "missing_sequence_numbers": [85],
            },
            "after_context": {
                "expected_count": 0, "available_count": 0,
                "upload_status": "waiting_until_complete",
                "missing_sequence_numbers": [],
            },
        })
        coordinator = RawContextCoordinator(
            self.database_path,
            transport=transport,
            clock_ns=lambda: 1_000_000_000,
            request_id_factory=lambda: "ctx_req_001",
        )

        result = coordinator.create_and_dispatch(
            review_id=self.review_id,
            task_id="task_00001",
            sender_id="sender_01",
            anchor_packet_id="batch_000101",
            anchor_sequence_number=101,
        )

        self.assertEqual(result["request_status"], "pending_context")
        self.assertEqual(result["last_error_code"], "EDGE_INSUFFICIENT_CONTEXT")
        self.assertIn('"status":"insufficient_context"', result["edge_response_json"])


class RawContextProductionIntegrationTests(unittest.TestCase):
    def test_preliminary_perception_does_not_enter_packet_buffer(self) -> None:
        request = cloud_review_request()

        with patch.object(
            perception_pipeline._BUFFER,
            "add",
        ) as aggregate:
            result = perception_pipeline.run_preliminary_perception(
                request
            )

        aggregate.assert_not_called()
        self.assertTrue(result["data_quality"]["valid"])
        self.assertEqual(
            result["data_quality"]["context_status"],
            "pending_context",
        )
        self.assertEqual(result["analysis_window"]["packet_count"], 1)

    def test_context_enabled_inference_stops_before_model_and_aggregation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "cloud.db"
            settings = CloudSettings(
                backend="mock",
                vllm_url="http://unused",
                vllm_model_name="unused",
                vllm_api_key="",
                vllm_timeout_seconds=1,
                database_path=database_path,
            )
            transport = EchoTransport()
            request = cloud_review_request()

            with patch("cloud_service.service.infer_mock") as model:
                result = infer_cloud_service(
                    request,
                    settings=settings,
                    context_transport=transport,
                )

            model.assert_not_called()
            self.assertIsNone(result["review_result"])
            review_id = result["review_id"]
            self.assertIsNotNone(review_id)
            self.assertEqual(
                result["raw_context_request"]["request_status"],
                "pending_context",
            )
            stored = RawContextRequestRepository(database_path).get(
                transport.requests[0]["request_id"]
            )
            self.assertEqual(stored["review_id"], review_id)
            review = CloudReviewRepository(database_path).get(review_id)
            self.assertEqual(review["review_status"], "preliminary")
            self.assertEqual(review["context_status"], "pending_context")


class RawContextTransportTests(unittest.TestCase):
    def test_production_transport_defaults_to_configured_edge_service(
        self,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            transport = _raw_context_transport()

        self.assertEqual(
            transport.url,
            "http://127.0.0.1:8001/edge/raw-context-requests",
        )

    def test_http_transport_posts_to_edge_context_route_with_timeout(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "pending_context"}
        transport = HttpRawContextTransport(
            "http://edge.test/",
            timeout_seconds=2.5,
        )

        with patch(
            "cloud_service.raw_context.transport.requests.post",
            return_value=response,
        ) as post:
            result = transport.send({"request_id": "ctx_req_001"})

        self.assertEqual(result, {"status": "pending_context"})
        post.assert_called_once_with(
            "http://edge.test/edge/raw-context-requests",
            json={"request_id": "ctx_req_001"},
            timeout=2.5,
        )

    def test_http_transport_maps_non_2xx_to_edge_rejected_code(self) -> None:
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("400")
        transport = HttpRawContextTransport("http://edge.test")

        with (
            patch(
                "cloud_service.raw_context.transport.requests.post",
                return_value=response,
            ),
            self.assertRaises(ContractError) as captured,
        ):
            transport.send({"request_id": "ctx_req_001"})

        self.assertEqual(
            captured.exception.code,
            "EDGE_REJECTED_CONTEXT_REQUEST",
        )


class RawContextReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "cloud.db"
        self.review_id = create_review(self.database_path)
        create_context_request(self.database_path, self.review_id)
        self.receiver = RawContextReceiver(
            self.database_path,
            clock_ns=lambda: 2_000_000_000,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edge_acknowledgement_hides_cloud_internal_fields(self) -> None:
        result = self.receiver.receive_batch(context_batch([100], position="before"))

        self.assertNotIn("review_id", result)
        self.assertNotIn("context_ready", result)
        self.assertEqual(result["context_status"], "pending_context")

    def test_accepts_valid_packet_and_persists_raw_index_and_review_link(self) -> None:
        result = self.receiver.receive_batch(
            context_batch([100], position="before")
        )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["context_status"], "pending_context")
        self.assertEqual(result["results"], [{
            "packet_id": "batch_000100",
            "sequence_number": 100,
            "status": "accepted",
        }])
        with connect(self.database_path) as connection:
            raw = connection.execute(
                "SELECT sequence_number,payload_sha256 FROM raw_packet_index "
                "WHERE sender_id='sender_01' AND packet_id='batch_000100'"
            ).fetchone()
            linked = connection.execute(
                "SELECT relative_position,role FROM review_context_packets "
                "WHERE review_id=?",
                (self.review_id,),
            ).fetchone()
        self.assertEqual(raw["sequence_number"], 100)
        self.assertTrue(raw["payload_sha256"])
        self.assertEqual(dict(linked), {
            "relative_position": -1,
            "role": "before",
        })

    def test_receiving_batch_preserves_initial_edge_response_for_audit(self) -> None:
        repository = RawContextRequestRepository(self.database_path)
        repository.update_dispatch(
            "ctx_req_001",
            request_status="pending_context",
            edge_response={"status": "pending_context"},
            updated_at_ns=1_500_000_000,
        )

        self.receiver.receive_batch(context_batch([100], position="before"))

        stored = repository.get("ctx_req_001")
        self.assertEqual(
            json.loads(stored["edge_response_json"]),
            {"status": "pending_context"},
        )

    def test_identical_retry_is_duplicate_without_duplicate_link(self) -> None:
        payload = context_batch([100], position="before")
        self.receiver.receive_batch(payload)

        retried = self.receiver.receive_batch(payload)

        self.assertEqual(retried["results"][0]["status"], "duplicate")
        with connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM review_context_packets"
                ).fetchone()[0],
                1,
            )

    def test_mixed_batch_rejects_only_invalid_packet(self) -> None:
        payload = context_batch([99, 100], position="before")
        payload["packets"][1]["data"]["vibration"]["sample_count"] = 3_199

        result = self.receiver.receive_batch(payload)

        self.assertEqual(result["results"][0]["status"], "accepted")
        self.assertEqual(result["results"][1], {
            "packet_id": "batch_000100",
            "sequence_number": 100,
            "status": "rejected",
            "error_code": "INVALID_SAMPLE_CONFIG",
        })

    def test_expiry_cannot_finalize_request_during_active_batch(
        self,
    ) -> None:
        second_database = Path(self.temporary_directory.name) / "lease.db"
        review_id = create_review(second_database)
        create_context_request(
            second_database,
            review_id,
            deadline_at_ns=2_000_000_000,
        )
        receiver = RawContextReceiver(
            second_database,
            clock_ns=lambda: 1_999_999_999,
        )
        receiver.receive_batch(
            context_batch(list(range(81, 91)), position="before")
        )
        original_ingest = receiver.raw_packets.ingest_context
        expired: list[str] = []
        call_count = 0

        def ingest_with_expiry(*args: object, **kwargs: object):
            nonlocal call_count
            result = original_ingest(*args, **kwargs)
            call_count += 1
            if call_count == 1:
                expired.extend(
                    RawContextRequestRepository(
                        second_database
                    ).expire_due(now_ns=2_000_000_001)
                )
            return result

        with patch.object(
            receiver.raw_packets,
            "ingest_context",
            side_effect=ingest_with_expiry,
        ):
            result = receiver.receive_batch(
                context_batch(list(range(91, 101)), position="before")
            )

        request = RawContextRequestRepository(second_database).get(
            "ctx_req_001"
        )
        self.assertEqual(expired, [])
        self.assertEqual(result["context_status"], "complete")
        self.assertEqual(request["request_status"], "complete")
        self.assertIsNone(request["last_error_code"])

    def test_terminal_race_before_lease_stops_without_writing_packet(
        self,
    ) -> None:
        second_database = Path(self.temporary_directory.name) / "race.db"
        review_id = create_review(second_database)
        create_context_request(
            second_database,
            review_id,
            deadline_at_ns=2_000_000_000,
        )
        receiver = RawContextReceiver(
            second_database,
            clock_ns=lambda: 1_999_999_999,
        )
        expired: list[str] = []

        def expire_before_acquire(*args: object, **kwargs: object) -> bool:
            expired.extend(
                RawContextRequestRepository(
                    second_database
                ).expire_due(now_ns=2_000_000_001)
            )
            return False

        captured: ContractError | None = None
        with patch.object(
            receiver.requests,
            "acquire_processing_lease",
            side_effect=expire_before_acquire,
        ):
            try:
                receiver.receive_batch(
                    context_batch([100], position="before")
                )
            except ContractError as error:
                captured = error

        with connect(second_database) as connection:
            raw_count = connection.execute(
                "SELECT COUNT(*) FROM raw_packet_index "
                "WHERE packet_id='batch_000100'"
            ).fetchone()[0]
            link_count = connection.execute(
                "SELECT COUNT(*) FROM review_context_packets "
                "WHERE review_id=?",
                (review_id,),
            ).fetchone()[0]
        self.assertEqual(expired, ["ctx_req_001"])
        self.assertEqual(raw_count, 0)
        self.assertEqual(link_count, 0)
        self.assertIsNotNone(captured)
        self.assertEqual(captured.code, "CONTEXT_REQUEST_BUSY")

    def test_packet_inherits_task_and_sender_from_batch(self) -> None:
        payload = context_batch([100], position="before")
        del payload["packets"][0]["task_id"]
        del payload["packets"][0]["sender_id"]

        result = self.receiver.receive_batch(payload)

        self.assertEqual(result["results"][0]["status"], "accepted")

    def test_accepts_batch_from_different_task_for_same_sender(self) -> None:
        payload = context_batch([81], position="before")
        payload["task_id"] = "task_00002"
        del payload["packets"][0]["task_id"]

        result = self.receiver.receive_batch(payload)

        self.assertEqual(result["results"][0]["status"], "accepted")
        with connect(self.database_path) as connection:
            stored_task_id = connection.execute(
                "SELECT task_id FROM raw_packet_index "
                "WHERE sender_id='sender_01' AND packet_id='batch_000081'"
            ).fetchone()[0]
        self.assertEqual(stored_task_id, "task_00002")

    def test_rejects_after_batch_for_preceding_only_request(self) -> None:
        with self.assertRaises(ContractError) as captured:
            self.receiver.receive_batch(
                context_batch([102], position="after")
            )

        self.assertEqual(captured.exception.code, "INVALID_CONTEXT_BATCH")

    def test_explicit_packet_identity_mismatch_is_rejected(self) -> None:
        payload = context_batch([100], position="before")
        payload["packets"][0]["sender_id"] = "other_sender"

        result = self.receiver.receive_batch(payload)

        self.assertEqual(
            result["results"][0]["error_code"],
            "INVALID_CONTEXT_PACKET",
        )

    def test_missing_packet_field_uses_stable_item_error_code(self) -> None:
        payload = context_batch([100], position="before")
        del payload["packets"][0]["data"]

        result = self.receiver.receive_batch(payload)

        self.assertEqual(
            result["results"][0]["error_code"],
            "INVALID_CONTEXT_PACKET",
        )

    def test_rejects_packet_id_that_can_escape_raw_storage(self) -> None:
        for unsafe_packet_id in ("..\\outside", "packet:alternate"):
            with self.subTest(packet_id=unsafe_packet_id):
                payload = context_batch([100], position="before")
                payload["packets"][0]["packet_id"] = unsafe_packet_id

                result = self.receiver.receive_batch(payload)

                self.assertEqual(result["results"], [{
                    "packet_id": unsafe_packet_id,
                    "sequence_number": 100,
                    "status": "rejected",
                    "error_code": "INVALID_CONTEXT_PACKET",
                }])
        with connect(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM raw_packet_index "
                "WHERE packet_id IN ('..\\outside','packet:alternate')"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_rejects_packet_whose_timestamp_cannot_connect_to_anchor(self) -> None:
        payload = context_batch([100], position="before")
        payload["packets"][0]["end_generate_timestamp_ns"] += 1

        result = self.receiver.receive_batch(payload)

        self.assertEqual(result["results"][0], {
            "packet_id": "batch_000100",
            "sequence_number": 100,
            "status": "rejected",
            "error_code": "TIMESTAMP_MISMATCH",
        })

    def test_rejects_nonfinite_signal_value(self) -> None:
        payload = context_batch([100], position="before")
        payload["packets"][0]["data"]["phase_current_1_A"]["values"][0] = (
            math.nan
        )

        result = self.receiver.receive_batch(payload)

        self.assertEqual(
            result["results"][0]["error_code"],
            "NONFINITE_VALUE",
        )

    def test_rejects_duplicate_packet_identifier_within_batch(self) -> None:
        payload = context_batch([99, 100], position="before")
        payload["packets"][1]["packet_id"] = "batch_000099"

        result = self.receiver.receive_batch(payload)

        self.assertEqual(result["results"][0]["status"], "accepted")
        self.assertEqual(result["results"][1]["status"], "rejected")
        self.assertEqual(
            result["results"][1]["error_code"],
            "INVALID_CONTEXT_PACKET",
        )

    def test_expired_batch_is_rejected_and_marks_review_insufficient(self) -> None:
        second_database = Path(self.temporary_directory.name) / "expired.db"
        review_id = create_review(second_database)
        create_context_request(
            second_database,
            review_id,
            deadline_at_ns=1_500_000_000,
        )

        with self.assertRaises(ContractError) as captured:
            RawContextReceiver(
                second_database,
                clock_ns=lambda: 2_000_000_000,
            ).receive_batch(
                context_batch([100], position="before")
            )

        self.assertEqual(
            captured.exception.code,
            "CONTEXT_REQUEST_EXPIRED",
        )
        review = CloudReviewRepository(second_database).get(review_id)
        self.assertEqual(review["context_status"], "insufficient_context")

    def test_expired_batch_finalizes_existing_contiguous_context_as_partial(
        self,
    ) -> None:
        second_database = Path(self.temporary_directory.name) / "partial.db"
        review_id = create_review(second_database)
        create_context_request(
            second_database,
            review_id,
            deadline_at_ns=1_500_000_000,
        )
        RawContextReceiver(
            second_database,
            clock_ns=lambda: 1_000_000_000,
        ).receive_batch(
            context_batch(list(range(85, 95)), position="before")
        )
        RawContextReceiver(
            second_database,
            clock_ns=lambda: 1_000_000_000,
        ).receive_batch(
            context_batch(list(range(95, 101)), position="before")
        )

        with self.assertRaises(ContractError) as captured:
            RawContextReceiver(
                second_database,
                clock_ns=lambda: 2_000_000_000,
            ).receive_batch(context_batch([84], position="before"))

        self.assertEqual(captured.exception.code, "CONTEXT_REQUEST_EXPIRED")
        request = RawContextRequestRepository(second_database).get("ctx_req_001")
        review = CloudReviewRepository(second_database).get(review_id)
        self.assertEqual(request["request_status"], "partial_context")
        self.assertEqual(review["context_status"], "partial_context")

    def test_same_packet_with_changed_content_is_conflict(self) -> None:
        payload = context_batch([100], position="before")
        self.receiver.receive_batch(payload)
        changed = copy.deepcopy(payload)
        changed["packets"][0]["data"]["vibration"]["values"][0] = 9.9

        result = self.receiver.receive_batch(changed)

        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(
            result["results"][0]["error_code"],
            "PACKET_CONTENT_CONFLICT",
        )

    def test_same_task_sequence_with_different_packet_id_is_conflict(self) -> None:
        self.receiver.receive_batch(context_batch([100], position="before"))
        payload = context_batch([100], position="before")
        payload["packets"][0]["packet_id"] = "different_packet"

        result = self.receiver.receive_batch(payload)

        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(
            result["results"][0]["error_code"],
            "TASK_SEQUENCE_CONFLICT",
        )

    def test_link_failure_rolls_back_raw_index_and_new_file(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_context_link "
                "BEFORE INSERT ON review_context_packets "
                "BEGIN SELECT RAISE(ABORT, 'forced link failure'); END"
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.receiver.receive_batch(
                context_batch([100], position="before")
            )

        with connect(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM raw_packet_index "
                "WHERE packet_id='batch_000100'"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(
            list((self.database_path.parent / "raw").rglob(
                "batch_000100*.json.gz"
            )),
            [],
        )

    def test_concurrent_conflicting_uploads_leave_index_and_file_consistent(
        self,
    ) -> None:
        first = context_batch([100], position="before")
        second = copy.deepcopy(first)
        second["packets"][0]["data"]["vibration"]["values"][0] = 9.9
        barrier = Barrier(2)

        def receive(payload: dict) -> dict:
            barrier.wait()
            return RawContextReceiver(
                self.database_path,
                clock_ns=lambda: 2_000_000_000,
            ).receive_batch(payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(receive, (first, second)))

        statuses = sorted(
            response["results"][0]["status"] for response in responses
        )
        self.assertEqual(statuses, ["accepted", "conflict"])
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT storage_path,payload_sha256 FROM raw_packet_index "
                "WHERE sender_id='sender_01' AND packet_id='batch_000100'"
            ).fetchone()
        stored_path = self.database_path.parent / "raw" / row["storage_path"]
        with gzip.open(stored_path, "rb") as stream:
            stored_payload = stream.read()
        self.assertEqual(
            hashlib.sha256(stored_payload).hexdigest(),
            row["payload_sha256"],
        )

    def test_all_twenty_positions_mark_context_ready_without_completing_review(self) -> None:
        before = self.receiver.receive_batch(
            context_batch(list(range(81, 91)), position="before")
        )
        preceding = self.receiver.receive_batch(
            context_batch(
                list(range(91, 101)),
                position="before",
                status="complete",
            )
        )

        self.assertEqual(before["context_status"], "pending_context")
        self.assertEqual(preceding["context_status"], "complete")
        request = RawContextRequestRepository(self.database_path).get(
            "ctx_req_001"
        )
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(request["request_status"], "complete")
        self.assertEqual(review["context_status"], "complete")
        self.assertEqual(review["review_status"], "preliminary")

    def test_complete_request_cannot_regress_to_insufficient(self) -> None:
        self.receiver.receive_batch(
            context_batch(list(range(81, 91)), position="before")
        )
        self.receiver.receive_batch(
            context_batch(list(range(91, 101)), position="before")
        )

        late_missing = self.receiver.receive_batch(
            context_batch(
                [91],
                position="before",
                status="insufficient_context",
                missing=[95],
            )
        )

        self.assertEqual(late_missing["context_status"], "complete")
        request = RawContextRequestRepository(self.database_path).get(
            "ctx_req_001"
        )
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(request["request_status"], "complete")
        self.assertEqual(review["context_status"], "complete")
        self.assertEqual(review["review_status"], "preliminary")

    def test_edge_reported_missing_packet_keeps_context_pending_until_deadline(self) -> None:
        result = self.receiver.receive_batch(
            context_batch(
                [91],
                position="before",
                status="insufficient_context",
                missing=[95],
            )
        )

        self.assertEqual(result["context_status"], "pending_context")
        request = RawContextRequestRepository(self.database_path).get("ctx_req_001")
        review = CloudReviewRepository(self.database_path).get(self.review_id)
        self.assertEqual(request["request_status"], "pending_context")
        self.assertEqual(request["last_error_code"], "EDGE_INSUFFICIENT_CONTEXT")
        self.assertEqual(review["review_status"], "preliminary")

    def test_partial_context_status_is_returned_as_partial(self) -> None:
        self.assertEqual(_context_status("partial_context"), "partial_context")


def response_body(response: JSONResponse) -> dict:
    return json.loads(response.body.decode("utf-8"))


class RawContextHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "cloud.db"
        os.environ["CLOUD_REVIEW_DB_PATH"] = str(self.database_path)
        review_id = create_review(self.database_path)
        create_context_request(self.database_path, review_id)

    def tearDown(self) -> None:
        os.environ.pop("CLOUD_REVIEW_DB_PATH", None)
        self.temporary_directory.cleanup()

    def test_http_route_returns_per_item_acknowledgement(self) -> None:
        client = TestClient(app)

        response = client.post(
            "/cloud/raw-context-batches",
            json=context_batch([100], position="before"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [{
            "packet_id": "batch_000100",
            "sequence_number": 100,
            "status": "accepted",
        }])
        self.assertTrue(
            any(
                route.path == "/cloud/raw-context-batches"
                for route in app.routes
            )
        )

    def test_cloud_infer_injects_production_context_transport(self) -> None:
        transport = Mock()
        expected = {"success": True}
        with (
            patch(
                "cloud_service.app._raw_context_transport",
                return_value=transport,
            ),
            patch(
                "cloud_service.app.infer_cloud",
                return_value=expected,
            ) as infer,
        ):
            from cloud_service.app import cloud_infer

            result = cloud_infer({"trigger": "packet"})

        self.assertEqual(result, expected)
        infer.assert_called_once_with(
            {"trigger": "packet"},
            context_transport=transport,
        )

    def test_lifespan_expires_request_without_followup_traffic(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE raw_context_request SET deadline_at_ns=? "
                "WHERE request_id='ctx_req_001'",
                (time.time_ns() + 100_000_000,),
            )

        with TestClient(app):
            stop_at = time.monotonic() + 2.0
            while time.monotonic() < stop_at:
                status = RawContextRequestRepository(
                    self.database_path
                ).get("ctx_req_001")["request_status"]
                if status == "insufficient_context":
                    break
                time.sleep(0.05)

        self.assertEqual(status, "insufficient_context")

    def test_invalid_envelope_and_unknown_request_return_http_400(self) -> None:
        invalid = context_batch([100], position="before")
        invalid["item_count"] = 2
        invalid_response = raw_context_batches(invalid)
        missing_field = context_batch([100], position="before")
        del missing_field["request_id"]
        missing_field_response = raw_context_batches(missing_field)
        unknown = context_batch([100], position="before")
        unknown["request_id"] = "unknown"
        unknown_response = raw_context_batches(unknown)

        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(
            response_body(invalid_response)["error_code"],
            "INVALID_CONTEXT_BATCH",
        )
        self.assertEqual(missing_field_response.status_code, 400)
        self.assertEqual(
            response_body(missing_field_response)["error_code"],
            "INVALID_CONTEXT_BATCH",
        )
        self.assertEqual(unknown_response.status_code, 400)
        self.assertEqual(
            response_body(unknown_response)["error_code"],
            "UNKNOWN_CONTEXT_REQUEST",
        )

    def test_database_error_returns_http_503(self) -> None:
        with patch(
            "cloud_service.app.RawContextReceiver.receive_batch",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            response = raw_context_batches(
                context_batch([100], position="before")
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response_body(response)["error_code"],
            "SERVICE_UNAVAILABLE",
        )


if __name__ == "__main__":
    unittest.main()
