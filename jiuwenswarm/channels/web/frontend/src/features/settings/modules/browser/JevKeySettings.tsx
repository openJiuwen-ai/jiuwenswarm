import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Loading } from '../../../../components/ui';
import { settingsActionIcons, settingsEmptyBoxIllustration } from '../../../../assets/settings';
import { getModelLogoUrl } from '../../../../assets/providers';
import { SettingsSection, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useSettingsConfig } from '../../services/useSettingsConfig';
import { SETTINGS_CONFIG_CHANGED_EVENT } from '../../services/settingsContract';
import { JevConfigDialog } from '../models/JevConfigDialog';
import { createEmptyJevDraft, createJevDraft, JEV_PRESETS, validateJevDraft, type JevDraft } from './jevSettings';

export function JevKeySettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const { values, save, patchLocal } = useSettingsSource();
  const { config, loading, error, save: saveConfigFields } = useSettingsConfig();
  const [dialogMode, setDialogMode] = useState<'add' | 'edit' | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearingBusy, setClearingBusy] = useState(false);
  const [clearError, setClearError] = useState('');
  const draft = createJevDraft(values, config);
  const refresh = async () => patchLocal(await request<Record<string, unknown>>('path.get'));
  const saveDraft = async (next: JevDraft) => {
    if (!next.provider) throw new Error(t('settingsPanel.models.validation.vendorSelectionRequired'));
    // Persist the credential before activating a newly selected provider.
    const keyField = JEV_PRESETS[next.provider].keyField;
    if (next.api_key.trim() !== config[keyField]) {
      await saveConfigFields({ [keyField]: next.api_key.trim() }, 'settingsPanel.browser.jevConfig');
    }
    await save({
      decision_provider: next.provider,
      decision_model: next.model.trim(),
      decision_display_name: next.display_name.trim(),
      decision_api_base: next.api_base.trim(),
    }, 'settingsPanel.browser.jevConfig');
    await refresh();
  };
  const confirmClear = async () => {
    setClearingBusy(true);
    setClearError('');
    try {
      await save({
        decision_mode: 'llm',
        decision_display_name: '',
      }, 'settingsPanel.browser.jevConfig');
      await saveConfigFields({
        [JEV_PRESETS[draft.provider].keyField]: '',
        moa_jev_enabled: 'false',
      }, 'settingsPanel.browser.jevConfig');
      await refresh();
      window.dispatchEvent(new window.Event(SETTINGS_CONFIG_CHANGED_EVENT));
      setClearing(false);
    } catch {
      setClearError(t('settingsPanel.models.jevDeleteFailed'));
    } finally {
      setClearingBusy(false);
    }
  };
  const providerName = draft.provider === 'custom'
    ? t('settingsPanel.browser.providerCustom')
    : JEV_PRESETS[draft.provider].label;
  const configured = values.jev_key_configured === true;
  const actionsDisabled = disabled || !isConnected || loading || Boolean(error) || clearingBusy;
  const metadata = `${providerName} · ${draft.model}`;
  return (
    <>
      <SettingsSection
        title={t('settingsPanel.models.jevModels')}
        separatedRows
      >
        {loading ? (
          <div className="settings-page__loading" data-testid="settings-models-jev-loading">
            <Loading aria-label={t('common.loading')} />
          </div>
        ) : null}
        {error ? (
          <div className="settings-page__error" role="alert" data-testid="settings-browser-jev-load-error">{error}</div>
        ) : null}
        {!loading && !error && !configured ? (
          <div className="settings-models__empty" data-testid="settings-models-jev-empty">
            <img src={settingsEmptyBoxIllustration} alt="" aria-hidden />
            <strong data-testid="settings-models-jev-empty-title">{t('settingsPanel.models.jevEmpty')}</strong>
            <p data-testid="settings-models-jev-empty-description">{t('settingsPanel.models.jevEmptyDescription')}</p>
            <Button
              variant="primary"
              disabled={actionsDisabled}
              onClick={() => setDialogMode('add')}
              data-testid="settings-models-jev-empty-add-btn"
            >
              {t('settingsPanel.models.addModel')}
            </Button>
          </div>
        ) : null}
        {!loading && !error && configured ? (
          <div className="settings-models__list" data-testid="settings-models-jev-list">
            <article className="settings-model-card" data-testid="settings-models-jev-card">
              <img
                className="settings-model-card__logo"
                src={getModelLogoUrl({ vendor_key: draft.provider })}
                alt=""
                aria-hidden
              />
              <div className="settings-model-card__copy">
                <div className="settings-model-card__title-row">
                  <h3 title={draft.display_name} data-testid="settings-models-jev-card-title">{draft.display_name}</h3>
                </div>
                <p title={metadata} data-testid="settings-models-jev-card-metadata">{metadata}</p>
              </div>
              <div className="settings-model-card__actions">
                <Button
                  icon={<settingsActionIcons.edit aria-hidden />}
                  aria-label={t('common.modify')}
                  title={t('common.modify')}
                  disabled={actionsDisabled}
                  onClick={() => setDialogMode('edit')}
                  data-testid="settings-models-jev-edit-btn"
                />
                <Button
                  variant="quiet"
                  icon={<settingsActionIcons.delete aria-hidden />}
                  aria-label={t('common.delete')}
                  title={t('common.delete')}
                  disabled={actionsDisabled}
                  onClick={() => { setClearError(''); setClearing(true); }}
                  data-testid="settings-models-jev-delete-btn"
                />
              </div>
            </article>
          </div>
        ) : null}
      </SettingsSection>
      {dialogMode ? (
        <JevConfigDialog
          mode={dialogMode}
          initialValues={dialogMode === 'add' ? createEmptyJevDraft() : draft}
          createProviderDraft={(provider) => createJevDraft({ decision_provider: provider }, config)}
          validate={validateJevDraft}
          save={saveDraft}
          onClose={() => setDialogMode(null)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t('settingsPanel.models.deleteConfirmTitle')}
        message={t('settingsPanel.browser.clearSharedJevConfirm')}
        confirming={clearingBusy}
        error={clearError || undefined}
        confirmLabel={t('common.delete')}
        confirmVariant="danger"
        onConfirm={() => void confirmClear()}
        onCancel={() => { if (!clearingBusy) setClearing(false); }}
      />
    </>
  );
}
