import { useQuery } from '@tanstack/react-query';
import { fetchTasks } from '../api/tasks';
import { unifyTask } from '../adapters/taskAdapter';
import { useAppConfig } from './useAppConfig';
import type { UnifiedTask, ApiError } from '../types';

export function useTasks(limit: number = 50) {
  const config = useAppConfig();

  const query = useQuery<UnifiedTask[], ApiError>({
    queryKey: ['tasks', config.logApiBaseUrl, limit],
    queryFn: async () => {
      const response = await fetchTasks(config.logApiBaseUrl, limit);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取任务列表失败' };
      }
      return response.data.tasks.map(unifyTask);
    },
    enabled: !config.enableMock,
    refetchInterval: config.pollIntervalMs,
    retry: 2,
    staleTime: 5000,
  });

  return query;
}