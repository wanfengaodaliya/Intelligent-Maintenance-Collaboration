import type { ApiResponse, ModelUpdate, CloudReview } from '../types';
import { createClient } from './client';

export function fetchPacketReview(cloudApiBaseUrl: string, reviewId: string): Promise<ApiResponse<CloudReview>> {
  return createClient(cloudApiBaseUrl).get<CloudReview>(`/cloud/packet-reviews/${encodeURIComponent(reviewId)}`);
}

export function fetchBearingWindowReview(cloudApiBaseUrl: string, reviewId: string): Promise<ApiResponse<CloudReview>> {
  return createClient(cloudApiBaseUrl).get<CloudReview>(`/cloud/bearing-window-reviews/${encodeURIComponent(reviewId)}`);
}

export function fetchDeviceReview(cloudApiBaseUrl: string, reviewId: string): Promise<ApiResponse<CloudReview>> {
  return createClient(cloudApiBaseUrl).get<CloudReview>(`/cloud/device-reviews/${encodeURIComponent(reviewId)}`);
}

export function fetchReviewSummary(cloudApiBaseUrl: string, reviewId: string): Promise<ApiResponse<Record<string, unknown>>> {
  return createClient(cloudApiBaseUrl).get<Record<string, unknown>>(`/cloud/reviews/${encodeURIComponent(reviewId)}/summary`);
}

export function fetchModelUpdate(cloudApiBaseUrl: string, updateId: string): Promise<ApiResponse<ModelUpdate>> {
  return createClient(cloudApiBaseUrl).get<ModelUpdate>(`/cloud/model-update/${encodeURIComponent(updateId)}`);
}

export function approveModelUpdate(cloudApiBaseUrl: string, updateId: string): Promise<ApiResponse<Record<string, unknown>>> {
  return createClient(cloudApiBaseUrl).post<Record<string, unknown>>(`/cloud/model-update/${encodeURIComponent(updateId)}/approve`);
}

export function rejectModelUpdate(cloudApiBaseUrl: string, updateId: string): Promise<ApiResponse<Record<string, unknown>>> {
  return createClient(cloudApiBaseUrl).post<Record<string, unknown>>(`/cloud/model-update/${encodeURIComponent(updateId)}/reject`);
}

export function handoffModelUpdateDistribution(cloudApiBaseUrl: string, updateId: string): Promise<ApiResponse<Record<string, unknown>>> {
  return createClient(cloudApiBaseUrl).post<Record<string, unknown>>(`/cloud/model-update/${encodeURIComponent(updateId)}/handoff-distribution`);
}

export function requestModelUpdateRollback(cloudApiBaseUrl: string, updateId: string): Promise<ApiResponse<Record<string, unknown>>> {
  return createClient(cloudApiBaseUrl).post<Record<string, unknown>>(`/cloud/model-update/${encodeURIComponent(updateId)}/request-rollback`);
}