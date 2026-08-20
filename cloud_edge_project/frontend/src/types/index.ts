// Service health
export interface ServiceHealth {
  service: string;
  node_id: string;
  status: string;
  port: number;
  model_backend?: string;
  model_version?: string;
  model_deployment_status?: string;
  ready?: boolean;
  mqtt_connected?: boolean;
  toxiproxy_available?: boolean;
  scheduler_reporter_healthy?: boolean;
  link_count?: number;
  available_link_count?: number;
  last_tick?: number;
}

// Dashboard metrics
export interface DashboardMetrics {
  total_packets: number;
  success_rate: number;
  avg_latency_ms: number;
  avg_total_latency_ms: number;
  p95_latency_ms: number;
  cloud_call_ratio: number;
  edge_only_ratio: number;
  weak_network_availability: number;
  conflict_rate: number;
  conflict_resolve_rate: number;
  fallback_edge_ratio: number;
  abnormal_ratio: number;
}

// Task trace (log format)
export interface TaskTrace {
  task_id: string;
  scenario: string;
  source_node: string;
  route: string;
  label?: string;
  confidence?: number;
  total_latency_ms: number;
  success: boolean;
  timestamp: string;
}

// Packet trace (log format)
export interface PacketTrace {
  packet_id: string;
  device_id: string;
  route: string;
  final_label?: string;
  final_confidence?: number;
  risk_level?: string;
  total_latency_ms: number;
  success: boolean;
  log_timestamp: string;
}

// Unified task item
export interface UnifiedTask {
  id: string;
  source: string;
  route: string;
  label?: string;
  confidence?: number;
  risk_level?: string;
  total_latency_ms: number;
  success: boolean;
  timestamp: string;
  scenario?: string;
  recommendation?: string;
  recommendation_source: 'backend' | 'frontend_rule' | 'none';
}

// Network link
export interface NetworkLink {
  link_id: string;
  link_type: string;
  sender_id: string | null;
  edge_id: string | null;
  protocol: string;
  proxy_name: string;
  listen: string;
  advertised_host: string;
  advertised_port: number;
  upstream: string;
  current_state: string;
  previous_state: string;
  state_since_ns: number;
  applied_state_since_ns: number | null;
  seed: number;
  desired_parameters: NetworkParameters | null;
  applied_parameters: NetworkParameters | null;
  link_reliability_score: number;
  score_components: Record<string, number>;
  available: boolean;
  last_apply_success: boolean;
  last_apply_timestamp_ns: number | null;
  consecutive_apply_failures: number;
  error: string | null;
  report_enabled: boolean;
  latency_ms: number | null;
  jitter_ms: number | null;
  bandwidth_kbps: number | null;
  packet_loss_percent: number | null;
}

export interface NetworkParameters {
  state: string;
  latency_ms: number | null;
  jitter_ms: number | null;
  bandwidth_kbps: number | null;
  packet_loss_percent: number;
  disconnect_mode: string;
  packet_loss_applied: boolean;
}

export interface NetworkRuntime {
  experiment_id: string;
  mode: string;
  tick: number;
  generated_at_ns: number;
  update_interval_seconds: number;
  link_count: number;
  available_link_count: number;
}

export interface NetworkHealth {
  status: string;
  toxiproxy_available: boolean;
  scheduler_reporter_healthy: boolean;
  link_count: number;
  available_link_count: number;
  last_tick: number;
}

// Edge node status
export interface EdgeNodeStatus {
  edge_node_id: string;
  reported_at_ns: number;
  resources: EdgeResources;
  models: EdgeModel[];
  network_to_scheduler?: EdgeNetworkInfo;
  last_task_activity_ns: number;
}

export interface EdgeResources {
  logical_cpu_count: number;
  cpu_utilization_percent: number;
  memory_available_mb: number;
  gpu_available: boolean;
  npu_available: boolean;
  queue_length: number;
}

export interface EdgeModel {
  model_version: string;
  load_status: 'LOADING' | 'LOADED' | 'UNLOADED' | 'ERROR';
}

export interface EdgeNetworkInfo {
  measured_at_ns: number;
  available_uplink_mbps_estimate: number;
  rtt_ms_avg: number;
  rtt_ms_p95: number;
  loss_rate: number;
}

export type NodeOnlineStatus = 'online' | 'stale' | 'offline' | 'unknown';

// Cloud review
export interface CloudReview {
  review_id: string;
  review_type: string;
  status: string;
  edge_conclusion?: string;
  cloud_conclusion?: string;
  edge_confidence?: number;
  cloud_confidence?: number;
  edge_risk_level?: string;
  cloud_risk_level?: string;
  dispute?: boolean;
  created_at?: string;
  updated_at?: string;
}

// Model update
export interface ModelUpdate {
  update_id: string;
  status: string;
  model_version?: string;
  created_at?: string;
  dataset_packet_count?: number;
  training_result?: {
    accuracy?: number;
    f1_score?: number;
    precision?: number;
    recall?: number;
  };
  validation_result?: {
    accuracy?: number;
    f1_score?: number;
  };
  distribution_result?: {
    success_count?: number;
    fail_count?: number;
  };
  rollback_reason?: string;
  confirmed_by?: string;
}

// API error
export interface ApiError {
  error_code: string;
  message?: string;
  detail?: string;
}

// Generic API response
export interface ApiResponse<T> {
  data?: T;
  error?: ApiError;
  status: number;
  ok: boolean;
}

// Service health map
export type ServiceName = 'log' | 'cloud' | 'edge' | 'scheduler' | 'network';
export type ServiceStatusMap = Record<ServiceName, 'online' | 'degraded' | 'offline' | 'unknown'>;

// Polling config
export interface PollingConfig {
  intervalMs: number;
  staleThresholdMs: number;
  offlineThresholdMs: number;
  edgeNodeIds: string[];
  enableMock: boolean;
  enableCloudActions: boolean;
}