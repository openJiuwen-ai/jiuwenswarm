// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../../../stores';
import { Switch } from '../../../../components/ui';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';

type A4PConfig = { enabled: boolean };

export function A4PSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const [config, setConfig] = useState<A4PConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const configGeneration = useRef(0);
  const enabled = config?.enabled ?? false;
  const [error, setError] = useState('');

  const loadConfig = useCallback(async () => {
    const generation = ++configGeneration.current;
    setConfig(null);
    setError('');
    setSaving(false);
    savingRef.current = false;
    if (!activeSessionId || !isConnected) return;
    try {
      const loaded = await request<A4PConfig>('a4p.config.get', { session_id: activeSessionId });
      if (generation === configGeneration.current) setConfig(loaded);
    } catch (loadError) {
      if (generation === configGeneration.current) {
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      }
    }
  }, [activeSessionId, isConnected, request]);

  useEffect(() => {
    void loadConfig();
    return () => {
      configGeneration.current += 1;
    };
  }, [loadConfig]);

  async function updateSetting(key: keyof A4PConfig, next: boolean) {
    if (controlsDisabled || !config || savingRef.current) return;
    setError('');
    const generation = configGeneration.current;
    savingRef.current = true;
    setSaving(true);
    try {
      const updated = await request<A4PConfig>('a4p.config.update', {
        [key]: next,
        session_id: activeSessionId,
      });
      if (generation !== configGeneration.current) return;
      setConfig(updated);
    } catch (saveError) {
      if (generation === configGeneration.current) {
        setError(saveError instanceof Error ? saveError.message : String(saveError));
      }
    } finally {
      if (generation === configGeneration.current) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  }

  const controlsDisabled = disabled || !isConnected || !activeSessionId;
  return (
    <div className="settings-a4p" data-testid="settings-a4p">
      <SettingRow title={t('a4pSettings.enableTitle')} description={t('a4pSettings.description')}>
        <Switch
          data-testid="settings-a4p-enabled"
          aria-label={t('a4pSettings.enableTitle')}
          checked={enabled}
          disabled={controlsDisabled || !config || saving}
          onChange={(next) => void updateSetting('enabled', next)}
        />
      </SettingRow>
      {error ? (
        <div className="settings-page__error" role="alert" data-testid="settings-a4p-operation-error">
          {error}
        </div>
      ) : null}
    </div>
  );
}
