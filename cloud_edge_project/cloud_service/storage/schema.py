"""SQLite DDL for sender-keyed cloud review persistence."""

SCHEMA_VERSION = 17

MODEL_UPDATE_TASK_DDL = """
CREATE TABLE IF NOT EXISTS model_update_task (
    update_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    problem_type TEXT NOT NULL,
    problem_context_json TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    candidate_version TEXT,
    training_dataset_id TEXT,
    candidate_artifact_json TEXT,
    status TEXT NOT NULL,
    validation_result_json TEXT,
    confirmation_result_json TEXT,
    distribution_result_json TEXT,
    post_validation_result_json TEXT,
    rollback_requested INTEGER NOT NULL DEFAULT 0 CHECK (rollback_requested IN (0, 1)),
    rollback_target_version TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    UNIQUE (analysis_id, problem_id),
    CHECK (json_valid(problem_context_json)),
    CHECK (json_valid(evidence_snapshot_json)),
    CHECK (candidate_artifact_json IS NULL OR json_valid(candidate_artifact_json)),
    CHECK (validation_result_json IS NULL OR json_valid(validation_result_json)),
    CHECK (confirmation_result_json IS NULL OR json_valid(confirmation_result_json)),
    CHECK (distribution_result_json IS NULL OR json_valid(distribution_result_json)),
    CHECK (post_validation_result_json IS NULL OR json_valid(post_validation_result_json))
);
CREATE INDEX IF NOT EXISTS idx_model_update_analysis
ON model_update_task(analysis_id, created_at_ns);
"""

EDGE_PACKET_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS edge_packet_summary (
    sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, device_id TEXT, task_id TEXT NOT NULL, bearing_id TEXT, sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
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
    edge_result TEXT CHECK (edge_result IN ('normal', 'warning', 'fault')),
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
CREATE INDEX IF NOT EXISTS idx_edge_summary_device_task_bearing ON edge_packet_summary(device_id, task_id, bearing_id, end_timestamp_ns);
"""

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at_ns INTEGER NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS senders (
    sender_id TEXT PRIMARY KEY, created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL,
    device_id TEXT, bearing_id TEXT,
    sender_config_version TEXT, sensor_unit_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (json_valid(sensor_unit_json)),
    CHECK ((device_id IS NULL AND bearing_id IS NULL) OR (device_id IS NOT NULL AND bearing_id IS NOT NULL))
);
""" + EDGE_PACKET_SUMMARY_DDL + """
CREATE TABLE IF NOT EXISTS raw_packet_index (
    sender_id TEXT NOT NULL, packet_id TEXT NOT NULL, device_id TEXT NOT NULL, task_id TEXT NOT NULL, bearing_id TEXT NOT NULL, sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    start_timestamp_ns INTEGER NOT NULL, end_generate_timestamp_ns INTEGER NOT NULL, sample_rate_hz INTEGER NOT NULL CHECK (sample_rate_hz > 0),
    sample_count INTEGER NOT NULL CHECK (sample_count > 0), storage_path TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    compressed_size_bytes INTEGER NOT NULL CHECK (compressed_size_bytes > 0), validation_status TEXT NOT NULL CHECK (validation_status IN ('valid', 'warning', 'invalid')),
    received_at_ns INTEGER NOT NULL, PRIMARY KEY (sender_id, packet_id), CHECK (end_generate_timestamp_ns > start_timestamp_ns),
    UNIQUE (sender_id, task_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_raw_packet_sender_time ON raw_packet_index(sender_id, end_generate_timestamp_ns);
CREATE INDEX IF NOT EXISTS idx_raw_packet_device_bearing_time ON raw_packet_index(device_id, bearing_id, end_generate_timestamp_ns);
CREATE TABLE IF NOT EXISTS cloud_review (
    review_id TEXT PRIMARY KEY, sender_id TEXT NOT NULL, anchor_packet_id TEXT NOT NULL, device_id TEXT NOT NULL, task_id TEXT NOT NULL, bearing_id TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_cloud_review_device_task_bearing ON cloud_review(device_id, task_id, bearing_id, updated_at_ns);
CREATE TABLE IF NOT EXISTS raw_context_request (
    request_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    bearing_id TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS bearing_configuration (
    configuration_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    bearing_id TEXT NOT NULL,
    sender_id TEXT,
    configuration_version TEXT NOT NULL,
    bearing_model TEXT,
    rolling_element_count INTEGER NOT NULL,
    rolling_element_diameter_mm REAL NOT NULL,
    pitch_diameter_mm REAL NOT NULL,
    contact_angle_deg REAL NOT NULL,
    resonance_low_hz REAL,
    resonance_high_hz REAL,
    effective_from_ns INTEGER NOT NULL,
    effective_to_ns INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    source TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    FOREIGN KEY (sender_id) REFERENCES senders(sender_id),
    CHECK (
        resonance_low_hz IS NULL
        OR resonance_high_hz IS NULL
        OR resonance_low_hz < resonance_high_hz
    )
);
CREATE INDEX IF NOT EXISTS idx_bearing_configuration_sender_time
ON bearing_configuration(sender_id, effective_from_ns);
CREATE INDEX IF NOT EXISTS idx_bearing_configuration_subject_time
ON bearing_configuration(device_id, bearing_id, effective_from_ns);
CREATE TABLE IF NOT EXISTS final_diagnosis_summary (
    review_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    backend TEXT NOT NULL,
    model_name TEXT NOT NULL,
    summary_json TEXT,
    error_code TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL,
    CHECK (summary_json IS NULL OR json_valid(summary_json))
);
CREATE TABLE IF NOT EXISTS device_arbitration_record (
    arbitration_id TEXT PRIMARY KEY,
    conflict_id TEXT NOT NULL UNIQUE,
    scenario_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    final_action TEXT,
    confidence REAL,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    created_at_ns INTEGER NOT NULL,
    CHECK (json_valid(request_json)),
    CHECK (result_json IS NULL OR json_valid(result_json))
);
CREATE TABLE IF NOT EXISTS global_analysis_result (
    analysis_id TEXT PRIMARY KEY,
    scenario_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    task_count INTEGER NOT NULL,
    health_trend TEXT,
    normal_rate REAL,
    warning_rate REAL,
    abnormal_rate REAL,
    reviewed_packet_count INTEGER,
    edge_cloud_agreement_rate REAL,
    cloud_correction_rate REAL,
    conflict_rate REAL,
    arbitration_success_rate REAL,
    result_json TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    CHECK (json_valid(result_json))
);
CREATE INDEX IF NOT EXISTS idx_global_analysis_subject_time
ON global_analysis_result(scenario_type, subject_id, created_at_ns);
""" + MODEL_UPDATE_TASK_DDL + """
CREATE TABLE IF NOT EXISTS packet_source_mapping (
    packet_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    bearing_id TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_bearing_code TEXT NOT NULL,
    start_index INTEGER NOT NULL CHECK (start_index >= 0),
    end_index INTEGER NOT NULL CHECK (end_index > start_index),
    window_index INTEGER NOT NULL CHECK (window_index >= 0),
    created_at_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packet_source_task ON packet_source_mapping(task_id, source_file);
CREATE TABLE IF NOT EXISTS label_confirmation (
    packet_id TEXT PRIMARY KEY,
    confirmed_label TEXT NOT NULL,
    label_source TEXT NOT NULL CHECK (label_source IN ('dataset_ground_truth', 'human_confirmed', 'cloud_reference')),
    confirmed_at_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_update_dataset_manifest (
    dataset_id TEXT PRIMARY KEY,
    update_id TEXT NOT NULL UNIQUE,
    baseline_version TEXT NOT NULL,
    feature_pipeline_version TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    FOREIGN KEY (update_id) REFERENCES model_update_task(update_id),
    CHECK (json_valid(manifest_json))
);
CREATE TABLE IF NOT EXISTS bearing_task_result (
    device_id TEXT NOT NULL, task_id TEXT NOT NULL, bearing_id TEXT NOT NULL,
    edge_state TEXT NOT NULL, edge_confidence REAL NOT NULL, cloud_reviewed INTEGER NOT NULL,
    cloud_state TEXT, cloud_confidence REAL, bearing_state TEXT NOT NULL, result_source TEXT NOT NULL,
    packet_count INTEGER NOT NULL, source_packet_manifest TEXT NOT NULL CHECK (json_valid(source_packet_manifest)),
    model_version TEXT, completed_at_ns INTEGER NOT NULL, result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    PRIMARY KEY (device_id, task_id, bearing_id)
);
CREATE TABLE IF NOT EXISTS device_task_result (
    device_id TEXT NOT NULL, task_id TEXT NOT NULL, final_state TEXT NOT NULL, confidence REAL NOT NULL,
    has_conflict INTEGER NOT NULL, arbitration_id TEXT, summary TEXT, completed_at_ns INTEGER NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)), PRIMARY KEY (device_id, task_id)
);
CREATE TABLE IF NOT EXISTS arbitration_summary (
    arbitration_id TEXT PRIMARY KEY, status TEXT NOT NULL, summary TEXT, maintenance_advice TEXT,
    error_code TEXT, created_at_ns INTEGER NOT NULL,
    FOREIGN KEY (arbitration_id) REFERENCES device_arbitration_record(arbitration_id)
);
CREATE TABLE IF NOT EXISTS cloud_moment_review_record (
    review_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    device_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    bearing_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    decision_round_id TEXT NOT NULL,
    diagnosis_window_id TEXT NOT NULL,
    window_start_sequence INTEGER,
    window_end_sequence INTEGER,
    window_start_ns INTEGER,
    window_end_ns INTEGER,
    bearing_state TEXT NOT NULL,
    confidence REAL,
    data_quality_score REAL,
    risk_level TEXT,
    action_grade INTEGER,
    recommended_action TEXT,
    model_version TEXT,
    created_at_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moment_review_device_task
ON cloud_moment_review_record(device_id, task_id, created_at_ns);
"""
