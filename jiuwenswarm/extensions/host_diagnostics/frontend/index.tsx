/**
 * Host Diagnostics - workspace page.
 *
 * Contributed as a `bundled` application plugin page: the core frontend
 * discovers this module through a Vite `import.meta.glob` over the
 * `extensions/<name>/frontend/index.tsx` pattern and resolves the default
 * export by `plugin_id`.
 *
 * The panel is strictly read-only. It talks to its own backend through the
 * shared WebSocket RPC client using the `host.diagnostics.*` method
 * namespace, so it cannot interfere with chat traffic.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { webRequest } from '../../../channels/web/frontend/src/services/webClient';

import { HostDiagnosticsPanel } from './HostDiagnosticsPanel';
import './hostDiagnostics.css';

/** Must match `plugin_id` in extension.py and `id` in extension.yaml. */
export const applicationPluginId = 'host-diagnostics';

interface DiagnosticsPayload {
  [key: string]: unknown;
}

function HostDiagnosticsPage() {
  const [summary, setSummary] = useState<DiagnosticsPayload | null>(null);
  const [plugins, setPlugins] = useState<DiagnosticsPayload | null>(null);
  const [paths, setPaths] = useState<DiagnosticsPayload | null>(null);
  const [includeEnv, setIncludeEnv] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(
    async (withEnv: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const [summaryResult, pluginsResult, pathsResult] = await Promise.all([
          webRequest<DiagnosticsPayload>('host.diagnostics.summary', {
            include_env: withEnv,
          }),
          webRequest<DiagnosticsPayload>('host.diagnostics.plugins', {}),
          webRequest<DiagnosticsPayload>('host.diagnostics.paths', {}),
        ]);
        setSummary(summaryResult);
        setPlugins(pluginsResult);
        setPaths(pathsResult);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void refresh(false);
  }, [refresh]);

  const onToggleEnv = useCallback(
    (next: boolean) => {
      setIncludeEnv(next);
      void refresh(next);
    },
    [refresh],
  );

  const sections = useMemo(
    () => [
      { key: 'summary', payload: summary },
      { key: 'plugins', payload: plugins },
      { key: 'paths', payload: paths },
    ],
    [summary, plugins, paths],
  );

  return (
    <HostDiagnosticsPanel
      sections={sections}
      loading={loading}
      error={error}
      includeEnv={includeEnv}
      onToggleEnv={onToggleEnv}
      onRefresh={() => void refresh(includeEnv)}
    />
  );
}

export default HostDiagnosticsPage;
