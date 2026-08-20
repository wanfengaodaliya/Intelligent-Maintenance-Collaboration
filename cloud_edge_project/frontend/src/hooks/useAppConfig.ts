import type { AppConfig } from '../types/api';

export function useAppConfig(): AppConfig {
  return {
    logApiBaseUrl: import.meta.env.VITE_LOG_API_BASE_URL || 'http://127.0.0.1:8006',
    cloudApiBaseUrl: import.meta.env.VITE_CLOUD_API_BASE_URL || 'http://127.0.0.1:8004',
    edgeApiBaseUrl: import.meta.env.VITE_EDGE_API_BASE_URL || 'http://127.0.0.1:8001',
    schedulerApiBaseUrl: import.meta.env.VITE_SCHEDULER_API_BASE_URL || 'http://127.0.0.1:8003',
    networkApiBaseUrl: import.meta.env.VITE_NETWORK_API_BASE_URL || 'http://127.0.0.1:8090',
    edgeNodeIds: (import.meta.env.VITE_EDGE_NODE_IDS || 'edge_1').split(',').map((s: string) => s.trim()),
    pollIntervalMs: parseInt(import.meta.env.VITE_POLL_INTERVAL_MS || '5000', 10),
    staleThresholdMs: parseInt(import.meta.env.VITE_STALE_THRESHOLD_MS || '15000', 10),
    offlineThresholdMs: parseInt(import.meta.env.VITE_OFFLINE_THRESHOLD_MS || '30000', 10),
    enableMock: import.meta.env.VITE_ENABLE_MOCK === 'true',
    enableCloudActions: import.meta.env.VITE_ENABLE_CLOUD_ACTIONS === 'true',
  };
}