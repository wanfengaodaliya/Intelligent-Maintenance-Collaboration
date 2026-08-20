import { useEffect, useRef, useCallback, useState } from 'react';

interface UsePollingOptions {
  enabled: boolean;
  intervalMs: number;
  onPoll: () => Promise<void>;
}

export function usePolling({ enabled, intervalMs, onPoll }: UsePollingOptions) {
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [isPolling, setIsPolling] = useState(false);

  const start = useCallback(() => {
    stop();
    setIsPolling(true);
    intervalRef.current = setInterval(() => {
      onPoll().catch(() => {});
    }, intervalMs);
  }, [intervalMs, onPoll]);

  const stop = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setIsPolling(false);
  }, []);

  useEffect(() => {
    if (enabled) {
      start();
    } else {
      stop();
    }
    return stop;
  }, [enabled, start, stop]);

  return { isPolling, start, stop };
}