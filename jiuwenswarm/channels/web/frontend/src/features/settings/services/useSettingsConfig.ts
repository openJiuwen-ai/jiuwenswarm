import { useCallback, useEffect, useRef, useState } from 'react';
import { setTeamScheduledDispatchEnabled } from '../../teamDispatch/featureConfig';
import { setTeamOrganizationUiEnabled } from '../../teamOrganization/featureConfig';
import { buildConfigSavePayload } from './settingsContract';
import { useSettingsServices } from './SettingsServicesProvider';

export function useSettingsConfig() {
  const { isConnected, request, saveQueue } = useSettingsServices();
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const reload = useCallback(async () => {
    if (!isConnected) {
      setLoading(false);
      return;
    }
    const id = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const next = await request<Record<string, unknown>>('config.get');
      if (id === requestId.current) {
        setConfig(next ?? {});
        setTeamOrganizationUiEnabled(next?.team_organization_ui_enabled);
        setTeamScheduledDispatchEnabled(next?.team_scheduled_dispatch_enabled);
      }
    } catch (loadError) {
      if (id === requestId.current) setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [isConnected, request]);
  useEffect(() => {
    void reload();
    return () => {
      requestId.current += 1;
    };
  }, [reload]);
  const save = useCallback(
    async (updates: Record<string, string>, operation: string) => {
      const payload = buildConfigSavePayload(updates);
      const result = await saveQueue.enqueue(operation, () =>
        request('config.save_all', payload, { timeoutMs: 600_000 }),
      );
      if ('team_organization_ui_enabled' in payload.config) {
        setTeamOrganizationUiEnabled(payload.config.team_organization_ui_enabled);
      }
      if ('team_scheduled_dispatch_enabled' in payload.config) {
        setTeamScheduledDispatchEnabled(payload.config.team_scheduled_dispatch_enabled);
      }
      setConfig((current) => ({ ...current, ...payload.config }));
      return result;
    },
    [request, saveQueue],
  );
  return { config, setConfig, loading, error, reload, save };
}
