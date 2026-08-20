import type { ApiError, ApiResponse } from '../types';

const REQUEST_TIMEOUT_MS = 10000;
const MAX_RETRIES = 2;

class ApiClient {
  private baseUrl: string;
  private abortController: AbortController | null = null;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  private buildUrl(path: string): string {
    return `${this.baseUrl}${path.startsWith('/') ? path : `/${path}`}`;
  }

  async get<T>(path: string, options?: { signal?: AbortSignal; retries?: number }): Promise<ApiResponse<T>> {
    const url = this.buildUrl(path);
    const retries = options?.retries ?? MAX_RETRIES;

    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
        const signal = options?.signal ? combineSignals(options.signal, controller.signal) : controller.signal;

        const response = await fetch(url, {
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          signal,
        });

        clearTimeout(timeoutId);

        if (!response.ok) {
          let error: ApiError;
          try {
            error = await response.json() as ApiError;
          } catch {
            error = { error_code: `HTTP_${response.status}`, message: response.statusText };
          }
          return { data: undefined, error, status: response.status, ok: false };
        }

        const data = await response.json() as T;
        return { data, error: undefined, status: response.status, ok: true };
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          return { data: undefined, error: { error_code: 'REQUEST_TIMEOUT', message: '请求超时' }, status: 0, ok: false };
        }
        if (err instanceof TypeError && (err as Error).message.includes('fetch')) {
          if (attempt < retries) {
            await new Promise(r => setTimeout(r, 500 * (attempt + 1)));
            continue;
          }
          return { data: undefined, error: { error_code: 'NETWORK_ERROR', message: '网络请求失败，请检查服务是否运行' }, status: 0, ok: false };
        }
        if (attempt < retries) {
          await new Promise(r => setTimeout(r, 500 * (attempt + 1)));
          continue;
        }
        return { data: undefined, error: { error_code: 'UNKNOWN_ERROR', message: (err as Error).message }, status: 0, ok: false };
      }
    }

    return { data: undefined, error: { error_code: 'MAX_RETRIES', message: '已达到最大重试次数' }, status: 0, ok: false };
  }

  async post<T>(path: string, body?: unknown): Promise<ApiResponse<T>> {
    const url = this.buildUrl(path);
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        let error: ApiError;
        try {
          error = await response.json() as ApiError;
        } catch {
          error = { error_code: `HTTP_${response.status}` };
        }
        return { data: undefined, error, status: response.status, ok: false };
      }

      const data = await response.json() as T;
      return { data, error: undefined, status: response.status, ok: true };
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return { data: undefined, error: { error_code: 'REQUEST_TIMEOUT', message: '请求超时' }, status: 0, ok: false };
      }
      return { data: undefined, error: { error_code: 'NETWORK_ERROR', message: (err as Error).message }, status: 0, ok: false };
    }
  }

  cancel(): void {
    this.abortController?.abort();
  }
}

function combineSignals(s1: AbortSignal, s2: AbortSignal): AbortSignal {
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  s1.addEventListener('abort', onAbort);
  s2.addEventListener('abort', onAbort);
  if (s1.aborted || s2.aborted) controller.abort();
  return controller.signal;
}

export function createClient(baseUrl: string): ApiClient {
  return new ApiClient(baseUrl);
}

export default ApiClient;