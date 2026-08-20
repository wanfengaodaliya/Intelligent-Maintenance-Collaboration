import { useQuery } from '@tanstack/react-query';
import { fetchEdgeNodeStatus } from '../api/edgeNodes';
import { checkHealth } from '../api/health';
import { useAppConfig } from './useAppConfig';
import { getNodeOnlineStatus } from '../utils/time';
import type { EdgeNodeStatus, ApiError, ServiceHealth } from '../types';

export interface EdgeNodeWithStatus extends EdgeNodeStatus {
  onlineStatus: 'online' | 'stale' | 'offline' | 'unknown';
  edgeHealth: ServiceHealth | null;
}

export function useEdgeNodes() {
  const config = useAppConfig();

  return useQuery<EdgeNodeWithStatus[], ApiError>({
    queryKey: ['edge-nodes', config.cloudApiBaseUrl, config.edgeApiBaseUrl, config.edgeNodeIds.join(',')],
    queryFn: async () => {
      const nodeIds = config.edgeNodeIds;
      const results = await Promise.allSettled(
        nodeIds.map(async (nodeId) => {
          const [statusRes, healthRes] = await Promise.allSettled([
            fetchEdgeNodeStatus(config.cloudApiBaseUrl, nodeId),
            checkHealth(config.edgeApiBaseUrl),
          ]);

          const status = statusRes.status === 'fulfilled' && statusRes.value.ok && statusRes.value.data
            ? statusRes.value.data
            : null;

          const health = healthRes.status === 'fulfilled' && healthRes.value.ok && healthRes.value.data
            ? healthRes.value.data
            : null;

          const nowMs = Date.now();
          const onlineStatus = status
            ? getNodeOnlineStatus(status.reported_at_ns, nowMs, config.staleThresholdMs, config.offlineThresholdMs)
            : 'unknown';

          return {
            edge_node_id: nodeId,
            reported_at_ns: status?.reported_at_ns || 0,
            resources: status?.resources || {
              logical_cpu_count: 0,
              cpu_utilization_percent: 0,
              memory_available_mb: 0,
              gpu_available: false,
              npu_available: false,
              queue_length: 0,
            },
            models: status?.models || [],
            network_to_scheduler: status?.network_to_scheduler,
            last_task_activity_ns: status?.last_task_activity_ns || 0,
            onlineStatus,
            edgeHealth: health,
          } as EdgeNodeWithStatus;
        })
      );

      return results
        .filter((r): r is PromiseFulfilledResult<EdgeNodeWithStatus> => r.status === 'fulfilled')
        .map(r => r.value);
    },
    enabled: !config.enableMock,
    refetchInterval: config.pollIntervalMs,
    retry: 2,
    staleTime: 5000,
  });
}