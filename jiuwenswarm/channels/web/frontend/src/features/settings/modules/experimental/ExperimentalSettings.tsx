// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Switch } from '../../../../components/ui';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { setA2UIFeatureEnabled } from '../../../../features/a2ui/featureConfig';
import { setTrajectoryUiEnabled } from '../../../../features/trajectory/featureConfig';
import { setTaskFullDuplexEnabled } from '../../../../features/taskFullDuplex/featureFlag';
import {
  EXTERNAL_CLI_AGENT_KINDS,
  ExternalCliAgentsSection,
  applyExternalCliAgentAtomicUpdates,
  externalCliDependencyInstalls,
  externalCliKey,
  type ExternalCliConfigSaveResult,
} from '../../../../components/ExternalCliAgentsSection';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';

const CLI_DEFAULTS: Record<string, string> = Object.fromEntries(
  EXTERNAL_CLI_AGENT_KINDS.flatMap((agent) => [
    [externalCliKey(agent, 'enabled'), 'false'],
    [externalCliKey(agent, 'use_builtin'), 'false'],
    [externalCliKey(agent, 'cli_path'), ''],
  ]),
);

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
        onConfirm={() => void submit()}
        onCancel={requestClose}
      >
        <Form
          form={form}
          optionalText={t('common.optional')}
          rules={{ daily: [{ validator }], rounds: [{ validator }] }}
          items={[
            {
              name: 'daily',
              label: t('settingsPanel.fields.proactive_recommendation_max_recommend_per_day.title'),
              component: 'input',
              type: 'number',
              required: true,
            },
            {
              name: 'rounds',
              label: t('settingsPanel.fields.proactive_recommendation_max_rounds_per_tick.title'),
              component: 'input',
              type: 'number',
              required: true,
            },
          ]}
        />
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

function ExternalCliSettings({
  config,
  onConfigPatch,
  onSave,
  inheritedDisabled,
}: {
  config: Record<string, unknown>;
  onConfigPatch: (updates: Record<string, unknown>) => void;
  onSave: (values: Record<string, string>) => Promise<ExternalCliConfigSaveResult | void>;
  inheritedDisabled: boolean;
}) {
  const { t } = useTranslation();
  const {
    isConnected,
    externalCliInstallBusy,
    externalCliInstallStatuses,
    onDetectExternalCli,
    onOpenExternalCliInstallDialog,
    onSelectExternalCliPath,
    onTrackExternalCliDependencyInstalls,
  } = useSettingsServices();
  const sourceValues = useMemo(
    () =>
      Object.fromEntries(Object.entries(CLI_DEFAULTS).map(([key, fallback]) => [key, String(config[key] ?? fallback)])),
    [config],
  );
  const [savedValues, setSavedValues] = useState(sourceValues);
  const [draftValues, setDraftValues] = useState(sourceValues);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const installDialogRestoredRef = useRef(false);
  const changed = Object.keys(savedValues).some((key) => draftValues[key] !== savedValues[key]);
  const installAgents = EXTERNAL_CLI_AGENT_KINDS.filter(
    (agent) => externalCliInstallStatuses?.[agent]?.status === 'running',
  );
  const installAgentNames = installAgents.map((agent) => (agent === 'claude' ? 'Claude' : 'Codex')).join(' / ');
  const installBusy = externalCliInstallBusy === true;
  const disabled = inheritedDisabled || saving || installBusy || !isConnected;
  useUnsavedChanges('external-cli', changed);

  useEffect(() => {
    if (!changed) {
      setSavedValues(sourceValues);
      setDraftValues(sourceValues);
    }
  }, [changed, sourceValues]);

  useEffect(() => {
    if (!installBusy) {
      installDialogRestoredRef.current = false;
      return;
    }
    if (installDialogRestoredRef.current) return;
    installDialogRestoredRef.current = true;
    onOpenExternalCliInstallDialog?.();
  }, [installBusy, onOpenExternalCliInstallDialog]);

  const submit = async () => {
    if (installBusy) {
      onOpenExternalCliInstallDialog?.();
      return;
    }
    const updates: Record<string, string> = {};
    for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
      applyExternalCliAgentAtomicUpdates(updates, agent, draftValues, savedValues);
    }
    if (!Object.keys(updates).length) return;
    setSaving(true);
    setSaveError('');
    try {
      const result = await onSave(updates);
      const nextValues = { ...draftValues };
      const installs = externalCliDependencyInstalls(result);
      for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
        if (installs[agent]?.status !== 'running') continue;
        nextValues[externalCliKey(agent, 'enabled')] = 'false';
        nextValues[externalCliKey(agent, 'use_builtin')] = 'false';
        nextValues[externalCliKey(agent, 'cli_path')] = '';
      }
      if (Object.keys(installs).length) onTrackExternalCliDependencyInstalls?.(installs);
      setSavedValues(nextValues);
      setDraftValues(nextValues);
      onConfigPatch(nextValues);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  if (config.external_cli_agents_supported !== undefined && !parseConfigBoolean(config.external_cli_agents_supported))
    return null;
  return (
    <div className="settings-experimental-cli">
      <span className="settings-experimental-cli__scope">
        {t('settingsPanel.experimental.externalCliAgentsDescription')}
      </span>
      {installBusy ? (
        <div className="settings-experimental-cli__install-running" role="status">
          <span>
            {t('config.externalCli.installInProgress', {
              agents: installAgentNames || t('config.externalCli.claudeCodex'),
            })}
          </span>
          <Button onClick={onOpenExternalCliInstallDialog}>{t('config.externalCli.installViewProgress')}</Button>
        </div>
      ) : null}
      {saveError ? (
        <div className="settings-page__error" role="alert">
          {saveError}
        </div>
      ) : null}
      <ExternalCliAgentsSection
        draftValues={draftValues}
        onChange={(key, value) => {
          setDraftValues((current) => ({ ...current, [key]: value }));
          setSaveError('');
        }}
        onDetect={onDetectExternalCli}
        onSelectFile={onSelectExternalCliPath}
        t={t}
        disabled={disabled}
      />
      <div className="settings-experimental-cli__actions">
        <Button
          disabled={disabled || !changed}
          onClick={() => {
            setDraftValues(savedValues);
            setSaveError('');
          }}
        >
          {t('common.cancel')}
        </Button>
        <Button variant="primary" disabled={disabled || !changed} onClick={() => void submit()}>
          {t('common.save')}
        </Button>
      </div>
    </div>
  );
}

export function ExternalCliSettingsItem({ disabled }: SettingsCustomItemProps) {
  const source = useSettingsSource();
  return (
    <ExternalCliSettings
      config={source.values}
      inheritedDisabled={disabled}
      onConfigPatch={source.patchLocal}
      onSave={(updates) => source.save(updates, 'external-cli-agents') as Promise<ExternalCliConfigSaveResult | void>}
    />
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
      />
    </SettingRow>
  );
}

export function TrajectoryUiSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const enabled = parseConfigBoolean(source.values.trajectory_ui_enabled);

  async function updateTrajectoryUi(next: boolean): Promise<void> {
    await source.save({ trajectory_ui_enabled: next }, 'trajectory-ui-enabled');
    setTrajectoryUiEnabled(next);
  }

  return (
    <SettingRow
      title={t('settingsPanel.fields.trajectory_ui_enabled.title')}
      description={t('settingsPanel.fields.trajectory_ui_enabled.description')}
    >
      <Switch
        aria-label={t('settingsPanel.fields.trajectory_ui_enabled.title')}
        checked={enabled}
        disabled={disabled || !isConnected || source.savingKeys.has('trajectory_ui_enabled')}
        onChange={(next) => void updateTrajectoryUi(next).catch(() => undefined)}
      />
    </SettingRow>
  );
}

export function TaskFullDuplexSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const enabled = parseConfigBoolean(source.values.task_full_duplex_enabled);

  async function updateTaskFullDuplex(next: boolean): Promise<void> {
    await source.save({ task_full_duplex_enabled: next }, 'task-full-duplex-enabled');
    setTaskFullDuplexEnabled(next);
  }

  return (
    <SettingRow
      title={t('settingsPanel.fields.task_full_duplex_enabled.title')}
      description={t('settingsPanel.fields.task_full_duplex_enabled.description')}
    >
      <Switch
        aria-label={t('settingsPanel.fields.task_full_duplex_enabled.title')}
        checked={enabled}
        disabled={disabled || !isConnected || source.savingKeys.has('task_full_duplex_enabled')}
        onChange={(next) => void updateTaskFullDuplex(next).catch(() => undefined)}
      />
    </SettingRow>
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
        <Button disabled={disabled || !isConnected} onClick={() => setLimitsOpen(true)}>
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
