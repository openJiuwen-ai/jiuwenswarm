// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Switch } from '../../../../components/ui';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { setA2UIFeatureEnabled } from '../../../../features/a2ui/featureConfig';
import { setTrajectoryUiEnabled } from '../../../../features/trajectory/featureConfig';
import { setTaskFullDuplexEnabled } from '../../../../features/taskFullDuplex/featureFlag';
import { setTaskAsrEnabled } from '../../../../features/taskAsr/featureFlag';
import { VideoDuplexModelSettings } from './VideoDuplexModelSettings';
import '../agent/AgentSettings.css';
import './VideoDuplexModelSettings.css';
import {
  EXTERNAL_CLI_AGENT_KINDS,
  ExternalCliAgentsSection,
  applyExternalCliAgentAtomicUpdates,
  externalCliDependencyInstalls,
  externalCliSaveValidationMessage,
  externalCliKey,
  type ExternalCliAgentKind,
  type ExternalCliConfigSaveResult,
  type ExternalCliPendingChoice,
} from '../../../../components/ExternalCliAgentsSection';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';
import {
  applyExternalCliPendingChoices,
  hasUnsavedExternalCliChanges,
} from './externalCliInstallState';

const CLI_DEFAULTS: Record<string, string> = Object.fromEntries(
  EXTERNAL_CLI_AGENT_KINDS.flatMap((agent) => [
    [externalCliKey(agent, 'enabled'), 'false'],
    [externalCliKey(agent, 'use_builtin'), 'false'],
    [externalCliKey(agent, 'cli_path'), ''],
  ]),
);

/** Auto-dismiss both the save-failure banner and the auto-save success notice after this delay. */
const NOTICE_AUTO_DISMISS_MS = 8000;

/**
 * Module-scope registry of deferred-choice replays whose async save is still
 * running. Module scope (not a per-component ref) so remounting the Settings
 * page while a replay is in flight cannot fire a duplicate save for the same
 * agent: the marker lives until that save settles. Pending choices themselves
 * are only consumed after a save succeeds, so an interrupted or failed replay
 * is picked up again the next time this component mounts.
 */
const externalCliReplayInFlight = new Set<ExternalCliAgentKind>();

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
    externalCliPendingChoices,
    externalCliDetectResults,
    onDetectExternalCli,
    onExternalCliPendingChoicesChange,
    onExternalCliDetectResultsChange,
    onOpenExternalCliInstallDialog,
    onSelectExternalCliPath,
    onTrackExternalCliDependencyInstalls,
  } = useSettingsServices();
  const sourceValues = useMemo(
    () =>
      Object.fromEntries(Object.entries(CLI_DEFAULTS).map(([key, fallback]) => [key, String(config[key] ?? fallback)])),
    [config],
  );
  // User choices deferred behind a dependency install. Held at the App layer
  // and session storage so they survive navigation and full page refreshes.
  const pendingChoices = externalCliPendingChoices ?? {};
  const setPendingChoices = onExternalCliPendingChoicesChange;
  const restoredDraftValues = useMemo(
    () => applyExternalCliPendingChoices(sourceValues, pendingChoices),
    [pendingChoices, sourceValues],
  );
  const [savedValues, setSavedValues] = useState(sourceValues);
  const [draftValues, setDraftValues] = useState(restoredDraftValues);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  // Latest CLI detect results, held at the App layer (via services) so they
  // survive unmounting the Settings page; the save flow pre-validates them to
  // surface a localized error instead of the backend's raw English message.
  const detectResults = externalCliDetectResults ?? {};
  const setDetectResults = onExternalCliDetectResultsChange;
  const installDialogRestoredRef = useRef(false);
  const [autoSavedAgents, setAutoSavedAgents] = useState<string[]>([]);
  const changed = hasUnsavedExternalCliChanges(
    savedValues,
    draftValues,
    pendingChoices,
    externalCliInstallStatuses,
  );
  useEffect(() => {
    if (!saveError) return undefined;
    const timer = window.setTimeout(() => setSaveError(''), NOTICE_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [saveError]);
  // Auto-save success notices fade out like the error banner does.
  useEffect(() => {
    if (!autoSavedAgents.length) return undefined;
    const timer = window.setTimeout(() => setAutoSavedAgents([]), NOTICE_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [autoSavedAgents]);
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
      setDraftValues(restoredDraftValues);
    }
  }, [changed, restoredDraftValues, sourceValues]);

  useEffect(() => {
    if (!installBusy) {
      installDialogRestoredRef.current = false;
      return;
    }
    if (installDialogRestoredRef.current) return;
    installDialogRestoredRef.current = true;
    onOpenExternalCliInstallDialog?.();
  }, [installBusy, onOpenExternalCliInstallDialog]);

  // When a dependency install finishes, replay the user's deferred choices:
  // save on success, drop on failure (user retries manually as before).
  useEffect(() => {
    const replays = EXTERNAL_CLI_AGENT_KINDS.filter((agent) => {
      const pending = pendingChoices[agent];
      if (!pending) return false;
      const status = externalCliInstallStatuses?.[agent]?.status;
      return status === 'succeeded' || status === 'failed';
    });
    if (!replays.length) return;
    // Guard against StrictMode double-invoke and against remounting the page
    // while a replay save is still running: the in-flight marker is module
    // scoped. Entries are only consumed (deleted from pendingChoices) after
    // their save succeeds, so an interrupted or failed replay is picked up
    // again the next time this component mounts.
    const fresh = replays.filter((agent) => !externalCliReplayInFlight.has(agent));
    const consumed: Record<string, ExternalCliPendingChoice> = {};
    for (const agent of fresh) {
      consumed[agent] = pendingChoices[agent]!;
      externalCliReplayInFlight.add(agent);
    }
    if (!fresh.length) return;
    // Failed installs are not retried: consume their entries immediately.
    const failed = fresh.filter(
      (agent) => externalCliInstallStatuses?.[agent]?.status !== 'succeeded',
    );
    if (failed.length) {
      setPendingChoices?.((current) => {
        const next: typeof current = { ...current };
        for (const agent of failed) {
          delete next[agent];
        }
        return next;
      });
    }
    const succeeded = fresh.filter(
      (agent) => externalCliInstallStatuses?.[agent]?.status === 'succeeded',
    );
    if (!succeeded.length) return;
    // Saving is armed once and released only after every replay settles, so a
    // fast first save cannot unlock the form while later ones are still running.
    setSaving(true);
    setSaveError('');
    void (async () => {
      try {
        await Promise.allSettled(
          succeeded.map(async (agent) => {
            const pending = consumed[agent];
            const clearPending = (choice: ExternalCliPendingChoice) => {
              setPendingChoices?.((current) => {
                const next: typeof current = { ...current };
                if (next[agent] && next[agent] === choice) delete next[agent];
                return next;
              });
            };
            try {
              await onSave({
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              });
              clearPending(pending);
              setSavedValues((current) => ({
                ...current,
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              }));
              setDraftValues((current) => ({
                ...current,
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              }));
              onConfigPatch({
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              });
              setAutoSavedAgents((current) => [...current, agent]);
            } catch (error) {
              setSaveError(
                error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'),
              );
            } finally {
              externalCliReplayInFlight.delete(agent);
            }
          }),
        );
      } finally {
        setSaving(false);
      }
    })();
  }, [externalCliInstallStatuses, onConfigPatch, onSave, pendingChoices, setPendingChoices, t]);

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
    // Pre-validate against the latest detect results: fail fast with a
    // localized message before the backend rejects with its raw English error.
    for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
      const validation = externalCliSaveValidationMessage(agent, draftValues, detectResults[agent], t);
      if (validation) {
        setSaveError(validation);
        return;
      }
    }
    setSaving(true);
    setSaveError('');
    try {
      const result = await onSave(updates);
      const installs = externalCliDependencyInstalls(result);
      const deferredAgents = EXTERNAL_CLI_AGENT_KINDS.filter(
        (agent) => installs[agent]?.status === 'running',
      );
      // The backend skips persisting agents whose dependency is still installing.
      // Keep the user's choices in the form (so the toggles stay as chosen) and
      // remember them for an automatic re-save once the install succeeds.
      if (deferredAgents.length) {
        setPendingChoices?.((current) => {
          const next: typeof current = { ...current };
          for (const agent of deferredAgents) {
            next[agent] = {
              enabled: draftValues[externalCliKey(agent, 'enabled')] === 'true' ? 'true' : 'false',
              useBuiltin: draftValues[externalCliKey(agent, 'use_builtin')] === 'true' ? 'true' : 'false',
              cliPath: (draftValues[externalCliKey(agent, 'cli_path')] ?? '').trim(),
            };
          }
          return next;
        });
      }
      if (Object.keys(installs).length) onTrackExternalCliDependencyInstalls?.(installs);
      // Deferred agents are not persisted yet: align savedValues and the config
      // store with what the backend actually stored (agent disabled), while
      // draftValues keeps the user's selection visible until the automatic
      // re-save completes.
      const nextSaved = { ...savedValues };
      const configPatch: Record<string, string> = {};
      for (const agent of deferredAgents) {
        nextSaved[externalCliKey(agent, 'enabled')] = 'false';
        nextSaved[externalCliKey(agent, 'use_builtin')] = 'false';
        nextSaved[externalCliKey(agent, 'cli_path')] = '';
        configPatch[externalCliKey(agent, 'enabled')] = 'false';
        configPatch[externalCliKey(agent, 'use_builtin')] = 'false';
        configPatch[externalCliKey(agent, 'cli_path')] = '';
      }
      // Non-deferred updates were persisted verbatim: keep savedValues,
      // draftValues, and the config store in lockstep so `changed` clears.
      const isDeferredKey = (key: string) =>
        deferredAgents.some((agent) => key.startsWith(`external_cli_agent_${agent}_`));
      for (const [key, value] of Object.entries(updates)) {
        if (isDeferredKey(key)) continue;
        nextSaved[key] = value;
        configPatch[key] = value;
      }
      setSavedValues(nextSaved);
      // draftValues keeps deferred agents' user selections; everything else
      // mirrors what was persisted (including the normalized values the atomic
      // updater may have produced, e.g. a cleared cli_path on disable).
      setDraftValues((current) => {
        const next = { ...current };
        for (const [key, value] of Object.entries(updates)) {
          if (isDeferredKey(key)) continue;
          next[key] = value;
        }
        return next;
      });
      if (Object.keys(configPatch).length) onConfigPatch(configPatch);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  if (config.external_cli_agents_supported !== undefined && !parseConfigBoolean(config.external_cli_agents_supported))
    return null;
  return (
    <div className="settings-experimental-cli" data-testid="settings-experimental-cli">
      <span className="settings-experimental-cli__scope">
        {t('settingsPanel.experimental.externalCliAgentsDescription')}
      </span>
      {installBusy ? (
        <div className="settings-experimental-cli__install-running" role="status" data-testid="settings-experimental-cli-install-status" data-variant="running">
          <span>
            {t('config.externalCli.installInProgress', {
              agents: installAgentNames || t('config.externalCli.claudeCodex'),
            })}
          </span>
          <Button onClick={onOpenExternalCliInstallDialog} data-testid="settings-experimental-cli-install-view-progress-btn">{t('config.externalCli.installViewProgress')}</Button>
        </div>
      ) : null}
      {autoSavedAgents.length ? (
        <div className="settings-experimental-cli__install-running settings-experimental-cli__install-running--success" role="status">
          {t('config.externalCli.autoSaved', {
            agents: autoSavedAgents
              .map((agent) => (agent === 'claude' ? 'Claude' : 'Codex'))
              .join(' / '),
          })}
        </div>
      ) : null}
      {saveError ? (
        <div className="settings-page__error" role="alert" data-testid="settings-experimental-cli-error">
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
        initialResults={detectResults}
        onResultsChange={setDetectResults}
        t={t}
        disabled={disabled}
      />
      <div className="settings-experimental-cli__actions" data-testid="settings-experimental-cli-actions">
        <Button
          disabled={disabled || !changed}
          onClick={() => {
            setDraftValues(savedValues);
            setSaveError('');
          }}
          data-testid="settings-experimental-cli-cancel-btn"
        >
          {t('common.cancel')}
        </Button>
        <Button variant="primary" disabled={disabled || !changed} onClick={() => void submit()} data-testid="settings-experimental-cli-save-btn">
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
        data-testid="settings-a2ui-switch"
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
    <>
      <SettingRow
        className="settings-agent-media__row"
        title={t('settingsPanel.fields.task_full_duplex_enabled.title')}
        description={t('settingsPanel.fields.task_full_duplex_enabled.description')}
        subSettings={enabled ? <VideoDuplexModelSettings /> : undefined}
      >
        <Switch
          aria-label={t('settingsPanel.fields.task_full_duplex_enabled.title')}
          checked={enabled}
          disabled={disabled || !isConnected || source.savingKeys.has('task_full_duplex_enabled')}
          onChange={(next) => void updateTaskFullDuplex(next).catch(() => undefined)}
          data-testid="settings-task-full-duplex-switch"
        />
      </SettingRow>
    </>
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
