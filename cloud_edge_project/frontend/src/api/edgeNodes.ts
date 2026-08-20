import type { ApiResponse, EdgeNodeStatus } from '../types';
import { createClient } from './client';

export function fetchEdgeNodeStatus(cloudApiBaseUrl: string, nodeId: string): Promise<ApiResponse<EdgeNodeStatus>> {
  return createClient(cloudApiBaseUrl).get<EdgeNodeStatus>(`/cloud/edge-status/${encodeURIComponent(nodeId)}`);
}