import { useQuery } from '@tanstack/react-query';
import { fetchDashboardMetrics } from '../api/metrics';
import { normalizeMetrics } from '../adapters/metricsAdapter';
import { useAppConfig } from './useAppConfig';
import type { DashboardMetrics } from '../types';
import type { ApiError } from '../types';

export function useMetrics() {
  const config = useAppConfig();

  const query = useQuery<DashboardMetrics, ApiError>({
    queryKey: ['dashboard-metrics', config.logApiBaseUrl],
    queryFn: async () => {
      const response = await fetchDashboardMetrics(config.logApiBaseUrl);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取指标失败' };
      }
      return normalizeMetrics(response.data as unknown as Record<string, unknown>);
    },
    enabled: !config.enableMock,
    refetchInterval: config.pollIntervalMs,
    retry: 2,
    staleTime: 5000,
  });

  return query;
}