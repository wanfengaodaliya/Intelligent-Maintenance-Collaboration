"""SQLite DDL for sender-keyed cloud review persistence."""

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at_ns INTEGER NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS senders (
    sender_id TEXT PRIMARY KEY, created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL,
    sender_config_version TEXT, sensor_unit_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (json_valid(sensor_unit_json))
);
CREATE TABLE IF NOT EXISTS edge_packet_summary (
    sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, task_id TEXT NOT NULL, sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    edge_node_id TEXT NOT NULL, end_timestamp_ns INTEGER NOT NULL, summary_generated_at_ns INTEGER NOT NULL,
    received_at_ns INTEGER NOT NULL, perception_status TEXT NOT NULL CHECK (perception_status IN ('good', 'warning')),
    perception_flags_json TEXT NOT NULL DEFAULT '[]',
    vibration_source_sample_rate_hz INTEGER NOT NULL, vibration_analysis_sample_rate_hz INTEGER NOT NULL, vibration_unit TEXT NOT NULL,
    vibration_rms REAL NOT NULL, vibration_absolute_peak REAL NOT NULL, vibration_kurtosis REAL NOT NULL,
    vibration_dominant_frequency_hz REAL NOT NULL, vibration_band_power_ratio_500_2000 REAL NOT NULL, vibration_spectral_entropy REAL NOT NULL,
    current_1_source_sample_rate_hz INTEGER NOT NULL, current_1_analysis_sample_rate_hz INTEGER NOT NULL, current_1_unit TEXT NOT NULL,
    current_1_rms_a REAL NOT NULL, current_1_absolute_peak_a REAL NOT NULL,
    current_2_source_sample_rate_hz INTEGER NOT NULL, current_2_analysis_sample_rate_hz INTEGER NOT NULL, current_2_unit TEXT NOT NULL,
    current_2_rms_a REAL NOT NULL, current_2_absolute_peak_a REAL NOT NULL, current_imbalance_ratio REAL NOT NULL,
    shaft_speed_rpm_mean REAL NOT NULL, shaft_speed_rpm_last REAL NOT NULL, shaft_speed_rpm_minimum REAL NOT NULL,
    shaft_speed_rpm_maximum REAL NOT NULL, shaft_speed_rpm_standard_deviation REAL NOT NULL,
    load_torque_nm_mean REAL NOT NULL, load_torque_nm_last REAL NOT NULL, load_torque_nm_minimum REAL NOT NULL,
    load_torque_nm_maximum REAL NOT NULL, load_torque_nm_standard_deviation REAL NOT NULL,
    bearing_radial_load_n_mean REAL NOT NULL CHECK (bearing_radial_load_n_mean >= 0), bearing_radial_load_n_last REAL NOT NULL,
    bearing_radial_load_n_minimum REAL NOT NULL, bearing_radial_load_n_maximum REAL NOT NULL,
    bearing_radial_load_n_standard_deviation REAL NOT NULL, bearing_module_temperature_c REAL NOT NULL,
    edge_result TEXT NOT NULL CHECK (edge_result IN ('normal', 'warning', 'abnormal')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    edge_risk_level TEXT NOT NULL CHECK (edge_risk_level IN ('low', 'medium', 'high')),
    edge_model_version TEXT NOT NULL, summary_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (sender_id, packet_id), UNIQUE (sender_id, task_id, sequence_number),
    FOREIGN KEY (sender_id) REFERENCES senders(sender_id),
    CHECK (json_valid(perception_flags_json)), CHECK (json_valid(summary_json))
);
CREATE INDEX IF NOT EXISTS idx_edge_summary_sender_time ON edge_packet_summary(sender_id, end_timestamp_ns);
CREATE INDEX IF NOT EXISTS idx_edge_summary_edge_received ON edge_packet_summary(edge_node_id, received_at_ns);
CREATE TABLE IF NOT EXISTS raw_packet_index (
    sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, task_id TEXT NOT NULL, sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    start_timestamp_ns INTEGER NOT NULL, end_generate_timestamp_ns INTEGER NOT NULL, sample_rate_hz INTEGER NOT NULL CHECK (sample_rate_hz > 0),
    sample_count INTEGER NOT NULL CHECK (sample_count > 0), storage_path TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    compressed_size_bytes INTEGER NOT NULL CHECK (compressed_size_bytes > 0), validation_status TEXT NOT NULL CHECK (validation_status IN ('valid', 'warning', 'invalid')),
    received_at_ns INTEGER NOT NULL, PRIMARY KEY (sender_id, packet_id), CHECK (end_generate_timestamp_ns > start_timestamp_ns),
    UNIQUE (sender_id, task_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_raw_packet_sender_time ON raw_packet_index(sender_id, end_generate_timestamp_ns);
CREATE TABLE IF NOT EXISTS cloud_review (
    review_id TEXT PRIMARY KEY, sender_id TEXT NOT NULL, anchor_packet_id TEXT NOT NULL, task_id TEXT NOT NULL,
    feature_extractor_version TEXT NOT NULL, schema_version TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status IN ('preliminary', 'complete', 'insufficient_context', 'invalid')),
    context_status TEXT NOT NULL CHECK (context_status IN ('pending_context', 'complete', 'insufficient_context', 'not_requested', 'invalid')),
    data_quality_valid INTEGER NOT NULL CHECK (data_quality_valid IN (0, 1)), start_timestamp_ns INTEGER, end_timestamp_ns INTEGER,
    packet_count INTEGER NOT NULL DEFAULT 1 CHECK (packet_count > 0), data_quality_json TEXT NOT NULL,
    cloud_recomputed_features_json TEXT, cloud_enhanced_features_json TEXT, advanced_features_json TEXT, context_features_json TEXT,
    created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL, UNIQUE (sender_id, anchor_packet_id, feature_extractor_version),
    FOREIGN KEY (sender_id, anchor_packet_id) REFERENCES edge_packet_summary(sender_id, packet_id),
    CHECK (json_valid(data_quality_json)), CHECK (cloud_recomputed_features_json IS NULL OR json_valid(cloud_recomputed_features_json)),
    CHECK (cloud_enhanced_features_json IS NULL OR json_valid(cloud_enhanced_features_json)), CHECK (advanced_features_json IS NULL OR json_valid(advanced_features_json)),
    CHECK (context_features_json IS NULL OR json_valid(context_features_json))
);
CREATE INDEX IF NOT EXISTS idx_cloud_review_sender_time ON cloud_review(sender_id, updated_at_ns);
CREATE TABLE IF NOT EXISTS review_context_packets (
    review_id TEXT NOT NULL, sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, relative_position INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('before', 'anchor', 'after')), PRIMARY KEY (review_id, sender_id, packet_id),
    FOREIGN KEY (review_id) REFERENCES cloud_review(review_id) ON DELETE CASCADE,
    FOREIGN KEY (sender_id, packet_id) REFERENCES raw_packet_index(sender_id, packet_id)
);
CREATE TABLE IF NOT EXISTS diagnosis_events (
    event_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, diagnosis_model_version TEXT,
    diagnosis_status TEXT NOT NULL CHECK (diagnosis_status IN ('pending', 'completed', 'skipped', 'failed')),
    result_json TEXT, human_review_json TEXT, created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL,
    FOREIGN KEY (review_id) REFERENCES cloud_review(review_id), CHECK (result_json IS NULL OR json_valid(result_json)),
    CHECK (human_review_json IS NULL OR json_valid(human_review_json))
);
CREATE TABLE IF NOT EXISTS ingestion_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT NOT NULL, packet_id TEXT NOT NULL,
    existing_payload_sha256 TEXT NOT NULL, incoming_payload_sha256 TEXT NOT NULL, incoming_summary_json TEXT NOT NULL,
    detected_at_ns INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)), CHECK (json_valid(incoming_summary_json))
);
"""
