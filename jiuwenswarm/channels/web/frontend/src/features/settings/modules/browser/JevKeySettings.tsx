import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useSettingsConfig } from '../../services/useSettingsConfig';
import { AgentConfigDialog, type SaveConfig } from '../agent/AgentSettings';

// Credentials can be configured before activating a provider. Reading/saving uses
// the same settings contract and password controls as model configuration.
const PROVIDER_FIELDS = [
  { provider: 'openrouter', fields: ['jev_openrouter_api_key'] },
  { provider: 'typesafe', fields: ['jev_typesafe_api_key'] },
] as const;
type ProviderFields = (typeof PROVIDER_FIELDS)[number];

export function JevKeySettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const { values, save, patchLocal } = useSettingsSource();
  const { config, loading, error, save: saveConfigFields } = useSettingsConfig();
  const [configuring, setConfiguring] = useState<ProviderFields | null>(null);
  const [clearing, setClearing] = useState<ProviderFields | null>(null);
  const [clearingBusy, setClearingBusy] = useState(false);
  const [clearError, setClearError] = useState('');
  const saveKey = async (updates: Record<string, string>, operation: string) => {
    const result = await saveConfigFields(updates, operation);
    // Refresh from the owner process after hot reload, not from a local guess.
    patchLocal(await request<Record<string, unknown>>('path.get'));
    return result;
  };
  const saveConfig: SaveConfig = (updates, operation) => saveKey(updates, operation);

  const confirmClear = async () => {
    if (!clearing) return;
    setClearingBusy(true);
    setClearError('');
    try {
      // Clearing an inactive provider must not disable the currently active one.
      if (clearing.provider === values.decision_provider && values.decision_mode !== 'llm')
        await save({ decision_mode: 'llm' }, 'settingsPanel.fields.decision_mode.title');
      const field = clearing.fields[0];
      await saveKey({ [field]: '' }, `settingsPanel.fields.${field}.title`);
      setClearing(null);
    } catch (error) {
      setClearError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setClearingBusy(false);
    }
  };

  return (
    <>
      {PROVIDER_FIELDS.map((entry) => {
        const field = entry.fields[0];
        const configured = entry.fields.every((name) => Boolean(config[name]));
        return (
          <SettingRow
            key={entry.provider}
            title={t(`settingsPanel.fields.${field}.title`)}
            description={
              loading
                ? t('common.loading')
                : configured
                  ? t('settingsPanel.common.configured')
                  : t('settingsPanel.common.notConfigured')
            }
          >
            <Button
              disabled={disabled || !isConnected || loading || Boolean(error) || clearingBusy}
              onClick={() => setConfiguring(entry)}
              data-testid="settings-browser-jev-key-configure-btn"
              data-variant={field}
            >
              {t('settingsPanel.common.configure')}
            </Button>
            {config[field] ? (
              <Button
                disabled={disabled || !isConnected || loading || clearingBusy}
                onClick={() => {
                  setClearError('');
                  setClearing(entry);
                }}
                data-testid="settings-browser-jev-key-clear-btn"
                data-variant={field}
              >
                {t('settingsPanel.common.clear')}
              </Button>
            ) : null}
          </SettingRow>
        );
      })}
      {error ? (
        <div className="settings-page__error" role="alert" data-testid="settings-browser-jev-key-load-error">
          {error}
        </div>
      ) : null}
      {configuring ? (
        <AgentConfigDialog
          titleKey={`settingsPanel.fields.${configuring.fields[0]}.title`}
          fields={configuring.fields}
          config={config}
          save={saveConfig}
          onClose={() => setConfiguring(null)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing !== null}
        title={
          clearing
            ? t('settingsPanel.dialog.clearTitle', { name: t(`settingsPanel.fields.${clearing.fields[0]}.title`) })
            : ''
        }
        message={
          clearing
            ? t('settingsPanel.dialog.clearConfirm', { name: t(`settingsPanel.fields.${clearing.fields[0]}.title`) })
            : ''
        }
        confirming={clearingBusy}
        error={clearError || undefined}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void confirmClear()}
        onCancel={() => {
          if (!clearingBusy) setClearing(null);
        }}
      />
    </>
  );
}
