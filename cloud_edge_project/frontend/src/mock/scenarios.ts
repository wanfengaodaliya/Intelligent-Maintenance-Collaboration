import { mockMetrics, mockTasks, mockNetworkLinks, mockEdgeNodeStatus, mockEdgeNode2Status, mockModelUpdate, mockServiceHealths } from './data';
import type { DashboardMetrics, UnifiedTask, NetworkLink, EdgeNodeStatus, ServiceHealth, ModelUpdate, ServiceStatusMap } from '../types';

let mockPollCount = 0;

export function resetMockPollCount(): void {
  mockPollCount = 0;
}

export function getMockMetrics(): DashboardMetrics {
  mockPollCount++;
  const jitter = Math.sin(mockPollCount * 0.1) * 0.01;
  return {
    ...mockMetrics,
    total_packets: mockMetrics.total_packets + mockPollCount,
    avg_latency_ms: mockMetrics.avg_latency_ms + jitter * 50,
    avg_total_latency_ms: mockMetrics.avg_total_latency_ms + jitter * 50,
  };
}

export function getMockTasks(): UnifiedTask[] {
  return mockTasks;
}

export function getMockNetworkLinks(): NetworkLink[] {
  const nowNs = Date.now() * 1_000_000;
  return mockNetworkLinks.map(link => ({
    ...link,
    latency_ms: link.latency_ms! + Math.sin(mockPollCount * 0.05) * 2,
    jitter_ms: link.jitter_ms! + Math.sin(mockPollCount * 0.05) * 0.5,
    state_since_ns: nowNs - 3600_000_000_000,
  }));
}

export function getMockEdgeNodeStatus(): EdgeNodeStatus[] {
  const nowNs = Date.now() * 1_000_000;
  const edge1 = {
    ...mockEdgeNodeStatus,
    reported_at_ns: nowNs - 2000_000_000,
    resources: {
      ...mockEdgeNodeStatus.resources,
      cpu_utilization_percent: 45 + Math.sin(mockPollCount * 0.1) * 5,
      memory_available_mb: 4000 + Math.sin(mockPollCount * 0.05) * 200,
      queue_length: Math.max(0, 3 + Math.floor(Math.sin(mockPollCount * 0.05))),
    },
  };

  return [edge1, mockEdgeNode2Status];
}

export function getMockModelUpdate(): ModelUpdate {
  return mockModelUpdate;
}

export function getMockServiceHealths(): Record<string, ServiceHealth> {
  return mockServiceHealths;
}

export function getMockServiceStatus(): ServiceStatusMap {
  return {
    log: 'online',
    cloud: 'online',
    edge: 'online',
    scheduler: 'online',
    network: 'online',
  };
}