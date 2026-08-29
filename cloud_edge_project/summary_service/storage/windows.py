from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from core.action_level_contract import ACTION_TO_LEVEL, build_final_decision

from ..aggregation import build_arbitration_request
from ..contracts import canonical_json, stable_id
from ..sync_contract import build_summary_window_sync_payload
from .metrics import increment_counter


def save_window_result(
    connection: sqlite3.Connection, result: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Persist a window result; each distinct payload bumps the revision."""

    arbitration_request = (
        build_arbitration_request(result) if result.get("has_conflict") else None
    )
    summary_window_id = str(result["summary_window_id"])
    summary_result_id = str(result["summary_result_id"])
    closed_at_ns = int(result["closed_at_ns"])
    existing = connection.execute(
        "SELECT result_status, revision, payload_json "
        "FROM summary_window_result WHERE summary_window_id = ?",
        (summary_window_id,),
    ).fetchone()
    if existing is not None:
        payload = dict(result)
        payload["revision"] = int(existing["revision"])
        if existing["payload_json"] == canonical_json(payload):
            return None
        existing_status = str(existing["result_status"])
        incoming_status = str(result["result_status"])
        # Window closure runs concurrently with MQTT ingestion. A stale
        # timeout snapshot must never replace a newer settled result.
        # Only a late, complete result may advance an INCOMPLETE window.
        if existing_status != "INCOMPLETE" or incoming_status == "INCOMPLETE":
            return None
    revision = int(existing["revision"]) + 1 if existing is not None else 1
    payload = dict(result)
    payload["revision"] = revision
    payload_json = canonical_json(payload)
    if existing is None:
        connection.execute(
            """
            INSERT INTO summary_window_result (
                summary_result_id, summary_window_id, device_id, run_id,
                window_start_sequence, window_end_sequence, result_status,
                revision, has_conflict, excluded_from_formal_metrics,
                payload_json, created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary_result_id,
                summary_window_id,
                str(result["device_id"]),
                result.get("run_id"),
                int(result["window_start_sequence"]),
                int(result["window_end_sequence"]),
                str(result["result_status"]),
                revision,
                int(bool(result["has_conflict"])),
                int(bool(result["excluded_from_formal_metrics"])),
                payload_json,
                closed_at_ns,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE summary_window_result
            SET result_status = ?, revision = ?, has_conflict = ?,
                excluded_from_formal_metrics = ?, payload_json = ?,
                created_at_ns = ?
            WHERE summary_window_id = ?
            """,
            (
                str(result["result_status"]),
                revision,
                int(bool(result["has_conflict"])),
                int(bool(result["excluded_from_formal_metrics"])),
                payload_json,
                closed_at_ns,
                summary_window_id,
            ),
        )

    _insert_delivery(
        connection,
        table="summary_window_publish_outbox",
        request_id=stable_id("publish", summary_result_id, revision),
        summary_result_id=summary_result_id,
        revision=revision,
        payload=payload,
        created_at_ns=closed_at_ns,
    )
    _insert_delivery(
        connection,
        table="summary_window_sync_outbox",
        request_id=stable_id("sync", summary_result_id, revision),
        summary_result_id=summary_result_id,
        revision=revision,
        payload=build_summary_window_sync_payload(payload),
        created_at_ns=closed_at_ns,
    )
    if arbitration_request is not None:
        connection.execute(
            """
            INSERT OR IGNORE INTO summary_arbitration_outbox (
                request_id, conflict_id, summary_result_id, revision,
                payload_json, state, attempts, next_attempt_at_ns,
                created_at_ns
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, 0, ?)
            """,
            (
                stable_id("arbitration", arbitration_request["conflict_id"]),
                arbitration_request["conflict_id"],
                summary_result_id,
                revision,
                canonical_json(arbitration_request),
                closed_at_ns,
            ),
        )
    if str(result["result_status"]) == "FINAL":
        _enqueue_suggestion_task(
            connection,
            summary_result_id=summary_result_id,
            revision=revision,
            source=payload,
            created_at_ns=closed_at_ns,
        )
    return payload


def apply_arbitration_result(
    connection: sqlite3.Connection,
    summary_result_id: str,
    arbitration: Mapping[str, Any],
    *,
    now_ns: int,
) -> dict[str, Any]:
    """Apply a Cloud arbitration outcome atomically."""

    row = connection.execute(
        """
        SELECT result_status, revision, payload_json FROM summary_window_result
        WHERE summary_result_id = ?
        """,
        (str(summary_result_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown summary window: {summary_result_id}")
    payload = json.loads(row["payload_json"])
    arbitration_id = arbitration.get("arbitration_id")
    if row["result_status"] in {"FINAL", "MANUAL_REVIEW"}:
        if payload.get("arbitration_id") == arbitration_id:
            return payload
        raise ValueError(f"summary window {summary_result_id} is not pending arbitration")
    if row["result_status"] != "PENDING_ARBITRATION":
        raise ValueError(f"summary window {summary_result_id} is not pending arbitration")

    status = str(arbitration.get("status", "")).strip().lower()
    raw_final_state = arbitration.get("final_state")
    if raw_final_state is not None and not isinstance(raw_final_state, str):
        raise ValueError("cloud final_state must be a string or null")
    cloud_final_state = (
        str(raw_final_state).strip().lower()
        if isinstance(raw_final_state, str)
        else None
    )
    raw_final_action = arbitration.get("final_action")
    final_action = (
        raw_final_action.strip()
        if isinstance(raw_final_action, str) and raw_final_action.strip()
        else None
    )
    # The maintenance action is the sole authority. final_state never decides
    # FINAL vs MANUAL_REVIEW.
    if status == "resolved" and final_action in ACTION_TO_LEVEL:
        new_status = "FINAL"
        arbitration_status = "RESOLVED"
    elif status == "manual_review" or (
        status == "resolved" and final_action not in ACTION_TO_LEVEL
    ):
        new_status = "MANUAL_REVIEW"
        arbitration_status = "MANUAL_REVIEW"
    else:
        raise ValueError(
            f"unsupported arbitration outcome: status={status}, final_action={final_action}"
        )

    revision = int(row["revision"]) + 1
    confidence = arbitration.get("confidence")
    payload.update(
        {
            "result_status": new_status,
            "arbitration_status": arbitration_status,
            "arbitration_id": arbitration_id,
            "final_state": None,
            "final_action": str(final_action) if final_action else None,
            "final_source": "cloud_arbitration",
            "arbitration_confidence": (
                float(confidence)
                if isinstance(confidence, (int, float))
                and 0.0 <= float(confidence) <= 1.0
                else None
            ),
            "revision": revision,
            "arbitrated_at_ns": int(now_ns),
        }
    )
    if new_status == "FINAL":
        decision = build_final_decision(ACTION_TO_LEVEL[final_action])
        if cloud_final_state is not None and cloud_final_state != decision["final_state"]:
            raise ValueError(
                f"cloud final_state {cloud_final_state!r} does not match final_action {final_action!r}"
            )
        payload["final_action_level"] = decision["final_action_level"]
        payload["recommended_action"] = decision["recommended_action"]
        payload["final_state"] = decision["final_state"]
    else:
        payload["final_action_level"] = None
        payload["recommended_action"] = None
        payload["final_state"] = None
    payload_json = canonical_json(payload)
    connection.execute(
        """
        UPDATE summary_window_result
        SET result_status = ?, revision = ?, payload_json = ?,
            created_at_ns = ?
        WHERE summary_result_id = ?
        """,
        (new_status, revision, payload_json, int(now_ns), str(summary_result_id)),
    )
    _insert_delivery(
        connection,
        table="summary_window_publish_outbox",
        request_id=stable_id("publish", summary_result_id, revision),
        summary_result_id=summary_result_id,
        revision=revision,
        payload=payload,
        created_at_ns=int(now_ns),
    )
    _insert_delivery(
        connection,
        table="summary_window_sync_outbox",
        request_id=stable_id("sync", summary_result_id, revision),
        summary_result_id=summary_result_id,
        revision=revision,
        payload=build_summary_window_sync_payload(payload),
        created_at_ns=int(now_ns),
    )
    if new_status == "FINAL":
        _enqueue_suggestion_task(
            connection,
            summary_result_id=summary_result_id,
            revision=revision,
            source=payload,
            created_at_ns=int(now_ns),
        )
        increment_counter(connection, "arbitration_resolved_windows")
    else:
        increment_counter(connection, "arbitration_manual_review_windows")
    return payload


def _insert_delivery(
    connection: sqlite3.Connection,
    *,
    table: str,
    request_id: str,
    summary_result_id: str,
    revision: int,
    payload: Mapping[str, Any],
    created_at_ns: int,
) -> None:
    if table not in {"summary_window_publish_outbox", "summary_window_sync_outbox"}:
        raise ValueError("unsupported delivery table")
    connection.execute(
        f"""
        INSERT OR IGNORE INTO {table} (
            request_id, summary_result_id, revision, payload_json,
            state, attempts, next_attempt_at_ns, created_at_ns
        ) VALUES (?, ?, ?, ?, 'PENDING', 0, 0, ?)
        """,
        (
            request_id,
            summary_result_id,
            int(revision),
            canonical_json(payload),
            int(created_at_ns),
        ),
    )


def _enqueue_suggestion_task(
    connection: sqlite3.Connection,
    *,
    summary_result_id: str,
    revision: int,
    source: Mapping[str, Any],
    created_at_ns: int,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO summary_suggestion_task (
            summary_result_id, revision, source_json, state, attempts,
            next_attempt_at_ns, created_at_ns, updated_at_ns
        ) VALUES (?, ?, ?, 'PENDING', 0, 0, ?, ?)
        """,
        (
            str(summary_result_id),
            int(revision),
            canonical_json(source),
            int(created_at_ns),
            int(created_at_ns),
        ),
    )
