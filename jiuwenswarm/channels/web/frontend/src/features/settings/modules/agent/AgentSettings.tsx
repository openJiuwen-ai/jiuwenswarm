import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';

const keyFields = ['jina_api_key', 'bocha_api_key', 'perplexity_api_key', 'serper_api_key'] as const;
const modalities = ['vision', 'audio', 'video'] as const;
const modalityConfigSuffixes = ['api_base', 'api_key', 'model', 'provider'] as const;
const modalityConfigFields: ReadonlySet<string> = new Set(
  modalities.flatMap((modality) => modalityConfigSuffixes.map((suffix) => `${modality}_${suffix}`)),
);
const modalityProviderFields: ReadonlySet<string> = new Set(modalities.map((modality) => `${modality}_provider`));

function isSearchKeyField(name: string): name is (typeof keyFields)[number] {
  return keyFields.some((field) => field === name);
}

function isRequiredAgentConfigField(name: string): boolean {
  return isSearchKeyField(name) || modalityConfigFields.has(name);
}

function getInitialAgentConfigValue(name: string, config: Record<string, unknown>): string {
  const value = String(config[name] ?? '');
  return modalityProviderFields.has(name) && !value.trim() ? 'OpenAI' : value;
}

type SaveConfig = (updates: Record<string, string>, operation: string) => Promise<unknown>;

function AgentConfigDialog({
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
  const form = useForm({
    initialValues: Object.fromEntries(fields.map((name) => [name, getInitialAgentConfigValue(name, config)])),
  });
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState('');
  const closeBlocked = submitting;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'agent-config-dialog',
    form,
    closeBlocked,
    onClose,
  });
  const items = useMemo(
    () =>
      fields.map((name) => {
        const provider = name.endsWith('_provider');
        const key = name.includes('key');
        const required = isRequiredAgentConfigField(name);
        return provider
          ? {
              name,
              label: t(`settingsPanel.fields.${name}.title`),
              helpTips: t('config.keyHelp.modelProvider'),
              component: 'select' as const,
              options: [{ value: 'OpenAI', label: 'OpenAI' }],
              required,
            }
          : {
              name,
              label: t(`settingsPanel.fields.${name}.title`),
              component: 'input' as const,
              type: key ? ('password' as const) : ('text' as const),
              passwordVisibilityLabels: key
                ? { show: t('settingsPanel.common.showValue'), hide: t('settingsPanel.common.hideValue') }
                : undefined,
              placeholder: t('config.enterValue'),
              required,
            };
      }),
    [fields, t],
  );
  const rules = useMemo(
    () =>
      Object.fromEntries(
        fields.filter(isRequiredAgentConfigField).map((name) => [
          name,
          [
            {
              validator: (value: unknown) =>
                String(value ?? '').trim() ? undefined : t('settingsPanel.validation.required'),
            },
          ],
        ]),
      ),
    [fields, t],
  );
  const confirm = async () => {
    const result = form.validate();
    if (!result.valid) return;
    setSubmitting(true);
    setSaveError('');
    try {
      await save(
        Object.fromEntries(fields.map((name) => [name, String(result.values[name] ?? '').trim()])),
        titleKey,
      );
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
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form form={form} items={items} rules={rules} optionalText={t('common.optional')} />
        {saveError ? (
          <div className="settings-page__error" role="alert">
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
  const saveConfig: SaveConfig = (updates, operation) => save(updates, operation);
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
          >
            {t('settingsPanel.common.configure')}
          </Button>
        </SettingRow>
      ))}
      {dialog ? (
        <AgentConfigDialog {...dialog} config={values} save={saveConfig} onClose={() => setDialog(null)} />
      ) : null}
    </>
  );
}

export function AgentMediaSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, save } = useSettingsSource();
  const [dialog, setDialog] = useState<{ titleKey: string; fields: readonly string[] } | null>(null);
  const saveConfig: SaveConfig = (updates, operation) => save(updates, operation);
  return (
    <>
      {modalities.map((modality) => (
        <SettingRow
          key={modality}
          title={t(`settingsPanel.agent.${modality}`)}
          description={
            values[`${modality}_model`] ? String(values[`${modality}_model`]) : t('settingsPanel.common.notConfigured')
          }
        >
          <Button
            disabled={disabled || !isConnected}
            onClick={() =>
              setDialog({
                titleKey: `settingsPanel.agent.${modality}`,
                fields: [`${modality}_api_base`, `${modality}_api_key`, `${modality}_model`, `${modality}_provider`],
              })
            }
          >
            {t('settingsPanel.common.configure')}
          </Button>
        </SettingRow>
      ))}
      {dialog ? (
        <AgentConfigDialog {...dialog} config={values} save={saveConfig} onClose={() => setDialog(null)} />
      ) : null}
    </>
  );
}
