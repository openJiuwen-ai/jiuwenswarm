import { useCallback, useEffect, useRef, useState } from 'react';
import {
  applyMultiModelCollaborationSnapshot,
  getMultiModelCollaborationRevision,
  isMultiModelCollaborationEnabled,
  setMultiModelCollaborationEnabled,
} from '../../multiModelCollaboration/featureConfig';
import { setTeamScheduledDispatchEnabled } from '../../teamDispatch/featureConfig';
import { setTeamOrganizationUiEnabled } from '../../teamOrganization/featureConfig';
import { buildConfigSavePayload, SETTINGS_CONFIG_CHANGED_EVENT } from './settingsContract';
import { useSettingsServices } from './SettingsServicesProvider';

export function useSettingsConfig() {
  const { isConnected, request, saveQueue } = useSettingsServices();
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const saveRevision = useRef(0);
  const confirmedUpdates = useRef(new Map<string, { revision: number; value: unknown }>());
  const reload = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    if (!isConnected) {
      setLoading(false);
      return;
    }
    const id = ++requestId.current;
    const revisionAtLoad = saveRevision.current;
    const featureRevisionAtLoad = getMultiModelCollaborationRevision();
    if (!silent) setLoading(true);
    setError(null);
    try {
      const next = await request<Record<string, unknown>>('config.get');
      if (id === requestId.current) {
        const visible = { ...(next ?? {}) };
        delete visible.moa_jev_api_key;
        delete visible.moa_jev_api_key_clear;
        // A reconnect read that began before a save must not overwrite the confirmed write.
        for (const [key, update] of confirmedUpdates.current) {
          if (update.revision > revisionAtLoad) visible[key] = update.value;
        }
        if (!applyMultiModelCollaborationSnapshot(visible.multi_model_collaboration_enabled, featureRevisionAtLoad)) {
          visible.multi_model_collaboration_enabled = isMultiModelCollaborationEnabled();
        }
        setConfig(visible);
        setTeamOrganizationUiEnabled(visible.team_organization_ui_enabled);
        setTeamScheduledDispatchEnabled(visible.team_scheduled_dispatch_enabled);
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
  useEffect(() => {
    const handleConfigChanged = () => {
      void reload({ silent: true });
    };
    window.addEventListener(SETTINGS_CONFIG_CHANGED_EVENT, handleConfigChanged);
    return () => window.removeEventListener(SETTINGS_CONFIG_CHANGED_EVENT, handleConfigChanged);
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
      if ('multi_model_collaboration_enabled' in payload.config) {
        setMultiModelCollaborationEnabled(payload.config.multi_model_collaboration_enabled);
      }
      const visibleUpdates = payload.config;
      const revision = ++saveRevision.current;
      for (const [key, value] of Object.entries(visibleUpdates)) {
        confirmedUpdates.current.set(key, { revision, value });
      }
      setConfig((current) => ({ ...current, ...visibleUpdates }));
      return result;
    },
    [request, saveQueue],
  );
  return { config, setConfig, loading, error, reload, save };
}
