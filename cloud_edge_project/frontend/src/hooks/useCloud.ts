import { useQuery, useMutation } from '@tanstack/react-query';
import {
  fetchPacketReview,
  fetchBearingWindowReview,
  fetchDeviceReview,
  fetchReviewSummary,
  fetchModelUpdate,
  approveModelUpdate,
  rejectModelUpdate,
  handoffModelUpdateDistribution,
  requestModelUpdateRollback,
} from '../api/cloud';
import { useAppConfig } from './useAppConfig';
import type { CloudReview, ModelUpdate, ApiError } from '../types';

export function useReviewQuery(
  type: 'packet' | 'bearing_window' | 'device',
  reviewId: string | null
) {
  const config = useAppConfig();

  const fetcher = type === 'packet' ? fetchPacketReview
    : type === 'bearing_window' ? fetchBearingWindowReview
    : fetchDeviceReview;

  return useQuery<CloudReview, ApiError>({
    queryKey: ['cloud-review', type, reviewId, config.cloudApiBaseUrl],
    queryFn: async () => {
      if (!reviewId) throw { error_code: 'NO_ID', message: '请提供复核ID' } as ApiError;
      const response = await fetcher(config.cloudApiBaseUrl, reviewId);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取复核结果失败' };
      }
      return response.data;
    },
    enabled: !!reviewId && !config.enableMock,
    retry: 1,
  });
}

export function useReviewSummary(reviewId: string | null) {
  const config = useAppConfig();

  return useQuery<Record<string, unknown>, ApiError>({
    queryKey: ['review-summary', reviewId, config.cloudApiBaseUrl],
    queryFn: async () => {
      if (!reviewId) throw { error_code: 'NO_ID', message: '请提供复核ID' } as ApiError;
      const response = await fetchReviewSummary(config.cloudApiBaseUrl, reviewId);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取复核摘要失败' };
      }
      return response.data;
    },
    enabled: !!reviewId && !config.enableMock,
    retry: 1,
  });
}

export function useModelUpdate(updateId: string | null) {
  const config = useAppConfig();

  return useQuery<ModelUpdate, ApiError>({
    queryKey: ['model-update', updateId, config.cloudApiBaseUrl],
    queryFn: async () => {
      if (!updateId) throw { error_code: 'NO_ID', message: '请提供更新ID' } as ApiError;
      const response = await fetchModelUpdate(config.cloudApiBaseUrl, updateId);
      if (!response.ok || !response.data) {
        throw response.error || { error_code: 'UNKNOWN', message: '获取模型更新失败' };
      }
      return response.data;
    },
    enabled: !!updateId && !config.enableMock,
    retry: 1,
  });
}

export function useApproveModelUpdate() {
  const config = useAppConfig();
  return useMutation({
    mutationFn: async (updateId: string) => {
      const response = await approveModelUpdate(config.cloudApiBaseUrl, updateId);
      if (!response.ok) throw response.error || { error_code: 'APPROVE_FAILED' };
      return response.data;
    },
  });
}

export function useRejectModelUpdate() {
  const config = useAppConfig();
  return useMutation({
    mutationFn: async (updateId: string) => {
      const response = await rejectModelUpdate(config.cloudApiBaseUrl, updateId);
      if (!response.ok) throw response.error || { error_code: 'REJECT_FAILED' };
      return response.data;
    },
  });
}

export function useHandoffDistribution() {
  const config = useAppConfig();
  return useMutation({
    mutationFn: async (updateId: string) => {
      const response = await handoffModelUpdateDistribution(config.cloudApiBaseUrl, updateId);
      if (!response.ok) throw response.error || { error_code: 'HANDOFF_FAILED' };
      return response.data;
    },
  });
}

export function useRequestRollback() {
  const config = useAppConfig();
  return useMutation({
    mutationFn: async (updateId: string) => {
      const response = await requestModelUpdateRollback(config.cloudApiBaseUrl, updateId);
      if (!response.ok) throw response.error || { error_code: 'ROLLBACK_FAILED' };
      return response.data;
    },
  });
}