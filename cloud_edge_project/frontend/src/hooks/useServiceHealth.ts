import { useQuery } from '@tanstack/react-query';
import { checkAllServices } from '../api/health';
import { useAppConfig } from './useAppConfig';
import type { ServiceStatusMap, ApiError } from '../types';

export function useServiceHealth() {
  const config = useAppConfig();

  const serviceUrls = {
    log: config.logApiBaseUrl,
    cloud: config.cloudApiBaseUrl,
    edge: config.edgeApiBaseUrl,
    scheduler: config.schedulerApiBaseUrl,
    network: config.networkApiBaseUrl,
  };

  return useQuery<ServiceStatusMap, ApiError>({
    queryKey: ['service-health', ...Object.values(serviceUrls)],
    queryFn: async () => {
      return checkAllServices(serviceUrls);
    },
    refetchInterval: config.pollIntervalMs,
    retry: 1,
    staleTime: 3000,
  });
}