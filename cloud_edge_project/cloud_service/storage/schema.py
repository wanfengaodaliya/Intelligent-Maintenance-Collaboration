"""SQLite DDL for sender-keyed cloud review persistence."""

SCHEMA_VERSION = 7

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at_ns INTEGER NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS senders (
    sender_id TEXT PRIMARY KEY, created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL,
    sender_config_version TEXT, sensor_unit_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (json_valid(sensor_unit_json))
);
CREATE TABLE IF NOT EXISTS edge_packet_summary (
    sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, task_id TEXT NOT NULL, sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    edge_node_id TEXT NOT NULL, end_timestamp_ns INTEGER NOT NULL, summary_generated_at_ns INTEGER,
    received_at_ns INTEGER NOT NULL, processing_status TEXT NOT NULL CHECK (processing_status IN ('perception_completed', 'perception_rejected')),
    perception_status TEXT CHECK (perception_status IN ('good', 'warning')), perception_flags_json TEXT,
    perception_error_codes_json TEXT,
    vibration_source_sample_rate_hz INTEGER, vibration_analysis_sample_rate_hz INTEGER, vibration_unit TEXT,
    vibration_rms REAL, vibration_absolute_peak REAL, vibration_kurtosis REAL,
    vibration_dominant_frequency_hz REAL, vibration_band_power_ratio_500_2000 REAL, vibration_spectral_entropy REAL,
    current_1_source_sample_rate_hz INTEGER, current_1_analysis_sample_rate_hz INTEGER, current_1_unit TEXT,
    current_1_rms_a REAL, current_1_absolute_peak_a REAL,
    current_2_source_sample_rate_hz INTEGER, current_2_analysis_sample_rate_hz INTEGER, current_2_unit TEXT,
    current_2_rms_a REAL, current_2_absolute_peak_a REAL, current_imbalance_ratio REAL,
    shaft_speed_rpm_mean REAL, shaft_speed_rpm_last REAL, shaft_speed_rpm_minimum REAL,
    shaft_speed_rpm_maximum REAL, shaft_speed_rpm_standard_deviation REAL,
    load_torque_nm_mean REAL, load_torque_nm_last REAL, load_torque_nm_minimum REAL,
    load_torque_nm_maximum REAL, load_torque_nm_standard_deviation REAL,
    bearing_radial_load_n_mean REAL, bearing_radial_load_n_last REAL,
    bearing_radial_load_n_minimum REAL, bearing_radial_load_n_maximum REAL,
    bearing_radial_load_n_standard_deviation REAL, bearing_module_temperature_c REAL,
    edge_result TEXT CHECK (edge_result IN ('normal', 'warning', 'abnormal')),
    confidence REAL CHECK (confidence >= 0 AND confidence <= 1),
    edge_risk_level TEXT CHECK (edge_risk_level IN ('low', 'medium', 'high')),
    edge_model_version TEXT, summary_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (sender_id, packet_id), UNIQUE (sender_id, task_id, sequence_number),
    FOREIGN KEY (sender_id) REFERENCES senders(sender_id),
    CHECK (perception_flags_json IS NULL OR json_valid(perception_flags_json)),
    CHECK (perception_error_codes_json IS NULL OR json_valid(perception_error_codes_json)), CHECK (json_valid(summary_json))
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
    context_status TEXT NOT NULL CHECK (context_status IN ('pending_context', 'partial_context', 'complete', 'insufficient_context', 'not_requested', 'invalid')),
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
CREATE TABLE IF NOT EXISTS raw_context_request (
    request_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    anchor_packet_id TEXT NOT NULL,
    anchor_sequence_number INTEGER NOT NULL CHECK (anchor_sequence_number > 0),
    before_packet_count INTEGER NOT NULL CHECK (before_packet_count > 0),
    after_packet_count INTEGER NOT NULL CHECK (after_packet_count >= 0),
    minimum_context_packet_count INTEGER NOT NULL CHECK (minimum_context_packet_count > 0),
    request_status TEXT NOT NULL CHECK (
        request_status IN (
            'created', 'dispatched', 'pending_context',
            'partial_context', 'complete', 'insufficient_context',
            'dispatch_failed'
        )
    ),
    requested_at_ns INTEGER NOT NULL,
    deadline_at_ns INTEGER NOT NULL,
    edge_response_json TEXT,
    last_error_code TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    FOREIGN KEY (review_id) REFERENCES cloud_review(review_id) ON DELETE CASCADE,
    CHECK (deadline_at_ns > requested_at_ns),
    CHECK (edge_response_json IS NULL OR json_valid(edge_response_json))
);
CREATE INDEX IF NOT EXISTS idx_raw_context_request_deadline
ON raw_context_request(request_status, deadline_at_ns);
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
CREATE TABLE IF NOT EXISTS summary_ingestion_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT NOT NULL, packet_id TEXT NOT NULL,
    existing_payload_sha256 TEXT NOT NULL, incoming_payload_sha256 TEXT NOT NULL, incoming_summary_json TEXT NOT NULL,
    detected_at_ns INTEGER NOT NULL, conflict_code TEXT NOT NULL CHECK (conflict_code IN ('PACKET_CONTENT_CONFLICT', 'TASK_SEQUENCE_CONFLICT')),
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)), CHECK (json_valid(incoming_summary_json))
);
CREATE TABLE IF NOT EXISTS aggregation_result (
    aggregation_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    preprocessing_config_version TEXT NOT NULL,
    aggregation_status TEXT NOT NULL CHECK (aggregation_status IN ('queued', 'running', 'succeeded', 'failed')),
    context_status TEXT NOT NULL CHECK (context_status IN ('complete', 'partial_context')),
    relative_positions_json TEXT,
    packet_manifest_json TEXT,
    packet_boundaries_json TEXT,
    raw_window_path TEXT,
    raw_window_sha256 TEXT,
    preprocessed_window_path TEXT,
    preprocessed_window_sha256 TEXT,
    sample_counts_json TEXT,
    quality_summary_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_until_ns INTEGER,
    retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
    next_retry_at_ns INTEGER,
    error_code TEXT,
    error_detail TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    succeeded_at_ns INTEGER,
    UNIQUE (review_id, source_fingerprint, preprocessing_config_version),
    FOREIGN KEY (review_id) REFERENCES cloud_review(review_id) ON DELETE CASCADE,
    CHECK (relative_positions_json IS NULL OR json_valid(relative_positions_json)),
    CHECK (packet_manifest_json IS NULL OR json_valid(packet_manifest_json)),
    CHECK (packet_boundaries_json IS NULL OR json_valid(packet_boundaries_json)),
    CHECK (sample_counts_json IS NULL OR json_valid(sample_counts_json)),
    CHECK (quality_summary_json IS NULL OR json_valid(quality_summary_json))
);
CREATE INDEX IF NOT EXISTS idx_aggregation_result_pending
ON aggregation_result(aggregation_status, updated_at_ns);
CREATE TABLE IF NOT EXISTS aggregation_outbox (
    outbox_id TEXT PRIMARY KEY,
    aggregation_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type = 'preprocessed_window_ready'),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending', 'delivered', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    FOREIGN KEY (aggregation_id) REFERENCES aggregation_result(aggregation_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_aggregation_outbox_pending
ON aggregation_outbox(dispatch_status, updated_at_ns);
"""
