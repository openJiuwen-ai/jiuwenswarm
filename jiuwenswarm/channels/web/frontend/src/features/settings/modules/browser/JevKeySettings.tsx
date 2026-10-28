import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useSettingsConfig } from '../../services/useSettingsConfig';
import { JevConfigDialog } from '../models/JevConfigDialog';
import { createJevDraft, JEV_PRESETS, validateJevDraft, type JevDraft } from './jevSettings';

export function JevKeySettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const { values, save, patchLocal } = useSettingsSource();
  const { config, loading, error, save: saveConfigFields } = useSettingsConfig();
  const [configuring, setConfiguring] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearingBusy, setClearingBusy] = useState(false);
  const [clearError, setClearError] = useState('');
  const draft = createJevDraft(values, config);
  const refresh = async () => patchLocal(await request<Record<string, unknown>>('path.get'));
  const saveDraft = async (next: JevDraft) => {
    // Persist the credential before activating a newly selected provider.
    const keyField = JEV_PRESETS[next.provider].keyField;
    if (next.api_key.trim() !== config[keyField]) {
      await saveConfigFields({ [keyField]: next.api_key.trim() }, 'settingsPanel.browser.jevConfig');
    }
    await save({
      decision_provider: next.provider,
      decision_model: next.model.trim(),
      decision_api_base: next.api_base.trim(),
    }, 'settingsPanel.browser.jevConfig');
    await refresh();
  };
  const confirmClear = async () => {
    setClearingBusy(true);
    setClearError('');
    try {
      if (values.decision_mode !== 'llm') {
        await save({ decision_mode: 'llm' }, 'settingsPanel.fields.decision_mode.title');
      }
      await saveConfigFields({
        [JEV_PRESETS[draft.provider].keyField]: '',
        moa_jev_enabled: 'false',
      }, 'settingsPanel.browser.jevConfig');
      await refresh();
      setClearing(false);
    } catch (error) {
      setClearError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setClearingBusy(false);
    }
  };
  const providerName = draft.provider === 'custom'
    ? t('settingsPanel.browser.providerCustom')
    : JEV_PRESETS[draft.provider].label;
  return (
    <>
      <SettingRow
        title={t('settingsPanel.browser.jevConfig')}
        description={loading ? t('common.loading') : draft.api_key
          ? `${providerName} · ${draft.model}` : t('settingsPanel.common.notConfigured')}
      >
        <Button
          disabled={disabled || !isConnected || loading || Boolean(error) || clearingBusy}
          onClick={() => setConfiguring(true)}
          data-testid="settings-browser-jev-configure-btn"
        >
          {t('settingsPanel.common.configure')}
        </Button>
        {draft.api_key ? (
          <Button
            disabled={disabled || !isConnected || loading || clearingBusy}
            onClick={() => { setClearError(''); setClearing(true); }}
            data-testid="settings-browser-jev-clear-btn"
          >
            {t('settingsPanel.common.clear')}
          </Button>
        ) : null}
      </SettingRow>
      {error ? (
        <div className="settings-page__error" role="alert" data-testid="settings-browser-jev-load-error">{error}</div>
      ) : null}
      {configuring ? (
        <JevConfigDialog
          initialValues={draft}
          createProviderDraft={(provider) => createJevDraft({ decision_provider: provider }, config)}
          validate={validateJevDraft}
          save={saveDraft}
          onClose={() => setConfiguring(false)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t('settingsPanel.dialog.clearTitle', { name: t('settingsPanel.browser.jevConfig') })}
        message={t('settingsPanel.browser.clearSharedJevConfirm')}
        confirming={clearingBusy}
        error={clearError || undefined}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void confirmClear()}
        onCancel={() => { if (!clearingBusy) setClearing(false); }}
      />
    </>
  );
}
