// Log service API
export interface LogMetricsResponse {
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

export interface LogTasksResponse {
  tasks: (import('./index').TaskTrace | import('./index').PacketTrace)[];
}

// Cloud API
export interface EdgeStatusResponse {
  edge_node_id: string;
  reported_at_ns: number;
  resources: {
    logical_cpu_count: number;
    cpu_utilization_percent: number;
    memory_available_mb: number;
    gpu_available: boolean;
    npu_available: boolean;
    queue_length: number;
  };
  models: { model_version: string; load_status: string }[];
  network_to_scheduler?: {
    measured_at_ns: number;
    available_uplink_mbps_estimate: number;
    rtt_ms_avg: number;
    rtt_ms_p95: number;
    loss_rate: number;
  };
  last_task_activity_ns: number;
}

export interface ModelUpdateResponse {
  update_id: string;
  status: string;
  model_version?: string;
  created_at?: string;
  dataset_packet_count?: number;
  training_result?: Record<string, unknown>;
  validation_result?: Record<string, unknown>;
  distribution_result?: Record<string, unknown>;
  rollback_reason?: string;
  confirmed_by?: string;
  [key: string]: unknown;
}

// App config
export interface AppConfig {
  logApiBaseUrl: string;
  cloudApiBaseUrl: string;
  edgeApiBaseUrl: string;
  schedulerApiBaseUrl: string;
  networkApiBaseUrl: string;
  edgeNodeIds: string[];
  pollIntervalMs: number;
  staleThresholdMs: number;
  offlineThresholdMs: number;
  enableMock: boolean;
  enableCloudActions: boolean;
}