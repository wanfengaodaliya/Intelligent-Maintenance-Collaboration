import type { ApiResponse, DashboardMetrics } from '../types';
import { createClient } from './client';

export function fetchDashboardMetrics(logApiBaseUrl: string): Promise<ApiResponse<DashboardMetrics>> {
  return createClient(logApiBaseUrl).get<DashboardMetrics>('/dashboard/metrics');
}