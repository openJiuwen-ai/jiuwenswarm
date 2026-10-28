// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Switch } from '../../../../components/ui';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { setA2UIFeatureEnabled } from '../../../../features/a2ui/featureConfig';
import { normalizeRSIEnabled, setRSIFeatureEnabled } from '../../../../features/rsi/featureConfig';
import { setTrajectoryDiagnosisEnabled } from '../../../../features/trajectory/diagnosis/diagnosisFeatureConfig';
import { setTaskFullDuplexEnabled } from '../../../../features/taskFullDuplex/featureFlag';
import { setTaskAsrEnabled } from '../../../../features/taskAsr/featureFlag';
import { VideoDuplexModelSettings } from './VideoDuplexModelSettings';
import '../agent/AgentSettings.css';
import './VideoDuplexModelSettings.css';
import {
  AddCustomAgentPanel,
  parseExternalCliCustomAgents,
  EXTERNAL_CLI_CUSTOM_AGENTS_CONFIG_KEY,
  type ExternalCliCustomAgent,
} from '../../../../components/ExternalCliAgentsSection';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';

const CUSTOM_AGENT_UPLOAD_URL = '/file-api/external-cli/custom-agents';

/** Upload a provider wheel (and optional CLI executable); resolves to the installed agent. */
async function uploadCustomAgent(wheel: File, cli: File | null): Promise<ExternalCliCustomAgent> {
  const form = new FormData();
  form.append('wheel', wheel);
  if (cli) form.append('cli', cli);
  const response = await fetch(CUSTOM_AGENT_UPLOAD_URL, { method: 'POST', body: form });
  const payload = (await response.json().catch(() => ({}))) as { agent?: ExternalCliCustomAgent; error?: string };
  if (!response.ok || !payload.agent) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload.agent;
}

function ProactiveLimitsDialog({
  values,
  onClose,
  onSave,
}: {
  values: { daily: string; rounds: string };
  onClose: () => void;
  onSave: (values: { daily: string; rounds: string }) => Promise<void>;
}) {
  const { t } = useTranslation();
  const form = useForm({ initialValues: values });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const closeBlocked = saving;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'proactive-limits-dialog',
    form,
    closeBlocked,
    onClose,
  });
  const validator = (value: unknown) =>
    /^\d+$/.test(String(value)) && Number(value) >= 1 && Number(value) <= 50
      ? undefined
      : t('settingsPanel.validation.integerRange', { min: 1, max: 50 });
  const submit = async () => {
    const result = form.validate();
    if (!result.valid) return;
    setSaving(true);
    setSaveError('');
    try {
      await onSave(result.values);
      onClose();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSaving(false);
    }
  };
  return (
    <>
      <FormDialog
        open
        title={t('settingsPanel.experimental.proactiveLimits')}
        submitting={closeBlocked}
        confirmLabel={t('common.confirm')}
        cancelLabel={t('common.cancel')}
        testIdPrefix="settings-proactive-limits-dialog"
        onConfirm={() => void submit()}
        onCancel={requestClose}
      >
        <Form
          form={form}
          optionalText={t('common.optional')}
          testIdPrefix="settings-proactive-limits-dialog"
          rules={{ daily: [{ validator }], rounds: [{ validator }] }}
          items={[
            {
              name: 'daily',
              label: t('settingsPanel.fields.proactive_recommendation_max_recommend_per_day.title'),
              component: 'input',
              type: 'number',
              min: 1,
              max: 50,
              step: 1,
              changeOnBlur: true,
              required: true,
            },
            {
              name: 'rounds',
              label: t('settingsPanel.fields.proactive_recommendation_max_rounds_per_tick.title'),
              component: 'input',
              type: 'number',
              min: 1,
              max: 50,
              step: 1,
              changeOnBlur: true,
              required: true,
            },
          ]}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-proactive-limits-dialog-error">
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

export function RSISetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const rsi = normalizeRSIEnabled(source.values.rsi_enabled);
  async function updateRSI(next: boolean): Promise<void> {
    await source.save({ rsi_enabled: next }, 'rsi-enabled');
    setRSIFeatureEnabled(next);
  }

  return (
    <SettingRow
      title={t('settingsPanel.fields.rsi_enabled.title')}
      description={t('settingsPanel.fields.rsi_enabled.description')}
    >
      <Switch
        aria-label={t('settingsPanel.fields.rsi_enabled.title')}
        checked={rsi}
        disabled={disabled || !isConnected || source.savingKeys.has('rsi_enabled')}
        onChange={(next) => void updateRSI(next).catch(() => undefined)}
      />
    </SettingRow>
  );
}

export function A2UISetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const a2ui = parseConfigBoolean(source.values.a2ui_enabled);
  async function updateA2UI(next: boolean): Promise<void> {
    await source.save({ a2ui_enabled: next }, 'a2ui-enabled');
    setA2UIFeatureEnabled(next);
  }

  return (
    <SettingRow
      title={t('settingsPanel.fields.a2ui_enabled.title')}
      description={t('settingsPanel.fields.a2ui_enabled.description')}
    >
      <Switch
        aria-label={t('settingsPanel.fields.a2ui_enabled.title')}
        checked={a2ui}
        disabled={disabled || !isConnected || source.savingKeys.has('a2ui_enabled')}
        onChange={(next) => void updateA2UI(next).catch(() => undefined)}
        data-testid="settings-a2ui-switch"
      />
    </SettingRow>
  );
}

export function TrajectoryDiagnosisSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const enabled = parseConfigBoolean(source.values.trajectory_ui_diagnosis_enabled);

  async function updateDiagnosis(next: boolean): Promise<void> {
    await source.save({ trajectory_ui_diagnosis_enabled: next }, 'trajectory-diagnosis-enabled');
    setTrajectoryDiagnosisEnabled(next);
  }

  return (
    <SettingRow
      title={t('settingsPanel.fields.trajectory_ui_diagnosis_enabled.title')}
      description={t('settingsPanel.fields.trajectory_ui_diagnosis_enabled.description')}
    >
      <Switch
        aria-label={t('settingsPanel.fields.trajectory_ui_diagnosis_enabled.title')}
        checked={enabled}
        disabled={disabled || !isConnected || source.savingKeys.has('trajectory_ui_diagnosis_enabled')}
        onChange={(next) => void updateDiagnosis(next).catch(() => undefined)}
        data-testid="settings-trajectory-diagnosis-switch"
      />
    </SettingRow>
  );
}

export function TaskFullDuplexSetting({ disabled }: SettingsCustomItemProps) {
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const enabled = parseConfigBoolean(source.values.task_full_duplex_enabled);

  async function updateTaskFullDuplex(next: boolean): Promise<void> {
    await source.save({ task_full_duplex_enabled: next }, 'task-full-duplex-enabled');
    setTaskFullDuplexEnabled(next);
  }

  return (
    <VideoDuplexModelSettings
      enabled={enabled}
      onEnabledChange={updateTaskFullDuplex}
      disabled={disabled || !isConnected || source.savingKeys.has('task_full_duplex_enabled')}
      onConfigured={async () => { if (!enabled) await updateTaskFullDuplex(true); }}
    />
  );
}

type TaskAsrDraft = {
  api_base: string;
  api_key: string;
  model: string;
};

function TaskAsrConfigDialog({
  initialValues,
  enableOnSave,
  save,
  onClose,
}: {
  initialValues: TaskAsrDraft;
  enableOnSave: boolean;
  save: (updates: Record<string, string>, operation: string) => Promise<unknown>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const [draft, setDraft] = useState(initialValues);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const update = (key: keyof TaskAsrDraft, value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const confirm = async () => {
    const missing = (Object.keys(draft) as Array<keyof TaskAsrDraft>).find((key) => !draft[key].trim());
    if (missing) {
      setError(t('settingsPanel.taskAsr.validationRequired'));
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await save(
        {
          asr_api_base: draft.api_base.trim(),
          asr_api_key: draft.api_key.trim(),
          asr_model: draft.model.trim(),
          ...(enableOnSave ? { task_asr_enabled: 'true' } : {}),
        },
        'task-asr-model-settings',
      );
      if (enableOnSave) setTaskAsrEnabled(true);
      onClose();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };
  const field = (key: keyof TaskAsrDraft, label: string, secret = false) => (
    <label className="video-duplex-model-settings__field" key={key}>
      <span>{label}</span>
      <input
        type={secret ? 'password' : 'text'}
        value={draft[key]}
        onChange={(event) => update(key, event.target.value)}
        autoComplete="off"
      />
    </label>
  );
  return (
    <FormDialog
      open
      title={t('settingsPanel.taskAsr.dialogTitle')}
      submitting={submitting}
      confirmDisabled={!isConnected}
      confirmLabel={enableOnSave ? t('settingsPanel.agent.saveAndEnable') : t('common.save')}
      cancelLabel={t('common.cancel')}
      dialogClassName="settings-model-dialog"
      testIdPrefix="settings-task-asr-config-dialog"
      onConfirm={() => void confirm()}
      onCancel={onClose}
    >
      <div className="video-duplex-model-settings__dialog-fields">
        {field('api_base', t('settingsPanel.fields.asr_api_base.title'))}
        {field('api_key', t('settingsPanel.fields.asr_api_key.title'), true)}
        {field('model', t('settingsPanel.fields.asr_model.title'))}
      </div>
      {error ? <div className="settings-page__error" role="alert">{error}</div> : null}
    </FormDialog>
  );
}

export function TaskAsrSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const enabled = parseConfigBoolean(source.values.task_asr_enabled ?? 'false');
  const configFields = ['asr_api_base', 'asr_api_key', 'asr_model'] as const;
  const configured = configFields.every((key) => String(source.values[key] ?? '').trim());
  const [dialogOpen, setDialogOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const busy = configFields.some((key) => source.savingKeys.has(key)) || source.savingKeys.has('task_asr_enabled');
  const modelName = String(source.values.asr_model ?? '');
  const name = t('settingsPanel.fields.task_asr_enabled.title');

  async function updateTaskAsr(next: boolean): Promise<void> {
    if (next && !configured) {
      setDialogOpen(true);
      return;
    }
    await source.save({ task_asr_enabled: next }, 'task-asr-enabled');
    setTaskAsrEnabled(next);
  }

  const clearConfig = async () => {
    setDeleting(true);
    setDeleteError('');
    try {
      await source.save(
        { asr_api_base: '', asr_api_key: '', asr_model: '', task_asr_enabled: 'false' },
        'task-asr-model-delete',
      );
      setTaskAsrEnabled(false);
      setDeleteOpen(false);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setDeleting(false);
    }
  };

  const subSettings = enabled ? (
    configured ? (
      <div className="settings-agent-media__model-card">
        <strong className="settings-agent-media__model-name">{modelName}</strong>
        <div className="settings-agent-media__actions">
          <Button
            variant="quiet"
            size="sm"
            icon={<settingsActionIcons.edit aria-hidden />}
            title={t('common.modify')}
            aria-label={`${t('common.modify')} ${name}`}
            disabled={disabled || !isConnected || busy}
            onClick={() => setDialogOpen(true)}
            data-testid="settings-task-asr-edit-btn"
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
              setDeleteOpen(true);
            }}
            data-testid="settings-task-asr-delete-btn"
          />
        </div>
      </div>
    ) : (
      <div className="settings-agent-media__model-card">
        <span className="settings-agent-media__model-name">{t('settingsPanel.taskAsr.notConfigured')}</span>
        <Button size="sm" variant="primary" disabled={disabled || !isConnected} onClick={() => setDialogOpen(true)}>
          {t('settingsPanel.common.configure')}
        </Button>
      </div>
    )
  ) : undefined;

  return (
    <>
      <SettingRow
        className="settings-agent-media__row"
        title={name}
        description={t('settingsPanel.fields.task_asr_enabled.description')}
        subSettings={subSettings}
      >
        <Switch
          aria-label={name}
          checked={enabled}
          disabled={disabled || !isConnected || busy}
          onChange={(next) => void updateTaskAsr(next).catch(() => undefined)}
          data-testid="settings-task-asr-switch"
        />
      </SettingRow>
      {dialogOpen ? (
        <TaskAsrConfigDialog
          initialValues={{
            api_base: String(source.values.asr_api_base ?? ''),
            api_key: String(source.values.asr_api_key ?? ''),
            model: String(source.values.asr_model ?? ''),
          }}
          enableOnSave={!enabled}
          save={source.save}
          onClose={() => setDialogOpen(false)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={deleteOpen}
        title={t('settingsPanel.agent.deleteModelTitle')}
        message={t('settingsPanel.agent.deleteModelConfirm', { name })}
        confirming={deleting}
        error={deleteError}
        onConfirm={() => void clearConfig()}
        onCancel={() => {
          if (!deleting) setDeleteOpen(false);
        }}
      />
    </>
  );
}

export function ProactiveLimitsSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const [limitsOpen, setLimitsOpen] = useState(false);
  const proactive = parseConfigBoolean(source.values.proactive_recommendation_enabled);
  return (
    <>
      <SettingRow
        title={t('settingsPanel.experimental.proactiveLimits')}
        description={t(
          proactive
            ? 'settingsPanel.experimental.proactiveLimitsDescription'
            : 'settingsPanel.experimental.proactiveLimitsDisabledDescription',
          {
            daily: String(source.values.proactive_recommendation_max_recommend_per_day ?? 10),
            rounds: String(source.values.proactive_recommendation_max_rounds_per_tick ?? 20),
          },
        )}
      >
        <Button disabled={disabled || !isConnected} onClick={() => setLimitsOpen(true)} data-testid="settings-proactive-limits-modify-btn">
          {t('common.modify')}
        </Button>
      </SettingRow>
      {limitsOpen ? (
        <ProactiveLimitsDialog
          values={{
            daily: String(source.values.proactive_recommendation_max_recommend_per_day ?? 10),
            rounds: String(source.values.proactive_recommendation_max_rounds_per_tick ?? 20),
          }}
          onClose={() => setLimitsOpen(false)}
          onSave={async (values) => {
            await source.save(
              {
                proactive_recommendation_max_recommend_per_day: values.daily,
                proactive_recommendation_max_rounds_per_tick: values.rounds,
              },
              'proactive-limits',
            );
          }}
        />
      ) : null}
    </>
  );
}

/**
 * Third-party agents entry for the experimental page. ONLY the "add agent"
 * upload capability is exposed here: uploaded agents' enable/path management
 * stays on the agents page, so this section renders the upload panel alone.
 */
function ExternalCliCustomAgentsSettings({
  config,
  onConfigPatch,
  inheritedDisabled,
}: {
  config: Record<string, unknown>;
  onConfigPatch: (updates: Record<string, unknown>) => void;
  inheritedDisabled: boolean;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const customAgents = useMemo(
    () => parseExternalCliCustomAgents(config[EXTERNAL_CLI_CUSTOM_AGENTS_CONFIG_KEY]),
    [config],
  );
  if (config.external_cli_agents_supported !== undefined && !parseConfigBoolean(config.external_cli_agents_supported))
    return null;
  return (
    <div className="settings-experimental-cli" data-testid="settings-experimental-cli">
      <AddCustomAgentPanel
        disabled={inheritedDisabled || !isConnected}
        appearance="hero"
        t={t}
        onUpload={async (wheel, cli) => {
          const agent = await uploadCustomAgent(wheel, cli);
          // A re-upload replaces the agent in place; the backend install
          // endpoint owns persisting the agent list, so a local patch keeps
          // the UI responsive until the refreshed config arrives.
          const next = [...customAgents.filter((item) => item.name !== agent.name), agent];
          onConfigPatch({ [EXTERNAL_CLI_CUSTOM_AGENTS_CONFIG_KEY]: JSON.stringify(next) });
        }}
      />
    </div>
  );
}

export function ExternalCliCustomAgentsItem({ disabled }: SettingsCustomItemProps) {
  const source = useSettingsSource();
  return (
    <ExternalCliCustomAgentsSettings
      config={source.values}
      inheritedDisabled={disabled}
      onConfigPatch={source.patchLocal}
    />
  );
}
