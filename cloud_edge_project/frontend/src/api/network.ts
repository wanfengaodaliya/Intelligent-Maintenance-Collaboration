import type { ApiResponse, NetworkLink, NetworkRuntime, NetworkHealth } from '../types';
import { createClient } from './client';

export function fetchNetworkLinks(networkApiBaseUrl: string): Promise<ApiResponse<NetworkLink[]>> {
  return createClient(networkApiBaseUrl).get<NetworkLink[]>('/api/v1/network/links');
}

export function fetchNetworkLink(networkApiBaseUrl: string, linkId: string): Promise<ApiResponse<NetworkLink>> {
  return createClient(networkApiBaseUrl).get<NetworkLink>(`/api/v1/network/links/${encodeURIComponent(linkId)}`);
}

export function fetchNetworkRuntime(networkApiBaseUrl: string): Promise<ApiResponse<NetworkRuntime>> {
  return createClient(networkApiBaseUrl).get<NetworkRuntime>('/api/v1/network/runtime');
}

export function fetchNetworkHealth(networkApiBaseUrl: string): Promise<ApiResponse<NetworkHealth>> {
  return createClient(networkApiBaseUrl).get<NetworkHealth>('/health');
}