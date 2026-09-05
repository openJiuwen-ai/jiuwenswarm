import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Switch } from '../../../../components/ui';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean, toConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import {
  isMediaCapabilityConfigured,
  mediaCapabilityEnabledField,
  mediaCapabilityPersistenceFields,
  wasConfigAppliedWithoutRestart,
  type MediaCapabilityModality,
} from './mediaCapabilities';
import { MediaModelConfigDialog } from './MediaModelConfigDialog';
import './AgentSettings.css';

const keyFields = ['jina_api_key', 'bocha_api_key', 'perplexity_api_key', 'serper_api_key'] as const;
const modalities = ['vision', 'audio', 'video'] as const;

function isSearchKeyField(name: string): name is (typeof keyFields)[number] {
  return keyFields.some((field) => field === name);
}

function isRequiredAgentConfigField(name: string): boolean {
  return isSearchKeyField(name);
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
    initialValues: Object.fromEntries(fields.map((name) => [name, String(config[name] ?? '')])),
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
        const key = name.includes('key');
        const required = isRequiredAgentConfigField(name);
        return {
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
        testIdPrefix="settings-agent-config-dialog"
        testVariant={titleKey}
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form form={form} items={items} rules={rules} optionalText={t('common.optional')} testIdPrefix="settings-agent-config-dialog" />
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
            data-testid="settings-agent-key-configure-btn"
            data-variant={name}
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
  const { values, savingKeys, save } = useSettingsSource();
  const [dialog, setDialog] = useState<{
    modality: MediaCapabilityModality;
    enableOnSave: boolean;
  } | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);
  const saveConfig: SaveConfig = (updates, operation) => save(updates, operation);
  const handleSaveResult = (result: unknown) => {
    setRestartRequired(!wasConfigAppliedWithoutRestart(result));
  };

  const toggleCapability = async (modality: MediaCapabilityModality, nextEnabled: boolean) => {
    if (nextEnabled && !isMediaCapabilityConfigured(values, modality)) {
      setDialog({ modality, enableOnSave: true });
      return;
    }

    setRestartRequired(false);
    try {
      const result = await saveConfig(
        { [mediaCapabilityEnabledField(modality)]: toConfigBoolean(nextEnabled) },
        `settingsPanel.agent.${modality}`,
      );
      handleSaveResult(result);
    } catch {
      setRestartRequired(false);
    }
  };

  return (
    <>
      {restartRequired ? (
        <div className="settings-agent-media__restart-notice" role="status" data-testid="settings-agent-media-restart-notice">
          {t('settingsPanel.agent.savedRestartRequired')}
        </div>
      ) : null}
      {modalities.map((modality) => {
        const configured = isMediaCapabilityConfigured(values, modality);
        const enabledField = mediaCapabilityEnabledField(modality);
        const enabled = configured && parseConfigBoolean(values[enabledField]);
        const capabilityFields = [...mediaCapabilityPersistenceFields(modality), enabledField];
        const busy = capabilityFields.some((field) => savingKeys.has(field));
        const name = t(`settingsPanel.agent.${modality}`);
        return (
          <SettingRow
            key={modality}
            className="settings-agent-media__row"
            title={name}
            description={t(`settingsPanel.agent.${modality}Description`)}
            subSettings={
              configured ? (
                <div className="settings-agent-media__model-card">
                  <strong className="settings-agent-media__model-name">{String(values[`${modality}_model`])}</strong>
                  <div className="settings-agent-media__actions">
                    <Button
                      variant="quiet"
                      size="sm"
                      icon={<settingsActionIcons.edit aria-hidden />}
                      title={t('common.modify')}
                      aria-label={`${t('common.modify')} ${name}`}
                      disabled={disabled || !isConnected || busy}
                      onClick={() => setDialog({ modality, enableOnSave: false })}
                      data-testid="settings-agent-modality-edit-btn"
                      data-variant={modality}
                    />
                  </div>
                </div>
              ) : null
            }
          >
            <Switch
              checked={enabled}
              disabled={disabled || !isConnected || busy}
              aria-label={t('settingsPanel.agent.toggleCapability', { name })}
              onChange={(nextEnabled) => void toggleCapability(modality, nextEnabled)}
              data-testid="settings-agent-modality-toggle"
              data-variant={modality}
            />
          </SettingRow>
        );
      })}
      {dialog ? (
        <MediaModelConfigDialog
          modality={dialog.modality}
          config={values}
          save={saveConfig}
          enableOnSave={dialog.enableOnSave}
          onSaved={handleSaveResult}
          onClose={() => setDialog(null)}
        />
      ) : null}
    </>
  );
}
