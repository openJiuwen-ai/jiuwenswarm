import { useCallback, useEffect, useRef, useState } from 'react';
import type { SettingsRequest } from '../../services/settingsContract';

export type ModelRefreshStatus = {
  status: 'pending' | 'applied' | 'failed';
  generation: number;
  detail?: string;
} | { status: 'unavailable' };

function parseRefreshStatus(payload: unknown): ModelRefreshStatus {
  if (!payload || typeof payload !== 'object') throw new Error('INVALID_MODEL_REFRESH_STATUS');
  const { status, generation, detail } = payload as Record<string, unknown>;
  if (
    typeof status !== 'string' || !['pending', 'applied', 'failed'].includes(status) ||
    typeof generation !== 'number' || !Number.isSafeInteger(generation) || generation < 0 ||
    (detail !== undefined && typeof detail !== 'string')
  ) {
    throw new Error('INVALID_MODEL_REFRESH_STATUS');
  }
  return { status: status as 'pending' | 'applied' | 'failed', generation, ...(detail ? { detail } : {}) };
}

export function useModelRefreshStatus(isConnected: boolean, request: SettingsRequest) {
  const [refreshStatus, setRefreshStatus] = useState<ModelRefreshStatus | null>(null);
  const checkRef = useRef<(() => void) | null>(null);
  const checkRefreshStatus = useCallback(() => checkRef.current?.(), []);

  useEffect(() => {
    setRefreshStatus(null);
    if (!isConnected) return;
    let controller: AbortController | null = null;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let timeoutTimer: ReturnType<typeof setTimeout> | undefined;

    const cancel = () => {
      controller?.abort();
      clearTimeout(pollTimer);
      clearTimeout(timeoutTimer);
    };

    const query = () => {
      cancel();
      const activeController = new AbortController();
      controller = activeController;
      const { signal } = activeController;
      // Also bound the wait locally: a transport can stall before its RPC timer starts.
      timeoutTimer = setTimeout(() => {
        activeController.abort();
        setRefreshStatus({ status: 'unavailable' });
      }, 5000);
      const options = { timeoutMs: 5000, signal };
      void Promise.resolve()
        .then(() => signal.aborted ? undefined : request('models.refresh_status', {}, options))
        .then((payload) => {
          if (signal.aborted) return;
          const next = parseRefreshStatus(payload);
          clearTimeout(timeoutTimer);
          setRefreshStatus(next);
          if (next.status === 'pending') pollTimer = setTimeout(query, 2000);
        })
        .catch(() => {
          if (signal.aborted) return;
          clearTimeout(timeoutTimer);
          setRefreshStatus({ status: 'unavailable' });
        });
    };

    checkRef.current = () => {
      setRefreshStatus(null);
      query();
    };
    query();
    return () => {
      checkRef.current = null;
      cancel();
    };
  }, [isConnected, request]);

  return { refreshStatus, checkRefreshStatus };
}
