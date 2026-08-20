import type { ApiResponse } from '../types';
import type { TaskTrace, PacketTrace } from '../types';
import { createClient } from './client';

export interface TasksResponse {
  tasks: (TaskTrace | PacketTrace)[];
}

export function fetchTasks(logApiBaseUrl: string, limit: number = 50): Promise<ApiResponse<TasksResponse>> {
  return createClient(logApiBaseUrl).get<TasksResponse>(`/dashboard/tasks?limit=${limit}`);
}