import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, FormDialog, useForm, useFormState, type FormItem } from '../../../../components/form';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { JEV_PRESETS, type JevDraft, type JevProvider } from '../browser/jevSettings';

const BROWSER_PROVIDERS: readonly JevProvider[] = ['openrouter', 'typesafe', 'custom'];

export function JevConfigDialog({
  initialValues,
  createProviderDraft,
  validate,
  save,
  onClose,
}: {
  initialValues: JevDraft;
  createProviderDraft: (provider: JevProvider) => JevDraft;
  validate: (draft: JevDraft) => Partial<Record<keyof JevDraft, string>>;
  save: (draft: JevDraft) => Promise<void>;
  onClose: () => void;
}) {
  const testIdPrefix = 'settings-browser-jev-dialog';
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const form = useForm<JevDraft>({ initialValues });
  useFormState(form);
  const values = form.getValues();
  const drafts = useRef<Partial<Record<JevProvider, JevDraft>>>({});
  const previousProvider = useRef(values.provider);
  const busy = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState('');
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: testIdPrefix,
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
      options: BROWSER_PROVIDERS.map((provider) => ({
        value: provider,
        label: provider === 'custom' ? t('settingsPanel.browser.providerCustom') : JEV_PRESETS[provider].label,
      })),
      onChange: (value) => {
        const provider = value as JevProvider;
        drafts.current[previousProvider.current] = { ...form.getValues(), provider: previousProvider.current };
        form.setValues(drafts.current[provider] ?? createProviderDraft(provider));
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
    if (busy.current || !isConnected) return;
    const result = form.validate();
    if (!result.valid) return;
    busy.current = true;
    setSubmitting(true);
    setSaveError('');
    try {
      await save(result.values);
      onClose();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      busy.current = false;
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
        testIdPrefix={testIdPrefix}
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form
          form={form}
          items={items}
          disabled={submitting || !isConnected}
          showOptional={false}
          optionalText={t('common.optional')}
          testIdPrefix={testIdPrefix}
          rules={Object.fromEntries(['api_base', 'api_key', 'model'].map((name) => [name, [{
            validator: (_value: unknown, draft: JevDraft) => {
              const key = validate(draft)[name as keyof JevDraft];
              return key ? t(key) : undefined;
            },
          }]]))}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid={`${testIdPrefix}-error`}>
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
