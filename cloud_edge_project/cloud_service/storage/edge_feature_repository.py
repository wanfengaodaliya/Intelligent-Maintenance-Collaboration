"""Sender-keyed storage for edge summaries defined by the cloud-ingestion API."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .database import connect


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class EdgeFeatureRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def ingest(self, summary: dict) -> str:
        """Persist one documented summary and return its idempotency outcome."""

        serialized = _json(summary)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        now = time.time_ns()
        sender_id, packet_id = summary["sender_id"], summary["packet_id"]
        task_id, sequence_number = summary["task_id"], summary["sequence_number"]
        features = summary["features"]
        vibration = features["vibration"]
        current_1 = features["phase_current_1"]
        current_2 = features["phase_current_2"]
        context = features["operating_context"]
        inference = summary["edge_inference"]
        quality = summary["perception_quality"]

        with connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO senders(sender_id,created_at_ns,updated_at_ns) VALUES (?,?,?) "
                "ON CONFLICT(sender_id) DO UPDATE SET updated_at_ns=excluded.updated_at_ns",
                (sender_id, now, now),
            )
            existing = connection.execute(
                "SELECT payload_sha256 FROM edge_packet_summary WHERE sender_id=? AND packet_id=?",
                (sender_id, packet_id),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] == digest:
                    connection.execute(
                        "UPDATE edge_packet_summary SET received_at_ns=? WHERE sender_id=? AND packet_id=?",
                        (now, sender_id, packet_id),
                    )
                    return "duplicate"
                self._record_conflict(connection, sender_id, packet_id, existing["payload_sha256"], digest, serialized, now)
                return "conflict"

            sequence_row = connection.execute(
                "SELECT packet_id,payload_sha256 FROM edge_packet_summary "
                "WHERE sender_id=? AND task_id=? AND sequence_number=?",
                (sender_id, task_id, sequence_number),
            ).fetchone()
            if sequence_row:
                self._record_conflict(connection, sender_id, packet_id, sequence_row["payload_sha256"], digest, serialized, now)
                return "conflict"

            columns = (
                "sender_id,packet_id,task_id,sequence_number,edge_node_id,end_timestamp_ns,summary_generated_at_ns,received_at_ns,"
                "perception_status,perception_flags_json,"
                "vibration_source_sample_rate_hz,vibration_analysis_sample_rate_hz,vibration_unit,"
                "vibration_rms,vibration_absolute_peak,vibration_kurtosis,vibration_dominant_frequency_hz,vibration_band_power_ratio_500_2000,vibration_spectral_entropy,"
                "current_1_source_sample_rate_hz,current_1_analysis_sample_rate_hz,current_1_unit,current_1_rms_a,current_1_absolute_peak_a,"
                "current_2_source_sample_rate_hz,current_2_analysis_sample_rate_hz,current_2_unit,current_2_rms_a,current_2_absolute_peak_a,current_imbalance_ratio,"
                "shaft_speed_rpm_mean,shaft_speed_rpm_last,shaft_speed_rpm_minimum,shaft_speed_rpm_maximum,shaft_speed_rpm_standard_deviation,"
                "load_torque_nm_mean,load_torque_nm_last,load_torque_nm_minimum,load_torque_nm_maximum,load_torque_nm_standard_deviation,"
                "bearing_radial_load_n_mean,bearing_radial_load_n_last,bearing_radial_load_n_minimum,bearing_radial_load_n_maximum,bearing_radial_load_n_standard_deviation,"
                "bearing_module_temperature_c,edge_result,confidence,edge_risk_level,edge_model_version,summary_json,payload_sha256"
            )
            values = (
                sender_id, packet_id, task_id, sequence_number, summary["edge_node_id"], summary["end_timestamp_ns"],
                summary["summary_generated_at_ns"], now, quality["status"], _json(quality["flags"]),
                vibration["source_sample_rate_hz"], vibration["analysis_sample_rate_hz"], vibration["unit"],
                vibration["rms"], vibration["absolute_peak"], vibration["kurtosis"], vibration["dominant_frequency_hz"],
                vibration["band_power_ratio_500_2000"], vibration["spectral_entropy"],
                current_1["source_sample_rate_hz"], current_1["analysis_sample_rate_hz"], current_1["unit"], current_1["rms_a"], current_1["absolute_peak_a"],
                current_2["source_sample_rate_hz"], current_2["analysis_sample_rate_hz"], current_2["unit"], current_2["rms_a"], current_2["absolute_peak_a"],
                features["current_relationship"]["current_imbalance_ratio"],
                *self._statistics(context["shaft_speed_rpm"]), *self._statistics(context["load_torque_nm"]),
                *self._statistics(context["bearing_radial_load_n"]), context["bearing_module_temperature_c"],
                inference["edge_result"], inference["confidence"], inference["edge_risk_level"], summary["edge_model_version"], serialized, digest,
            )
            connection.execute(
                f"INSERT INTO edge_packet_summary ({columns}) VALUES ({','.join('?' for _ in values)})",
                values,
            )
        return "accepted"

    @staticmethod
    def _statistics(values: dict) -> tuple[object, object, object, object, object]:
        return (values["mean"], values["last"], values["minimum"], values["maximum"], values["standard_deviation"])

    @staticmethod
    def _record_conflict(connection, sender_id: str, packet_id: str, existing_digest: str,
                         incoming_digest: str, serialized: str, now: int) -> None:
        connection.execute(
            "INSERT INTO ingestion_conflicts(sender_id,packet_id,existing_payload_sha256,incoming_payload_sha256,incoming_summary_json,detected_at_ns) "
            "VALUES (?,?,?,?,?,?)",
            (sender_id, packet_id, existing_digest, incoming_digest, serialized, now),
        )
