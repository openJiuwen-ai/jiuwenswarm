import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { buildConfigSavePayload } from '../../services/settingsContract';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { AgentConfigDialog, type SaveConfig } from '../agent/AgentSettings';

// The key is stored in the .env variable of the selected provider and hot-reloaded into
// the AgentServer by config.save_all; the page only ever learns whether it is configured.
const KEY_FIELD_BY_PROVIDER: Record<string, string> = {
  openrouter: 'jev_openrouter_api_key',
  typesafe: 'jev_typesafe_api_key',
};

export function JevKeySettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request, saveQueue } = useSettingsServices();
  const { values, save, patchLocal } = useSettingsSource();
  const [configuring, setConfiguring] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearingBusy, setClearingBusy] = useState(false);
  const [clearError, setClearError] = useState('');
  const field = KEY_FIELD_BY_PROVIDER[String(values.decision_provider)] ?? KEY_FIELD_BY_PROVIDER.typesafe;
  const titleKey = `settingsPanel.fields.${field}.title`;
  const configured = values.jev_key_configured === true;

  const saveKey = async (updates: Record<string, string>, operation: string) => {
    const result = await saveQueue.enqueue(operation, () =>
      request('config.save_all', buildConfigSavePayload(updates), { timeoutMs: 600_000 }),
    );
    patchLocal({ jev_key_configured: Boolean(updates[field]) });
    return result;
  };
  const saveConfig: SaveConfig = (updates, operation) => saveKey(updates, operation);

  const confirmClear = async () => {
    setClearingBusy(true);
    setClearError('');
    try {
      // Jev cannot run without its key, so turn it off before removing the key.
      if (values.decision_mode !== 'llm')
        await save({ decision_mode: 'llm' }, 'settingsPanel.fields.decision_mode.title');
      await saveKey({ [field]: '' }, titleKey);
      setClearing(false);
    } catch (error) {
      setClearError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setClearingBusy(false);
    }
  };

  return (
    <>
      <SettingRow
        title={t('settingsPanel.fields.jev_api_key.title')}
        description={configured ? t('settingsPanel.common.configured') : t('settingsPanel.common.notConfigured')}
      >
        <Button
          disabled={disabled || !isConnected}
          onClick={() => setConfiguring(true)}
          data-testid="settings-browser-jev-key-configure-btn"
          data-variant={field}
        >
          {t('settingsPanel.common.configure')}
        </Button>
        {configured ? (
          <Button
            disabled={disabled || !isConnected}
            onClick={() => {
              setClearError('');
              setClearing(true);
            }}
            data-testid="settings-browser-jev-key-clear-btn"
            data-variant={field}
          >
            {t('settingsPanel.common.clear')}
          </Button>
        ) : null}
      </SettingRow>
      {configuring ? (
        <AgentConfigDialog
          titleKey={titleKey}
          fields={[field]}
          config={{}}
          save={saveConfig}
          onClose={() => setConfiguring(false)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t('settingsPanel.dialog.clearTitle', { name: t(titleKey) })}
        message={t('settingsPanel.dialog.clearConfirm', { name: t(titleKey) })}
        confirming={clearingBusy}
        error={clearError || undefined}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void confirmClear()}
        onCancel={() => {
          if (!clearingBusy) setClearing(false);
        }}
      />
    </>
  );
}
