import type { DashboardMetrics } from '../types';

export function normalizeMetrics(raw: Record<string, unknown>): DashboardMetrics {
  return {
    total_packets: (raw.total_packets as number) || 0,
    success_rate: (raw.success_rate as number) || 0,
    avg_latency_ms: (raw.avg_latency_ms as number) ?? (raw.avg_total_latency_ms as number) ?? 0,
    avg_total_latency_ms: (raw.avg_total_latency_ms as number) ?? (raw.avg_latency_ms as number) ?? 0,
    p95_latency_ms: (raw.p95_latency_ms as number) || 0,
    cloud_call_ratio: (raw.cloud_call_ratio as number) || 0,
    edge_only_ratio: (raw.edge_only_ratio as number) || 0,
    weak_network_availability: (raw.weak_network_availability as number) || 0,
    conflict_rate: (raw.conflict_rate as number) || 0,
    conflict_resolve_rate: (raw.conflict_resolve_rate as number) || 0,
    fallback_edge_ratio: (raw.fallback_edge_ratio as number) || 0,
    abnormal_ratio: (raw.abnormal_ratio as number) || 0,
  };
}