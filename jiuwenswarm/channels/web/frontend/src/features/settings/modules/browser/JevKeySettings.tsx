import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { Form, FormDialog, useForm, useFormState, type FormItem } from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useSettingsConfig } from '../../services/useSettingsConfig';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { createJevDraft, JEV_PRESETS, validateJevDraft, type JevDraft, type JevProvider } from './jevSettings';

function JevConfigDialog({
  initialValues,
  config,
  save,
  onClose,
}: {
  initialValues: JevDraft;
  config: Record<string, unknown>;
  save: (draft: JevDraft) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const form = useForm<JevDraft>({ initialValues });
  useFormState(form);
  const values = form.getValues();
  const drafts = useRef<Partial<Record<JevProvider, JevDraft>>>({});
  const previousProvider = useRef(values.provider);
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState('');
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'browser-jev-config-dialog',
    form,
    closeBlocked: submitting,
    onClose,
  });
  const items: FormItem<JevDraft>[] = [
    {
      name: 'provider',
      label: t('settingsPanel.models.vendor'),
      component: 'select',
      required: true,
      options: (Object.keys(JEV_PRESETS) as JevProvider[]).map((provider) => ({
        value: provider,
        label: provider === 'custom' ? t('settingsPanel.browser.providerCustom') : JEV_PRESETS[provider].label,
      })),
      onChange: (value) => {
        const provider = value as JevProvider;
        drafts.current[previousProvider.current] = { ...form.getValues(), provider: previousProvider.current };
        form.setValues(drafts.current[provider] ?? createJevDraft({ decision_provider: provider }, config));
        previousProvider.current = provider;
        form.clearValidate();
        setSaveError('');
      },
    },
  ];
  if (values.provider === 'custom') {
    items.push({
      name: 'api_base',
      label: t('settingsPanel.fields.api_base.title'),
      component: 'input',
      required: true,
      placeholder: 'https://example.com/v1',
    });
  }
  items.push(
    {
      name: 'api_key',
      label: t('settingsPanel.models.apiKeyLabel'),
      component: 'input',
      type: 'password',
      required: true,
      placeholder: t('settingsPanel.fields.api_key.placeholder'),
      passwordVisibilityLabels: { show: t('settingsPanel.common.showValue'), hide: t('settingsPanel.common.hideValue') },
    },
    { name: 'model', label: t('settingsPanel.models.model'), component: 'input', required: true },
  );
  const confirm = async () => {
    const result = form.validate();
    if (!result.valid) return;
    setSubmitting(true);
    setSaveError('');
    try {
      await save(result.values);
      onClose();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <>
      <FormDialog
        open
        title={t('settingsPanel.browser.jevConfig')}
        submitting={submitting}
        confirmDisabled={!isConnected}
        confirmLabel={t('common.save')}
        cancelLabel={t('common.cancel')}
        testIdPrefix="settings-browser-jev-dialog"
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form
          form={form}
          items={items}
          disabled={submitting}
          showOptional={false}
          optionalText={t('common.optional')}
          testIdPrefix="settings-browser-jev-dialog"
          rules={Object.fromEntries(['api_base', 'api_key', 'model'].map((name) => [name, [{
            validator: (_value: unknown, draft: JevDraft) => {
              const key = validateJevDraft(draft)[name as keyof JevDraft];
              return key ? t(key) : undefined;
            },
          }]]))}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-browser-jev-dialog-error">
            {saveError}
          </div>
        ) : null}
      </FormDialog>
      <SettingsConfirmDialog
        open={discardConfirmationOpen}
        title={t('settingsPanel.dialog.discardTitle')}
        message={t('settingsPanel.dialog.discardConfirm')}
        onConfirm={confirmDiscard}
        onCancel={cancelDiscard}
      />
    </>
  );
}

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
      await saveConfigFields({ [JEV_PRESETS[draft.provider].keyField]: '' }, 'settingsPanel.browser.jevConfig');
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
        <JevConfigDialog initialValues={draft} config={config} save={saveDraft} onClose={() => setConfiguring(false)} />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t('settingsPanel.dialog.clearTitle', { name: t('settingsPanel.browser.jevConfig') })}
        message={t('settingsPanel.dialog.clearConfirm', { name: t('settingsPanel.models.apiKeyLabel') })}
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
