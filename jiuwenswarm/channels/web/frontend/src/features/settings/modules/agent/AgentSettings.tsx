import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Switch } from '../../../../components/ui';
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
import { parseConfigBoolean, toConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { GenerationModelConfigDialog } from './GenerationModelConfigDialog';
import { isGenerationSlotConfigured, type GenerationSlot } from './generationModels';
import './AgentSettings.css';

const keyFields = ['jina_api_key', 'bocha_api_key', 'perplexity_api_key', 'serper_api_key'] as const;
const generationSlotFields = ['provider', 'protocol', 'api_base', 'api_key', 'model'] as const;
// 音频生成没有 context window 字段，后端也不认 music_gen_context_window_tokens。
const generationSlotSettings: Record<GenerationSlot, { titleKey: string; contextWindow: boolean }> = {
  video_gen: { titleKey: 'settingsPanel.agent.videoGenConfigTitle', contextWindow: true },
  visual_gen: { titleKey: 'settingsPanel.agent.visualGenConfigTitle', contextWindow: true },
  music_gen: { titleKey: 'settingsPanel.agent.musicGenConfigTitle', contextWindow: false },
};

function generationSlotConfigFields(slot: GenerationSlot): string[] {
  const fields = generationSlotFields.map((suffix) => `${slot}_${suffix}`);
  return generationSlotSettings[slot].contextWindow ? [...fields, `${slot}_context_window_tokens`] : fields;
}

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

export function MusicGenSettings(props: SettingsCustomItemProps) {
  return <GenerationSlotSettings slot="music_gen" {...props} />;
}

function GenerationSlotSettings({ slot, disabled }: SettingsCustomItemProps & { slot: GenerationSlot }) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, savingKeys, save } = useSettingsSource();
  const [dialog, setDialog] = useState<{ enableOnSave: boolean } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const saveConfig: SaveConfig = (updates, operation) => save(updates, operation);

  const enabledField = `${slot}_enabled`;
  const enabledTitleKey = `settingsPanel.fields.${enabledField}.title`;
  const configFields = generationSlotConfigFields(slot);
  const configured = isGenerationSlotConfigured(values, slot);
  const enabled = configured && parseConfigBoolean(values[enabledField]);
  const busy = [...configFields, enabledField].some((field) => savingKeys.has(field));
  const name = t(enabledTitleKey);

  const toggle = async (nextEnabled: boolean) => {
    if (nextEnabled && !configured) {
      setDialog({ enableOnSave: true });
      return;
    }
    try {
      await saveConfig({ [enabledField]: toConfigBoolean(nextEnabled) }, enabledTitleKey);
    } catch {
      // Surfaced via savingKeys/isConnected state already; nothing further to do here.
    }
  };

  const confirmDelete = async () => {
    const updates: Record<string, string> = Object.fromEntries(configFields.map((field) => [field, '']));
    if (parseConfigBoolean(values[enabledField])) {
      updates[enabledField] = toConfigBoolean(false);
    }
    setDeleting(true);
    setDeleteError('');
    try {
      await saveConfig(updates, enabledTitleKey);
      setDeleteTarget(false);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <>
      <SettingRow
        className="settings-agent-media__row"
        title={name}
        description={t(`settingsPanel.fields.${enabledField}.description`)}
        subSettings={
          configured ? (
            <div className="settings-agent-media__model-card">
              <strong className="settings-agent-media__model-name">
                {String(values[`${slot}_model`] ?? '').trim() || t('settingsPanel.agent.servedModel')}
              </strong>
              <div className="settings-agent-media__actions">
                <Button
                  variant="quiet"
                  size="sm"
                  icon={<settingsActionIcons.edit aria-hidden />}
                  title={t('common.modify')}
                  aria-label={`${t('common.modify')} ${name}`}
                  disabled={disabled || !isConnected || busy}
                  onClick={() => setDialog({ enableOnSave: false })}
                />
                <Button
                  variant="quiet"
                  size="sm"
                  icon={<settingsActionIcons.delete aria-hidden />}
                  title={t('common.delete')}
                  aria-label={`${t('common.delete')} ${name}`}
                  disabled={disabled || !isConnected || busy}
                  onClick={() => {
                    setDeleteError('');
                    setDeleteTarget(true);
                  }}
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
          onChange={(nextEnabled) => void toggle(nextEnabled)}
        />
      </SettingRow>
      {dialog ? (
        <GenerationModelConfigDialog
          slot={slot}
          titleKey={generationSlotSettings[slot].titleKey}
          enableOnSave={dialog.enableOnSave}
          config={values}
          save={
            dialog.enableOnSave
              ? (updates, operation) => saveConfig({ ...updates, [enabledField]: toConfigBoolean(true) }, operation)
              : saveConfig
          }
          onClose={() => setDialog(null)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={deleteTarget}
        title={t('settingsPanel.agent.deleteModelTitle')}
        message={t('settingsPanel.agent.deleteModelConfirm', { name })}
        confirming={deleting}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onCancel={() => {
          if (!deleting) setDeleteTarget(false);
        }}
      />
    </>
  );
}
