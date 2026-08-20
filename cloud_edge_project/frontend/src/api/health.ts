import type { ServiceHealth, ApiResponse, ServiceName, ServiceStatusMap } from '../types';
import { createClient } from './client';

const clients = new Map<string, ReturnType<typeof createClient>>();

function getClient(baseUrl: string): ReturnType<typeof createClient> {
  if (!clients.has(baseUrl)) {
    clients.set(baseUrl, createClient(baseUrl));
  }
  return clients.get(baseUrl)!;
}

export async function checkHealth(baseUrl: string): Promise<ApiResponse<ServiceHealth>> {
  return getClient(baseUrl).get<ServiceHealth>('/health');
}

export async function checkAllServices(
  urls: Record<string, string>
): Promise<ServiceStatusMap> {
  const entries = Object.entries(urls);
  const results = await Promise.allSettled(
    entries.map(([name, url]) => checkHealth(url).then(r => ({ name, result: r })))
  );

  const statusMap: ServiceStatusMap = {
    log: 'unknown',
    cloud: 'unknown',
    edge: 'unknown',
    scheduler: 'unknown',
    network: 'unknown',
  };

  for (const entry of results) {
    if (entry.status === 'fulfilled') {
      const { name, result } = entry.value;
      if (result.ok && result.data?.status === 'ok') {
        statusMap[name as ServiceName] = 'online';
      } else if (result.ok) {
        statusMap[name as ServiceName] = 'degraded';
      } else {
        statusMap[name as ServiceName] = 'offline';
      }
    } else {
      const name = entries[results.indexOf(entry)]?.[0] as ServiceName;
      if (name) statusMap[name] = 'offline';
    }
  }

  return statusMap;
}