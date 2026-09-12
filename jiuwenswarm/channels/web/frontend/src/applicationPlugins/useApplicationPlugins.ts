import { useCallback, useEffect, useState } from 'react';

import { fetchApplicationPlugins } from './manifest';
import type { ApplicationPluginContribution } from './types';

export interface ApplicationPluginsState {
  plugins: ApplicationPluginContribution[];
  loading: boolean;
  error: string;
  refresh: () => Promise<void>;
}

export function useApplicationPlugins(isGatewayConnected: boolean): ApplicationPluginsState {
  const [plugins, setPlugins] = useState<ApplicationPluginContribution[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    if (!isGatewayConnected) return;
    setLoading(true);
    setError('');
    try {
      setPlugins(await fetchApplicationPlugins());
    } catch (refreshError) {
      console.warn('Application plugin discovery failed:', refreshError);
      setError(refreshError instanceof Error ? refreshError.message : 'Application plugin discovery failed');
    } finally {
      setLoading(false);
    }
  }, [isGatewayConnected]);

  useEffect(() => {
    if (!isGatewayConnected) {
      setPlugins([]);
      setLoading(false);
      setError('');
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError('');
    void fetchApplicationPlugins(controller.signal)
      .then(nextPlugins => {
        setPlugins(nextPlugins);
        setError('');
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        console.warn('Application plugin discovery failed:', error);
        setError(error instanceof Error ? error.message : 'Application plugin discovery failed');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [isGatewayConnected]);

  return { plugins, loading, error, refresh };
}
