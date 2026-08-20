import { useQuery } from '@tanstack/react-query';
import { fetchNetworkLinks, fetchNetworkRuntime, fetchNetworkHealth } from '../api/network';
import { useAppConfig } from './useAppConfig';
import type { NetworkLink, NetworkRuntime, NetworkHealth, ApiError } from '../types';

export function useNetworkLinks() {
  const config = useAppConfig();

  return useQuery<NetworkLink[], ApiError>({
    queryKey: ['network-links', config.networkApiBaseUrl],
    queryFn: async () => {
      const response = await fetchNetworkLinks(config.networkApiBaseUrl);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取网络链路失败' };
      }
      return response.data;
    },
    enabled: !config.enableMock,
    refetchInterval: config.pollIntervalMs,
    retry: 2,
    staleTime: 5000,
  });
}

export function useNetworkRuntime() {
  const config = useAppConfig();

  return useQuery<NetworkRuntime, ApiError>({
    queryKey: ['network-runtime', config.networkApiBaseUrl],
    queryFn: async () => {
      const response = await fetchNetworkRuntime(config.networkApiBaseUrl);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取网络运行时失败' };
      }
      return response.data;
    },
    enabled: !config.enableMock,
    refetchInterval: config.pollIntervalMs,
    retry: 2,
    staleTime: 5000,
  });
}

export function useNetworkHealth() {
  const config = useAppConfig();

  return useQuery<NetworkHealth, ApiError>({
    queryKey: ['network-health', config.networkApiBaseUrl],
    queryFn: async () => {
      const response = await fetchNetworkHealth(config.networkApiBaseUrl);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取网络健康状态失败' };
      }
      return response.data;
    },
    enabled: !config.enableMock,
    refetchInterval: config.pollIntervalMs,
    retry: 2,
    staleTime: 5000,
  });
}