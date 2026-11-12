import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import {
  Form,
  FormDialog,
  useForm,
  useFormState,
  type FormItem,
  type FormRules,
} from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';

const keyFields = ['jina_api_key', 'bocha_api_key', 'perplexity_api_key', 'serper_api_key'] as const;
export type SaveConfig = (updates: Record<string, string>, operation: string) => Promise<unknown>;

export function AgentConfigDialog({
  titleKey,
  fields,
  config,
  save,
  onClose,
}: {
  titleKey: string;
  fields: readonly string[];
  config: Record<string, unknown>;
  save: SaveConfig;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const form = useForm<Record<string, unknown>>({
    initialValues: Object.fromEntries(fields.map((name) => [name, String(config[name] ?? '')])),
  });
  useFormState(form);
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState('');
  const closeBlocked = submitting;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'agent-config-dialog',
    form,
    closeBlocked,
    onClose,
  });
  const items = useMemo(() => {
    const nextItems: FormItem<Record<string, unknown>>[] = fields.map((name) => {
      const key = name.includes('key');
      return {
        name,
        label: t(`settingsPanel.fields.${name}.title`),
        component: 'input' as const,
        type: key ? ('password' as const) : ('text' as const),
        required: true,
        passwordVisibilityLabels: key
          ? { show: t('settingsPanel.common.showValue'), hide: t('settingsPanel.common.hideValue') }
          : undefined,
        placeholder: t('config.enterValue'),
      };
    });
    return nextItems;
  }, [fields, form, t]);
  const rules = useMemo(() => {
    const nextRules: FormRules<Record<string, unknown>> = Object.fromEntries(
      fields.map((name) => [
        name,
        [
          {
            trigger: 'blur' as const,
            validator: (value: unknown) =>
              typeof value === 'string' && value.trim().length > 0 ? undefined : t('settingsPanel.validation.required'),
          },
        ],
      ]),
    );
    return nextRules;
  }, [fields, t]);
  const confirm = async () => {
    const result = form.validate();
    if (!result.valid) return;
    setSubmitting(true);
    setSaveError('');
    try {
      await save(Object.fromEntries(fields.map((name) => [name, String(result.values[name] ?? '').trim()])), titleKey);
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
        title={t(titleKey)}
        submitting={closeBlocked}
        confirmDisabled={!isConnected}
        confirmLabel={t('common.confirm')}
        cancelLabel={t('common.cancel')}
        testIdPrefix="settings-agent-config-dialog"
        testVariant={titleKey}
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form form={form} items={items} rules={rules} optionalText={t('common.optional')} />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-agent-config-dialog-error">
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

export function AgentSearchSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, save } = useSettingsSource();
  const [dialog, setDialog] = useState<{ titleKey: string; fields: readonly string[] } | null>(null);
  const [clearing, setClearing] = useState<{ name: string; titleKey: string } | null>(null);
  const [clearingBusy, setClearingBusy] = useState(false);
  const [clearError, setClearError] = useState('');
  const saveConfig: SaveConfig = (updates, operation) => save(updates, operation);
  const confirmClear = async () => {
    if (!clearing) return;
    setClearingBusy(true);
    setClearError('');
    try {
      await save({ [clearing.name]: '' }, clearing.titleKey);
      setClearing(null);
    } catch (error) {
      setClearError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setClearingBusy(false);
    }
  };
  return (
    <>
      {keyFields.map((name) => (
        <SettingRow
          key={name}
          title={t(`settingsPanel.fields.${name}.title`)}
          description={values[name] ? t('settingsPanel.common.configured') : t('settingsPanel.common.notConfigured')}
        >
          <Button
            disabled={disabled || !isConnected}
            onClick={() => setDialog({ titleKey: `settingsPanel.fields.${name}.title`, fields: [name] })}
            data-testid="settings-agent-key-configure-btn"
            data-variant={name}
          >
            {t('settingsPanel.common.configure')}
          </Button>
          {values[name] ? (
            <Button
              disabled={disabled || !isConnected}
              onClick={() => {
                setClearError('');
                setClearing({ name, titleKey: `settingsPanel.fields.${name}.title` });
              }}
              data-testid="settings-agent-key-clear-btn"
              data-variant={name}
            >
              {t('settingsPanel.common.clear')}
            </Button>
          ) : null}
        </SettingRow>
      ))}
      {dialog ? (
        <AgentConfigDialog {...dialog} config={values} save={saveConfig} onClose={() => setDialog(null)} />
      ) : null}
      <SettingsConfirmDialog
        open={clearing !== null}
        title={clearing ? t('settingsPanel.dialog.clearTitle', { name: t(clearing.titleKey) }) : ''}
        message={clearing ? t('settingsPanel.dialog.clearConfirm', { name: t(clearing.titleKey) }) : ''}
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
