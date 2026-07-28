"""Sender-keyed, all-packet edge feature history."""

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
        serialized = _json(summary)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        now, sender_id, packet_id = time.time_ns(), summary["sender_id"], summary["packet_id"]
        context, features = summary["operating_context"], summary["features"]
        with connect(self.database_path) as connection:
            connection.execute("INSERT INTO senders(sender_id,created_at_ns,updated_at_ns) VALUES (?,?,?) ON CONFLICT(sender_id) DO UPDATE SET updated_at_ns=excluded.updated_at_ns", (sender_id, now, now))
            existing = connection.execute("SELECT payload_sha256 FROM edge_packet_summary WHERE sender_id=? AND packet_id=?", (sender_id, packet_id)).fetchone()
            if existing:
                if existing["payload_sha256"] == digest:
                    connection.execute("UPDATE edge_packet_summary SET received_at_ns=? WHERE sender_id=? AND packet_id=?", (now, sender_id, packet_id))
                    return "retransmitted"
                connection.execute("INSERT INTO ingestion_conflicts(sender_id,packet_id,existing_payload_sha256,incoming_payload_sha256,incoming_summary_json,detected_at_ns) VALUES (?,?,?,?,?,?)", (sender_id, packet_id, existing["payload_sha256"], digest, serialized, now))
                return "conflict"
            values = [
                sender_id, packet_id, summary["task_id"], summary["sequence_number"], summary["end_generate_timestamp_ns"], summary["feature_generated_at_ns"], now,
                summary["edge_feature_extractor_version"], summary.get("edge_model_version"), summary["perception_status"], _json(summary.get("perception_flags", [])),
                context["shaft_speed_rpm"]["mean"], context["load_torque_nm"]["mean"], context["bearing_radial_load_n"]["mean"], context["bearing_module_temperature_c"],
                features["vibration_rms"], features["vibration_absolute_peak"], features["vibration_kurtosis"], features["vibration_dominant_frequency_hz"], features["vibration_band_power_ratio_500_2000"], features["vibration_spectral_entropy"],
                features["current_1_rms_a"], features["current_1_absolute_peak_a"], features.get("current_1_fundamental_frequency_hz"), features.get("current_1_thd"),
                features["current_2_rms_a"], features["current_2_absolute_peak_a"], features.get("current_2_fundamental_frequency_hz"), features.get("current_2_thd"), features["current_imbalance_ratio"], serialized, digest,
            ]
            columns = "sender_id,packet_id,task_id,sequence_number,end_generate_timestamp_ns,feature_generated_at_ns,received_at_ns,edge_feature_extractor_version,edge_model_version,perception_status,perception_flags_json,shaft_speed_rpm_mean,load_torque_nm_mean,bearing_radial_load_n_mean,bearing_module_temperature_c,vibration_rms,vibration_absolute_peak,vibration_kurtosis,vibration_dominant_frequency_hz,vibration_band_power_ratio_500_2000,vibration_spectral_entropy,current_1_rms_a,current_1_absolute_peak_a,current_1_fundamental_frequency_hz,current_1_thd,current_2_rms_a,current_2_absolute_peak_a,current_2_fundamental_frequency_hz,current_2_thd,current_imbalance_ratio,summary_json,payload_sha256"
            connection.execute(f"INSERT INTO edge_packet_summary ({columns}) VALUES ({','.join('?' for _ in values)})", values)
        return "inserted"
